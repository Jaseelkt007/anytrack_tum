"""Promote rows in data/active_watchlist.csv to tier='active' in Neo4j.

For each row:
  - Locate or create the Person (matching by display_name against M2 reference set;
    if not found, create a new augmentation Person with a deterministic gh:<handle>
    canonical_id).
  - Add a PlatformIdentity(platform='github', handle) and a HAS_IDENTITY edge.
  - Upsert the WATCHED_BY edge to the demo User with tier='active' and archetype.

Idempotent: re-running mutates only what changed.

Usage:
    python scripts/promote_active_watchlist.py
    python scripts/promote_active_watchlist.py --input data/active_watchlist.csv --user-id demo
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

# Same NAMESPACE used by load_investor_reference.py and scrapers.cypher.
NAMESPACE = uuid.UUID("8e1b3f2a-1c5d-4e7f-9a8b-2c3d4e5f6a7b")


def github_person_id(handle: str) -> str:
    return str(uuid.uuid5(NAMESPACE, f"gh:{handle.lower()}"))


# Try to locate an existing Person by display_name (case-insensitive). If found,
# return its canonical_id. Otherwise return None — caller creates an augmentation.
QUERY_FIND_PERSON_BY_NAME = """
MATCH (p:Person)
WHERE toLower(p.display_name) = toLower($display_name)
RETURN p.canonical_id AS canonical_id
LIMIT 1
"""

UPSERT_AUGMENTATION_PERSON = """
MERGE (p:Person {canonical_id: $canonical_id})
ON CREATE SET
    p.first_observed_at = datetime($now_iso),
    p.confidence_score  = 1.0
SET
    p.display_name     = $display_name,
    p.role_tags        = ['investor', 'angel_operator'],
    p.investor_type    = $archetype,
    p.last_observed_at = datetime($now_iso)
"""

UPSERT_GITHUB_IDENTITY = """
MATCH (p:Person {canonical_id: $canonical_id})
WITH p, toLower($github_handle) AS handle_lc
MERGE (i:PlatformIdentity {platform: 'github', handle: handle_lc})
ON CREATE SET
    i.handle_original   = $github_handle,
    i.profile_url       = $profile_url,
    i.verified_via      = 'manual',
    i.confidence        = 1.0,
    i.kind              = 'User',
    i.first_observed_at = datetime($now_iso)
SET i.kind = coalesce(i.kind, 'User')
MERGE (p)-[:HAS_IDENTITY]->(i)
"""

PROMOTE_TO_ACTIVE = """
MATCH (p:Person {canonical_id: $canonical_id})
MATCH (u:User {id: $user_id})
MERGE (p)-[r:WATCHED_BY]->(u)
ON CREATE SET r.added_at = datetime($now_iso)
SET
    r.tier      = 'active',
    r.archetype = $archetype,
    r.notes     = $notes
"""


def run(input_path: Path, user_id: str) -> int:
    try:
        from dotenv import load_dotenv
        from neo4j import GraphDatabase
    except ImportError as exc:
        print(f"ERROR: missing dependency ({exc.name}). Run: pip install -r requirements.txt", file=sys.stderr)
        return 2

    load_dotenv(ROOT / ".env")
    uri = os.getenv("NEO4J_URI")
    user = os.getenv("NEO4J_USER")
    password = os.getenv("NEO4J_PASSWORD")
    if not all([uri, user, password]):
        print("ERROR: NEO4J_URI / NEO4J_USER / NEO4J_PASSWORD missing from .env", file=sys.stderr)
        return 2

    if not input_path.exists():
        print(f"ERROR: not found: {input_path}", file=sys.stderr)
        return 2

    now_iso = datetime.now(timezone.utc).isoformat()
    rows = list(csv.DictReader(open(input_path, encoding="utf-8")))
    if not rows:
        print("ERROR: input CSV is empty", file=sys.stderr)
        return 2

    matched_existing = 0
    created_new = 0

    driver = GraphDatabase.driver(uri, auth=(user, password))
    with driver.session() as session:
        # Ensure the demo User exists (idempotent).
        session.run("MERGE (u:User {id:$id}) ON CREATE SET u.created_at = datetime($now)",
                    id=user_id, now=now_iso).consume()

        for row in rows:
            display_name = row["display_name"].strip()
            handle = row["github_handle"].strip()
            archetype = row.get("archetype", "").strip() or "angel_operator"
            notes = row.get("rationale", "").strip()
            if not display_name or not handle:
                print(f"  SKIP malformed row: {row}", file=sys.stderr)
                continue

            # Try to find an existing Person from M2 reference set.
            result = session.run(QUERY_FIND_PERSON_BY_NAME, display_name=display_name).single()
            if result:
                canonical_id = result["canonical_id"]
                matched_existing += 1
                tag = "matched"
            else:
                canonical_id = github_person_id(handle)
                session.run(UPSERT_AUGMENTATION_PERSON,
                            canonical_id=canonical_id,
                            display_name=display_name,
                            archetype=archetype,
                            now_iso=now_iso).consume()
                created_new += 1
                tag = "augmented"

            # Add github identity (idempotent).
            session.run(UPSERT_GITHUB_IDENTITY,
                        canonical_id=canonical_id,
                        github_handle=handle,
                        profile_url=f"https://github.com/{handle}",
                        now_iso=now_iso).consume()

            # Promote to tier='active'.
            session.run(PROMOTE_TO_ACTIVE,
                        canonical_id=canonical_id,
                        user_id=user_id,
                        archetype=archetype,
                        notes=notes,
                        now_iso=now_iso).consume()

            print(f"  {tag:<10} {display_name:<30} -> github.com/{handle}")

    driver.close()

    print()
    print(f"Promoted {len(rows)} rows. matched_existing={matched_existing}, created_new={created_new}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=ROOT / "data" / "active_watchlist.csv")
    parser.add_argument("--user-id", default="demo")
    args = parser.parse_args()
    return run(args.input, args.user_id)


if __name__ == "__main__":
    sys.exit(main())
