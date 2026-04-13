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

# Audible API response groups for enriched library data
LIBRARY_RESPONSE_GROUPS = (
    "contributors,product_desc,product_extended_attrs,product_attrs,"
    "media,rating,series,categories"
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


def map_to_inventory_schema(api_book: dict, region: str = "us") -> dict:
    """Map enriched audible-cli API response to inventory JSON schema.

    Populates everything that maps cleanly. Leaves filename/files for the
    download step. Includes both legacy OpenAudible fields and new extras.
    """
    asin = api_book.get("asin", "")

    # Authors and narrators come as lists of {asin, name} dicts
    authors = api_book.get("authors") or []
    narrators = api_book.get("narrators") or []
    author_str = ", ".join(a.get("name", "") for a in authors if a.get("name"))
    narrator_str = ", ".join(n.get("name", "") for n in narrators if n.get("name"))

    # Categories come as nested ladders — flatten unique leaf names
    genres: list[str] = []
    for ladder in api_book.get("category_ladders") or []:
        for cat in ladder.get("ladder") or []:
            name = cat.get("name")
            if name and name not in genres:
                genres.append(name)
    genre_str = ", ".join(genres)

    # Rating is nested
    rating = api_book.get("rating") or {}
    overall = rating.get("overall_distribution") or {}
    rating_average = overall.get("average_rating", "")
    rating_count = overall.get("num_ratings", "") or rating.get("num_reviews", "")

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

    # Author link (Amazon page) — derive from first author's ASIN
    author_link = ""
    if authors and authors[0].get("asin"):
        author_link = f"https://www.amazon.com/author/{authors[0]['asin']}"

    # is_finished maps to read_status (OpenAudible convention: "" or "READ")
    read_status = "READ" if api_book.get("is_finished") else ""

    return {
        # --- Legacy OpenAudible fields ---
        "asin": asin,
        "product_id": asin,
        "title": api_book.get("title", ""),
        "title_short": api_book.get("title", "")[:200],
        "author": author_str,
        "author_link": author_link,
        "narrated_by": narrator_str,
        "publisher": api_book.get("publisher_name", ""),
        "summary": api_book.get("merchandising_summary", ""),
        "description": api_book.get("publisher_summary", ""),
        "copyright": api_book.get("copyright", ""),
        "language": api_book.get("language", ""),
        "region": region,
        "genre": genre_str,
        "rating_average": str(rating_average) if rating_average else "",
        "rating_count": rating_count if rating_count else "",
        "purchase_date": api_book.get("purchase_date", ""),
        "release_date": api_book.get("release_date", "") or api_book.get("issue_date", ""),
        "duration": duration_str,
        "seconds": runtime_sec,
        "abridged": api_book.get("format_type", "") != "unabridged",
        "image_url": (api_book.get("product_images") or {}).get("500", "")
                     or api_book.get("cover_url", ""),
        "info_link": api_book.get("product_page_url", ""),
        "download_link": "",
        "ayce": api_book.get("is_ayce", False),
        "read_status": read_status,
        "user_id": "",
        "key": asin,
        "filename": "",
        "files": [],

        # --- New extras (audible-cli has, OpenAudible didn't) ---
        "subtitle": api_book.get("subtitle", ""),
        "series_title": series_first.get("title", ""),
        "series_sequence": series_first.get("sequence", ""),
        "percent_complete": api_book.get("percent_complete", 0),
        "date_added": api_book.get("date_added", ""),
        "isbn": api_book.get("isbn", ""),
        "sku": api_book.get("sku", ""),
        "voice_description": api_book.get("voice_description", ""),
        "narration_accent": api_book.get("narration_accent", ""),
        "format_type": api_book.get("format_type", ""),
        "content_type": api_book.get("content_type", ""),
        "publication_datetime": api_book.get("publication_datetime", ""),
        "goodreads_ratings": api_book.get("goodreads_ratings", []),

        # chapters populated separately via fetch_chapters()
        "chapters": [],
    }


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
