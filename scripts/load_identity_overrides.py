"""Load data/identity_overrides.csv — hand-curated demo founders/targets.

Each row creates:
  - Person with role_tags including 'founder_candidate' and the row's `role`
  - Optional GitHub / LinkedIn / Twitter PlatformIdentities
  - HAS_IDENTITY edges

These are NOT in the active watchlist (they are observation targets, not watchers).
The deterministic canonical_id uses the same `gh:<handle>` scheme as the scraper, so
if the scraper later encounters them as followed users, the same Person is reused.

Usage:
    python scripts/load_identity_overrides.py
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

NAMESPACE = uuid.UUID("8e1b3f2a-1c5d-4e7f-9a8b-2c3d4e5f6a7b")


def github_person_id(handle: str) -> str:
    return str(uuid.uuid5(NAMESPACE, f"gh:{handle.lower()}"))


UPSERT_FOUNDER_PERSON = """
MERGE (p:Person {canonical_id: $canonical_id})
ON CREATE SET p.first_observed_at = datetime($now_iso), p.confidence_score = 1.0
SET
    p.display_name     = $display_name,
    p.role_tags        = $role_tags,
    p.last_observed_at = datetime($now_iso)
"""

UPSERT_IDENTITY = """
MATCH (p:Person {canonical_id: $canonical_id})
MERGE (i:PlatformIdentity {platform: $platform, handle: $handle})
ON CREATE SET
    i.profile_url       = $profile_url,
    i.verified_via      = 'manual',
    i.confidence        = 1.0,
    i.first_observed_at = datetime($now_iso)
MERGE (p)-[:HAS_IDENTITY]->(i)
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=ROOT / "data" / "identity_overrides.csv")
    args = parser.parse_args()

    from dotenv import load_dotenv
    from neo4j import GraphDatabase
    load_dotenv(ROOT / ".env")

    if not args.input.exists():
        print(f"ERROR: {args.input} not found", file=sys.stderr)
        return 2

    rows = list(csv.DictReader(open(args.input, encoding="utf-8")))
    if not rows:
        print("WARN: empty CSV", file=sys.stderr)
        return 1

    now_iso = datetime.now(timezone.utc).isoformat()
    driver = GraphDatabase.driver(
        os.environ["NEO4J_URI"],
        auth=(os.environ["NEO4J_USER"], os.environ["NEO4J_PASSWORD"]),
    )

    with driver.session() as session:
        for row in rows:
            handle = row["github_handle"].strip()
            cid = github_person_id(handle)
            role = row.get("role", "").strip() or "founder_candidate"
            role_tags = ["founder_candidate"]
            if role and role not in role_tags:
                role_tags.append(role)

            session.run(UPSERT_FOUNDER_PERSON,
                        canonical_id=cid,
                        display_name=row["display_name"].strip(),
                        role_tags=role_tags,
                        now_iso=now_iso).consume()

            if handle:
                session.run(UPSERT_IDENTITY,
                            canonical_id=cid,
                            platform="github", handle=handle,
                            profile_url=f"https://github.com/{handle}",
                            now_iso=now_iso).consume()
            li = (row.get("linkedin_slug") or "").strip()
            if li:
                session.run(UPSERT_IDENTITY,
                            canonical_id=cid,
                            platform="linkedin", handle=li,
                            profile_url=f"https://www.linkedin.com/in/{li}/",
                            now_iso=now_iso).consume()
            tw = (row.get("twitter_handle") or "").strip()
            if tw:
                session.run(UPSERT_IDENTITY,
                            canonical_id=cid,
                            platform="twitter", handle=tw,
                            profile_url=f"https://twitter.com/{tw}",
                            now_iso=now_iso).consume()

            print(f"  loaded {row['display_name']:<25} -> github.com/{handle} (id={cid[:8]})")

    driver.close()
    print(f"\nLoaded {len(rows)} identity override(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
