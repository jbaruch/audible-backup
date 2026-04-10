# audible-backup

Dockerized Audible audiobook backup. Downloads new purchases, decrypts to M4B with chapters. Migrates from an existing OpenAudible library without re-downloading.

## What it does

1. Reads your existing OpenAudible `books.json` for known ASINs
2. Fetches your current Audible library via [audible-cli](https://github.com/mkb79/audible-cli)
3. Finds new purchases not in the inventory
4. Downloads AAX/AAXC files with vouchers and cover art
5. Decrypts to M4B (chapters preserved) using ffmpeg
6. Archives raw AAX files separately

## Quick start

```bash
# Build
docker build -t audible-backup .

# One-time auth (interactive — opens browser for Audible OAuth)
docker run -it -v ~/.audible:/root/.audible audible-backup \
  bash -c "audible quickstart"

# Dry run — see what's new without downloading
docker run --rm \
  -v ~/.audible:/root/.audible:ro \
  -v /path/to/audiobooks:/library \
  audible-backup --dry-run

# Full backup
docker run --rm \
  -v ~/.audible:/root/.audible:ro \
  -v /path/to/audiobooks:/library \
  audible-backup
```

## Library structure

The container expects an OpenAudible-compatible directory at `/library`:

```
/library/
  books.json          # OpenAudible inventory (read-only, used for ASIN diffing)
  books/              # Decoded audiobooks (M4B/MP3)
  aax/                # Raw encrypted AAX files (archived after decrypt)
  art/                # Cover art
```

If you don't have an existing OpenAudible library, create a minimal one:

```bash
mkdir -p /path/to/audiobooks/books
echo '[]' > /path/to/audiobooks/books.json
```

## Options

| Flag | Description |
|------|-------------|
| `--dry-run` | List new books without downloading |
| `--json` | Output results as JSON (for automation) |

## JSON output

With `--json`, the output is machine-readable:

```json
{
  "new_books": 5,
  "downloaded": 5,
  "failed": 0,
  "books": [
    {"asin": "B079LRSMNN", "title": "Galaxy's Edge", "authors": "Jason Anspach", "status": "ok"}
  ]
}
```

## Scheduling

Run weekly via cron, systemd timer, or any scheduler:

```bash
0 3 * * 0  docker run --rm -v ~/.audible:/root/.audible:ro -v /mnt/audiobooks:/library audible-backup --json >> /var/log/audible-backup.log 2>&1
```

## Requirements

- Docker
- An Audible account (one-time interactive auth)
- An existing audiobook directory (OpenAudible format, or empty `books.json`)
