"""Clean and normalize investor_list_1000.csv into investors_clean.csv.

Source columns: Name, Typ, land, Branche, Stage, Linkedin, Twitter
Output columns: display_name, investor_type, country, sector_tags, stage_tags,
                linkedin_url, linkedin_slug, twitter_handle, source_row

Normalization rules:
  - Strip whitespace from every field.
  - country: empty string -> None (rendered as empty in output CSV).
  - sector_tags / stage_tags: split on comma, trim each, drop empties.
  - linkedin_url: kept as-is when present; linkedin_slug extracted from /in/<slug>/.
  - twitter_handle: extracted from URL, lowercased, no '@'. Handles unusual
    /status/ paths by taking the segment immediately after the host.
  - source_row: 1-indexed row number in the source file, for debuggability.

Usage:
    python scripts/clean_investor_csv.py
        [--input investor_list_1000.csv]
        [--output data/investors_clean.csv]
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent

OUTPUT_COLUMNS = [
    "display_name",
    "investor_type",
    "country",
    "sector_tags",
    "stage_tags",
    "linkedin_url",
    "linkedin_slug",
    "twitter_handle",
    "source_row",
]

VALID_INVESTOR_TYPES = {
    "Angel",
    "VC - Small fund",
    "VC - Medium-Sized Fund",
    "VC - Big fund",
}

LINKEDIN_SLUG_RE = re.compile(r"linkedin\.com/in/([A-Za-z0-9\-_%]+)/?", re.IGNORECASE)
TWITTER_HOST_RE = re.compile(r"https?://(?:www\.)?(?:twitter|x)\.com/", re.IGNORECASE)
HANDLE_RE = re.compile(r"^[A-Za-z0-9_]{1,20}$")


def split_tags(raw: str) -> list[str]:
    if not raw:
        return []
    return [tag.strip() for tag in raw.split(",") if tag.strip()]


def extract_linkedin_slug(url: str) -> Optional[str]:
    if not url:
        return None
    m = LINKEDIN_SLUG_RE.search(url)
    if not m:
        return None
    return m.group(1).strip("/").lower()


def extract_twitter_handle(url: str) -> Optional[str]:
    """Extract a bare twitter handle from a URL.

    Handles standard `twitter.com/handle`, `x.com/handle`, and unusual paths like
    `twitter.com/handle/status/12345?s=20` by taking the first path segment.
    Returns None if the URL doesn't parse to a valid-looking handle.
    """
    if not url:
        return None
    url = url.strip()
    m = TWITTER_HOST_RE.match(url)
    if not m:
        # Maybe it's already a bare handle.
        bare = url.lstrip("@").rstrip("/")
        return bare.lower() if HANDLE_RE.match(bare) else None
    rest = url[m.end():]
    first_segment = rest.split("/", 1)[0].split("?", 1)[0]
    handle = first_segment.lstrip("@").lower()
    return handle if HANDLE_RE.match(handle) else None


def clean_row(row: dict[str, str], source_row: int) -> Optional[dict[str, str]]:
    """Convert a raw CSV row into a normalized output row. Returns None if the row
    is too malformed to keep."""
    name = row.get("Name", "").strip()
    typ = row.get("Typ", "").strip()
    if not name or not typ:
        return None
    if typ not in VALID_INVESTOR_TYPES:
        # Phase 1 invariant: every investor must fall into one of the four types.
        # We don't silently relabel — surface the row instead.
        print(f"WARN: row {source_row} has unexpected Typ {typ!r}; keeping as-is", file=sys.stderr)

    country = row.get("land", "").strip()
    sector_tags = split_tags(row.get("Branche", ""))
    stage_tags = split_tags(row.get("Stage", ""))
    linkedin_url = row.get("Linkedin", "").strip()
    twitter_raw = row.get("Twitter", "").strip()

    return {
        "display_name": name,
        "investor_type": typ,
        "country": country,
        "sector_tags": "|".join(sector_tags),     # pipe-delimited for CSV roundtrip safety
        "stage_tags": "|".join(stage_tags),
        "linkedin_url": linkedin_url,
        "linkedin_slug": extract_linkedin_slug(linkedin_url) or "",
        "twitter_handle": extract_twitter_handle(twitter_raw) or "",
        "source_row": str(source_row),
    }


def run(input_path: Path, output_path: Path) -> int:
    if not input_path.exists():
        print(f"ERROR: input file not found: {input_path}", file=sys.stderr)
        return 2

    output_path.parent.mkdir(parents=True, exist_ok=True)

    rows_in = 0
    rows_out = 0
    skipped = 0
    type_counts: dict[str, int] = {}
    li_count = 0
    tw_count = 0

    with open(input_path, encoding="utf-8") as f_in, open(output_path, "w", encoding="utf-8", newline="") as f_out:
        reader = csv.DictReader(f_in)
        writer = csv.DictWriter(f_out, fieldnames=OUTPUT_COLUMNS)
        writer.writeheader()

        for i, raw in enumerate(reader, start=2):  # start=2 because header is row 1
            rows_in += 1
            cleaned = clean_row(raw, source_row=i)
            if cleaned is None:
                skipped += 1
                continue
            writer.writerow(cleaned)
            rows_out += 1
            type_counts[cleaned["investor_type"]] = type_counts.get(cleaned["investor_type"], 0) + 1
            if cleaned["linkedin_slug"]:
                li_count += 1
            if cleaned["twitter_handle"]:
                tw_count += 1

    print(f"Read {rows_in} rows from {input_path}")
    print(f"Wrote {rows_out} rows to {output_path}")
    if skipped:
        print(f"Skipped {skipped} rows (missing name or type)")
    print()
    print("Counts by investor_type:")
    for t, c in sorted(type_counts.items(), key=lambda x: -x[1]):
        print(f"  {c:4} {t}")
    print()
    print(f"LinkedIn slugs extracted: {li_count}")
    print(f"Twitter handles extracted: {tw_count}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=ROOT / "investor_list_1000.csv")
    parser.add_argument("--output", type=Path, default=ROOT / "data" / "investors_clean.csv")
    args = parser.parse_args()
    return run(args.input, args.output)


if __name__ == "__main__":
    sys.exit(main())
