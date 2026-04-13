#!/usr/bin/env python3
"""Audible audiobook backup.

Maintains books (1).json as the canonical library inventory, using audible-cli
as the source of truth for "what's in my Audible account." Downloads new
purchases, decrypts to M4B, and appends enriched records to the inventory.

Usage:
    python3 backup.py [--dry-run] [--json]

Mounts expected:
    /library     — audiobook library root
    /auth        — audible-cli auth dir (~/.audible)

Environment:
    AUDIOBOOK_DIR — override library path (default: /library)
"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

# Audible API response groups for enriched library data.
# listening_status is REQUIRED — it's the source of is_finished and
# percent_complete which we need for read_status mapping.
LIBRARY_RESPONSE_GROUPS = (
    "contributors,product_desc,product_extended_attrs,product_attrs,"
    "media,rating,series,categories,category_ladders,listening_status,"
    "product_details,reviews,relationships"
)


# Required fields the API must return on every book. If any are missing,
# the script fails loudly so we know the API contract changed.
REQUIRED_API_FIELDS = ("asin", "title")

# Fields we expect to populate in every output record. Used by validation
# to catch silent data loss.
REQUIRED_OUTPUT_FIELDS = (
    "asin", "product_id", "title", "author", "narrated_by", "duration",
    "seconds", "release_date", "image_url", "info_link", "author_link",
    "abridged", "ayce", "read_status", "genre", "key", "filename", "files",
    "rating_average", "rating_count", "summary", "description", "copyright",
    "language", "region", "publisher", "purchase_date", "title_short",
    "user_id", "download_link",
    # Series + extras
    "series_name", "series_sequence", "series_link", "subtitle",
    "percent_complete", "isbn", "sku", "voice_description", "narration_accent",
    "format_type", "content_type", "publication_datetime", "date_added",
    "performance_rating", "performance_rating_count",
    "story_rating", "story_rating_count",
    "chapters",
)


class FieldValidationError(Exception):
    """Raised when a record's fields don't match the expected schema."""
    pass


def py_bool_str(value) -> str:
    """OpenAudible-compatible bool string: 'true' / 'false' (lowercase)."""
    return "true" if value else "false"


def safe_str(value, default: str = "") -> str:
    """Return value as string, or default if None/empty."""
    if value is None or value == "":
        return default
    return str(value)


def validate_record(record: dict, asin: str) -> None:
    """Raise FieldValidationError if record is missing required fields.

    Empty values are OK (some books don't have all metadata), but the
    KEY must be present so downstream consumers can rely on the schema.
    """
    missing = [f for f in REQUIRED_OUTPUT_FIELDS if f not in record]
    if missing:
        raise FieldValidationError(
            f"Record for {asin} missing required fields: {missing}"
        )


def find_inventory(library_dir: Path) -> Optional[Path]:
    """Find the inventory JSON. Google Drive may rename to 'books (1).json'."""
    for name in ["books (1).json", "books.json"]:
        p = library_dir / name
        if p.exists():
            return p
    return None


def load_inventory(inventory_path: Path) -> list[dict]:
    """Load existing inventory."""
    return json.loads(inventory_path.read_text())


def save_inventory(inventory_path: Path, books: list[dict]) -> None:
    """Save inventory atomically (write to temp + rename)."""
    tmp = inventory_path.with_suffix(inventory_path.suffix + ".tmp")
    tmp.write_text(json.dumps(books, indent=2, ensure_ascii=False))
    tmp.replace(inventory_path)


def fetch_enriched_library(tmp_dir: Path) -> list[dict]:
    """Fetch full Audible library with enriched metadata via API.

    Uses /1.0/library with response_groups for full field set, paginating
    through results. Returns all owned items.
    """
    all_items: list[dict] = []
    page = 1
    page_size = 1000
    while True:
        out = tmp_dir / f"library-page-{page}.json"
        subprocess.run(
            [
                "audible", "api", "/1.0/library",
                "-p", f"response_groups={LIBRARY_RESPONSE_GROUPS}",
                "-p", f"num_results={page_size}",
                "-p", f"page={page}",
                "-o", str(out),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        data = json.loads(out.read_text())
        items = data.get("items", [])
        if not items:
            break
        all_items.extend(items)
        if len(items) < page_size:
            break
        page += 1
    return all_items


def fetch_chapters(asin: str) -> list[dict]:
    """Fetch chapter list for a book via /1.0/content/{asin}/metadata.

    Returns OpenAudible-compatible chapter list. Returns empty list on failure.
    """
    try:
        result = subprocess.run(
            [
                "audible", "api", f"/1.0/content/{asin}/metadata",
                "-p", "response_groups=chapter_info",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        data = json.loads(result.stdout)
        return data.get("content_metadata", {}).get("chapter_info", {}).get("chapters", [])
    except (subprocess.CalledProcessError, json.JSONDecodeError, KeyError):
        return []


def get_known_asins(books: list[dict]) -> set[str]:
    """Extract ASINs from inventory."""
    return {b["asin"] for b in books if b.get("asin")}


def find_new_books(library: list[dict], known_asins: set[str]) -> list[dict]:
    """Find books in Audible library not yet in inventory."""
    return [
        book for book in library
        if book.get("asin") and book["asin"] not in known_asins
    ]


def map_to_inventory_schema(api_book: dict, region: str = "US") -> dict:
    """Map enriched audible-cli API response to inventory JSON schema.

    Field conventions match OpenAudible's books.json schema:
    - Bools serialized as 'true'/'false' lowercase strings
    - None/missing values become empty strings (not 'None')
    - URLs use audible.com format

    Raises FieldValidationError if required API fields are missing.
    """
    asin = api_book.get("asin", "")
    title = safe_str(api_book.get("title"))

    # Validate required API fields
    for field in REQUIRED_API_FIELDS:
        if not api_book.get(field):
            raise FieldValidationError(
                f"API response missing required field '{field}' "
                f"for ASIN={asin or '(no asin)'}"
            )

    # Authors and narrators come as lists of {asin, name} dicts
    authors = api_book.get("authors") or []
    narrators = api_book.get("narrators") or []
    author_str = ", ".join(a.get("name", "") for a in authors if a.get("name"))
    narrator_str = ", ".join(n.get("name", "") for n in narrators if n.get("name"))

    # Categories: each "ladder" is a hierarchy path (e.g.,
    # ["Mystery, Thriller & Suspense", "Crime Fiction"]).
    # OpenAudible format: "Top:Sub:SubSub" per path, multiple paths comma-separated.
    genre_paths: list[str] = []
    for ladder in api_book.get("category_ladders") or []:
        names = [
            cat.get("name") for cat in (ladder.get("ladder") or [])
            if cat.get("name")
        ]
        if names:
            path = ":".join(names)
            if path not in genre_paths:
                genre_paths.append(path)
    genre_str = ", ".join(genre_paths)

    # Rating is nested. Three separate scores: overall, performance (narration),
    # story. Useful signal for recommendations (a book may rate high overall
    # but be carried entirely by performance, or vice versa).
    rating = api_book.get("rating") or {}
    overall = rating.get("overall_distribution") or {}
    perf = rating.get("performance_distribution") or {}
    story = rating.get("story_distribution") or {}
    rating_average = overall.get("average_rating", "")
    rating_count = overall.get("num_ratings", "") or rating.get("num_reviews", "")
    perf_rating = perf.get("average_rating", "")
    perf_count = perf.get("num_ratings", "")
    story_rating = story.get("average_rating", "")
    story_count = story.get("num_ratings", "")

    # Duration: API gives runtime_length_min
    runtime_min = api_book.get("runtime_length_min") or 0
    runtime_sec = int(runtime_min) * 60 if runtime_min else 0
    duration_str = (
        f"{int(runtime_min) // 60:02d}:{int(runtime_min) % 60:02d}:00"
        if runtime_min else ""
    )

    # Series — first series in list (book may be in multiple)
    series_list = api_book.get("series") or []
    series_first = series_list[0] if series_list else {}
    series_name = safe_str(series_first.get("title"))
    series_sequence = safe_str(series_first.get("sequence"))
    series_asin = safe_str(series_first.get("asin"))
    series_link = f"/series/{series_name.replace(' ', '-')}/{series_asin}" if series_asin else ""

    # Author link — OpenAudible uses audible.com/author/{Name}/{ASIN}
    author_link = ""
    if authors and authors[0].get("asin") and authors[0].get("name"):
        name_url = authors[0]["name"].replace(" ", "+")
        author_link = f"https://www.audible.com/author/{name_url}/{authors[0]['asin']}"

    # info_link — construct from ASIN; API may return None
    api_info_link = api_book.get("product_page_url")
    info_link = api_info_link if api_info_link else f"https://www.audible.com/pd/{asin}"

    # listening_status nested object — required for read_status mapping
    listening = api_book.get("listening_status") or {}
    is_finished = listening.get("is_finished") or api_book.get("is_finished")
    pct = listening.get("percent_complete") or api_book.get("percent_complete") or 0

    # read_status: OpenAudible 3-state convention (Finished/Reading/Unread)
    if is_finished:
        read_status = "Finished"
    elif pct and float(pct) > 0:
        read_status = "Reading"
    else:
        read_status = "Unread"

    record = {
        # --- Identity ---
        "asin": asin,
        "product_id": safe_str(api_book.get("sku") or asin),
        "key": safe_str(api_book.get("sku") or asin),

        # --- Titles ---
        "title": title,
        "title_short": title[:200],
        "subtitle": safe_str(api_book.get("subtitle")),

        # --- Contributors ---
        "author": author_str,
        "author_link": author_link,
        "narrated_by": narrator_str,
        "voice_description": safe_str(api_book.get("voice_description")),
        "narration_accent": safe_str(api_book.get("narration_accent")),

        # --- Publisher / metadata ---
        "publisher": safe_str(api_book.get("publisher_name")),
        "summary": safe_str(api_book.get("merchandising_summary")),
        "description": safe_str(api_book.get("publisher_summary")),
        "copyright": safe_str(api_book.get("copyright")),
        "language": safe_str(api_book.get("language")),
        "region": region,
        "genre": genre_str,
        "format_type": safe_str(api_book.get("format_type")),
        "content_type": safe_str(api_book.get("content_type")),
        "isbn": safe_str(api_book.get("isbn")),
        "sku": safe_str(api_book.get("sku")),

        # --- Ratings (overall + per-aspect for recommendation signal) ---
        "rating_average": str(rating_average) if rating_average else "",
        "rating_count": str(rating_count) if rating_count else "",
        "performance_rating": str(perf_rating) if perf_rating else "",
        "performance_rating_count": str(perf_count) if perf_count else "",
        "story_rating": str(story_rating) if story_rating else "",
        "story_rating_count": str(story_count) if story_count else "",

        # --- Dates ---
        "purchase_date": safe_str(api_book.get("purchase_date")),
        "release_date": safe_str(
            api_book.get("release_date") or api_book.get("issue_date")
        ),
        "publication_datetime": safe_str(api_book.get("publication_datetime")),
        # date_added is nested under library_status, not top-level
        "date_added": safe_str(
            (api_book.get("library_status") or {}).get("date_added")
            or api_book.get("date_added")
        ),

        # --- Duration ---
        "duration": duration_str,
        "seconds": runtime_sec,

        # --- Format flags (OpenAudible: lowercase string bools) ---
        "abridged": py_bool_str(api_book.get("format_type") != "unabridged"),
        "ayce": py_bool_str(api_book.get("is_ayce")),

        # --- Read status ---
        "read_status": read_status,
        "percent_complete": float(pct) if pct else 0,

        # --- Series (OpenAudible convention) ---
        "series_name": series_name,
        "series_sequence": series_sequence,
        "series_link": series_link,

        # --- Media URLs ---
        "image_url": safe_str(
            (api_book.get("product_images") or {}).get("500")
            or api_book.get("cover_url")
        ),
        "info_link": info_link,

        # --- User-specific (preserved by enrich) ---
        "user_id": "",
        "download_link": "",

        # --- Local file paths (populated after download) ---
        "filename": "",
        "files": [],

        # --- Chapters (populated separately via fetch_chapters) ---
        "chapters": [],
    }

    validate_record(record, asin)
    return record


def log(msg: str) -> None:
    """Print progress to stderr so stdout stays clean for --json output."""
    print(msg, file=sys.stderr)


def sanitize_filename(title: str) -> str:
    """Make a filesystem-safe filename from a book title."""
    safe = "".join(c for c in title if c.isalnum() or c in " ._-")
    return " ".join(safe.split())[:200]


def download_and_decrypt(
    book: dict, download_dir: Path, books_dir: Path,
    aax_dir: Path, art_dir: Path,
) -> dict:
    """Download and decrypt a single book.

    Returns book dict augmented with status, m4b_path, filename, files.
    """
    asin = book["asin"]
    title = book.get("title", "Unknown")
    log(f"\n--- Downloading: {title} ({asin}) ---")

    # Download
    result = subprocess.run(
        [
            "audible", "download",
            "--asin", asin,
            "--output-dir", str(download_dir),
            "--aax-fallback",
            "--cover", "--cover-size", "500", "--chapter",
            "--no-confirm",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        log(f"FAILED to download {asin}: {result.stderr[-500:]}")
        return {**book, "status": "failed", "error": "download failed"}

    if "not downloadable" in result.stdout + result.stderr:
        log(f"SKIPPED: {title} — not downloadable (not owned or Plus-only)")
        return {**book, "status": "skipped", "error": "not downloadable"}

    # Find downloaded files
    audio_files = list(download_dir.glob(f"{asin}*.aax")) + list(
        download_dir.glob(f"{asin}*.aaxc")
    )
    if not audio_files:
        log(f"FAILED: no audio file found for {asin}")
        return {**book, "status": "failed", "error": "no audio file after download"}

    audio_file = audio_files[0]
    voucher_files = list(download_dir.glob(f"{asin}*.voucher"))
    cover_files = list(download_dir.glob(f"{asin}*.jpg")) + list(
        download_dir.glob(f"{asin}*.png")
    )

    safe_title = sanitize_filename(title)
    output_m4b = books_dir / f"{safe_title}.m4b"

    # Decrypt
    log("Decrypting to M4B...")
    decrypt_cmd = ["audible", "decrypt", "--input", str(audio_file)]
    if voucher_files:
        decrypt_cmd += ["--voucher", str(voucher_files[0])]
    decrypt_cmd += ["--output", str(output_m4b)]

    result = subprocess.run(decrypt_cmd, capture_output=True, text=True)
    if result.returncode != 0 or not output_m4b.exists():
        log(f"FAILED to decrypt {asin}: {result.stderr[-500:]}")
        return {**book, "status": "failed", "error": "decrypt failed"}

    log(f"OK: {safe_title}.m4b")

    # Cover art
    cover_path = ""
    if cover_files:
        cover_dest = art_dir / f"{safe_title}.jpg"
        cover_dest.write_bytes(cover_files[0].read_bytes())
        cover_path = str(cover_dest)

    # Archive raw AAX
    aax_dest = aax_dir / audio_file.name
    audio_file.rename(aax_dest)

    # Cleanup temp files
    for f in download_dir.glob(f"{asin}*"):
        f.unlink()

    # Populate post-download fields
    files_list = [str(output_m4b)]
    if cover_path:
        files_list.append(cover_path)
    files_list.append(str(aax_dest))

    return {
        **book,
        "status": "ok",
        "m4b_path": str(output_m4b),
        "filename": output_m4b.name,
        "files": files_list,
    }


def main():
    parser = argparse.ArgumentParser(description="Audible audiobook backup")
    parser.add_argument("--dry-run", action="store_true",
                        help="List new books without downloading")
    parser.add_argument("--json", action="store_true",
                        help="Output results as JSON")
    args = parser.parse_args()

    library_dir = Path(os.environ.get("AUDIOBOOK_DIR", "/library"))
    tmp_dir = Path("/tmp/audible-backup")
    tmp_dir.mkdir(exist_ok=True)

    inventory_path = find_inventory(library_dir)
    if not inventory_path:
        msg = "Inventory file not found (looked for books (1).json, books.json)"
        print(json.dumps({"error": msg}) if args.json else f"ERROR: {msg}")
        sys.exit(1)

    books_dir = library_dir / "books"
    aax_dir = library_dir / "aax"
    art_dir = library_dir / "art"
    download_dir = library_dir / "tmp_download"

    # Load existing inventory
    inventory = load_inventory(inventory_path)
    known = get_known_asins(inventory)

    if not args.json:
        print("=== Audible Backup ===")
        print(f"Inventory: {inventory_path.name} ({len(known)} books)")
        print("Fetching Audible library (enriched metadata)...")

    # Fetch enriched library
    library = fetch_enriched_library(tmp_dir)
    new_books_raw = find_new_books(library, known)

    if not args.json:
        print(f"Audible library: {len(library)}")
        print(f"New books to download: {len(new_books_raw)}")
        for b in new_books_raw:
            authors = ", ".join(
                a.get("name", "") for a in (b.get("authors") or [])
            )
            print(f"  {b.get('asin')}  {b.get('title', '?')} — {authors}")

    if not new_books_raw:
        if args.json:
            print(json.dumps({
                "new_books": 0, "downloaded": 0, "failed": 0, "books": []
            }))
        else:
            print("Library up to date.")
        return

    # Map to inventory schema
    new_records = [map_to_inventory_schema(b) for b in new_books_raw]

    if args.dry_run:
        if args.json:
            print(json.dumps({
                "new_books": len(new_records),
                "dry_run": True,
                "books": new_records,
            }))
        else:
            print("\nDRY RUN — nothing downloaded.")
        return

    # Download and decrypt
    books_dir.mkdir(exist_ok=True)
    aax_dir.mkdir(exist_ok=True)
    art_dir.mkdir(exist_ok=True)
    download_dir.mkdir(exist_ok=True)

    downloaded = 0
    failed = 0
    skipped = 0
    results = []

    for record in new_records:
        # Fetch chapters before download (cheap API call)
        record["chapters"] = fetch_chapters(record["asin"])

        result = download_and_decrypt(
            record, download_dir, books_dir, aax_dir, art_dir
        )
        results.append(result)

        if result["status"] == "ok":
            downloaded += 1
            # Append to inventory and persist immediately so a crash mid-batch
            # doesn't lose successful downloads
            inventory.append({
                k: v for k, v in result.items()
                if k not in ("status", "error", "m4b_path")
            })
            save_inventory(inventory_path, inventory)
        elif result["status"] == "skipped":
            skipped += 1
        else:
            failed += 1

    # Cleanup empty download dir
    try:
        download_dir.rmdir()
    except OSError:
        pass

    if args.json:
        print(json.dumps({
            "new_books": len(new_records),
            "downloaded": downloaded,
            "skipped": skipped,
            "failed": failed,
            "books": results,
        }))
    else:
        print(f"\n=== Backup complete ===")
        print(f"Downloaded: {downloaded}")
        print(f"Skipped: {skipped}")
        print(f"Failed: {failed}")
        print(f"Inventory now has: {len(inventory)} books")


if __name__ == "__main__":
    main()
