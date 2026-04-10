#!/usr/bin/env python3
"""Audible audiobook backup.

Reads OpenAudible's books.json as existing inventory, uses audible-cli
to find new purchases, downloads and decrypts them to M4B.

Usage:
    python3 backup.py [--dry-run] [--json]

Mounts expected:
    /library     — audiobook library root (OpenAudible structure)
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


def find_inventory(library_dir: Path) -> Path | None:
    """Find OpenAudible books.json (Google Drive may rename it)."""
    for name in ["books.json", "books (1).json"]:
        p = library_dir / name
        if p.exists():
            return p
    return None


def get_known_asins(inventory_path: Path) -> set[str]:
    """Extract ASINs from OpenAudible inventory."""
    books = json.loads(inventory_path.read_text())
    return {b["asin"] for b in books if b.get("asin")}


def export_audible_library(tmp_dir: Path) -> list[dict]:
    """Export current Audible library via audible-cli."""
    out = tmp_dir / "library.json"
    subprocess.run(
        ["audible", "library", "export", "-f", "json", "-o", str(out)],
        check=True,
        capture_output=True,
    )
    return json.loads(out.read_text())


def find_new_books(
    library: list[dict], known_asins: set[str]
) -> list[dict]:
    """Find books in Audible library not in OpenAudible inventory.

    Returns full records so the caller can update books-library.csv.
    """
    new = []
    for book in library:
        asin = book.get("asin", "")
        if asin and asin not in known_asins:
            new.append(book)
    return new


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
    """Download and decrypt a single book. Returns book dict with status + m4b_path."""
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
    if cover_files:
        (art_dir / f"{safe_title}.jpg").write_bytes(cover_files[0].read_bytes())

    # Archive raw AAX
    audio_file.rename(aax_dir / audio_file.name)

    # Cleanup temp files for this book
    for f in download_dir.glob(f"{asin}*"):
        f.unlink()

    return {**book, "status": "ok", "m4b_path": str(output_m4b)}


def main():
    parser = argparse.ArgumentParser(description="Audible audiobook backup")
    parser.add_argument("--dry-run", action="store_true", help="List new books without downloading")
    parser.add_argument("--json", action="store_true", help="Output results as JSON")
    args = parser.parse_args()

    library_dir = Path(os.environ.get("AUDIOBOOK_DIR", "/library"))
    tmp_dir = Path("/tmp/audible-backup")
    tmp_dir.mkdir(exist_ok=True)

    # Validate
    inventory = find_inventory(library_dir)
    if not inventory:
        print(json.dumps({"error": "OpenAudible inventory not found"}) if args.json
              else "ERROR: OpenAudible inventory not found")
        sys.exit(1)

    books_dir = library_dir / "books"
    aax_dir = library_dir / "aax"
    art_dir = library_dir / "art"
    download_dir = library_dir / "tmp_download"

    # Get known ASINs
    known = get_known_asins(inventory)

    # Fetch Audible library
    if not args.json:
        print(f"=== Audible Backup ===")
        print(f"Known books (OpenAudible): {len(known)}")
        print("Fetching Audible library...")

    library = export_audible_library(tmp_dir)
    new_books = find_new_books(library, known)

    if not args.json:
        print(f"Audible library: {len(library)}")
        print(f"New books to download: {len(new_books)}")
        for b in new_books:
            print(f"  {b.get('asin')}  {b.get('title', '?')} — {b.get('authors', '?')}")

    if not new_books:
        if args.json:
            print(json.dumps({"new_books": 0, "downloaded": 0, "failed": 0, "books": []}))
        else:
            print("Library up to date.")
        return

    if args.dry_run:
        if args.json:
            print(json.dumps({"new_books": len(new_books), "dry_run": True, "books": new_books}))
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
    results = []

    for book in new_books:
        result = download_and_decrypt(book, download_dir, books_dir, aax_dir, art_dir)
        results.append(result)
        if result["status"] == "ok":
            downloaded += 1
        else:
            failed += 1

    download_dir.rmdir() if not any(download_dir.iterdir()) else None

    if args.json:
        print(json.dumps({
            "new_books": len(new_books),
            "downloaded": downloaded,
            "failed": failed,
            "books": results,
        }))
    else:
        print(f"\n=== Backup complete ===")
        print(f"Downloaded: {downloaded}")
        print(f"Failed: {failed}")


if __name__ == "__main__":
    main()
