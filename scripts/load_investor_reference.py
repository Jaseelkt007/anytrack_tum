"""Load investors_clean.csv into Neo4j as reference Person records.

For each row, MERGEs:
  - User {id: 'demo'}                                    (the single Phase 1 demo user)
  - Person {canonical_id} with full investor metadata
  - PlatformIdentity {platform:'linkedin', handle: <slug>}    if linkedin_slug present
  - PlatformIdentity {platform:'twitter',  handle: <handle>}  if twitter_handle present
  - (Person)-[:HAS_IDENTITY]->(PlatformIdentity)         per identity
  - (Person)-[:WATCHED_BY {tier:'reference', added_at}]->(User {id:'demo'})

Idempotency:
  - canonical_id is generated deterministically (UUID5 from a stable key) so
    re-running yields the same nodes.
  - All writes use MERGE; uniqueness constraints from M1 enforce no duplicates.

Usage:
    python scripts/load_investor_reference.py
        [--input data/investors_clean.csv]
        [--user-id demo]
        [--batch-size 100]
        [--dry-run]            # print Cypher params, do not connect to Neo4j
"""

from __future__ import annotations

import argparse
import csv
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

ROOT = Path(__file__).resolve().parent.parent

# A stable namespace for our deterministic UUID5s. Do not change — would invalidate IDs.
NAMESPACE = uuid.UUID("8e1b3f2a-1c5d-4e7f-9a8b-2c3d4e5f6a7b")


def stable_key(row: dict[str, str]) -> str:
    """Build a stable identity key for canonical_id generation.

    Priority order: linkedin_slug -> twitter_handle -> normalized display_name.
    The chosen key is prefixed so collisions across columns aren't possible
    (e.g., a person whose linkedin slug equals someone else's twitter handle
    still gets a different id).
    """
    if row.get("linkedin_slug"):
        return f"li:{row['linkedin_slug'].lower()}"
    if row.get("twitter_handle"):
        return f"tw:{row['twitter_handle'].lower()}"
    name_norm = " ".join(row["display_name"].lower().split())
    typ = row.get("investor_type", "").lower()
    return f"name:{name_norm}|{typ}"


def canonical_id_for(row: dict[str, str]) -> str:
    return str(uuid.uuid5(NAMESPACE, stable_key(row)))


def role_tags_for(row: dict[str, str]) -> list[str]:
    tags = ["investor"]
    if row.get("investor_type") == "Angel":
        tags.append("angel")
    return tags


def split_pipe(value: str) -> list[str]:
    if not value:
        return []
    return [part for part in value.split("|") if part]


def build_person_params(row: dict[str, str], now_iso: str) -> dict:
    """Pure function — turns one CSV row into the params dict the Cypher MERGE expects.

    Kept separate from any Neo4j calls so it is unit-testable.
    """
    return {
        "canonical_id": canonical_id_for(row),
        "display_name": row["display_name"],
        "investor_type": row.get("investor_type") or None,
        "country": row.get("country") or None,
        "sector_tags": split_pipe(row.get("sector_tags", "")),
        "stage_tags": split_pipe(row.get("stage_tags", "")),
        "role_tags": role_tags_for(row),
        "now_iso": now_iso,
        "linkedin_slug": row.get("linkedin_slug") or None,
        "linkedin_url": row.get("linkedin_url") or None,
        "twitter_handle": row.get("twitter_handle") or None,
        "twitter_url": (
            f"https://twitter.com/{row['twitter_handle']}" if row.get("twitter_handle") else None
        ),
    }


# --- Cypher queries -----------------------------------------------------------
# Each query MERGEs on a uniqueness-constrained key, so re-runs are idempotent.

CYPHER_USER = """
MERGE (u:User {id: $user_id})
ON CREATE SET u.created_at = datetime($now_iso)
RETURN u.id AS id
"""

CYPHER_PERSON = """
MERGE (p:Person {canonical_id: $canonical_id})
ON CREATE SET
    p.first_observed_at = datetime($now_iso),
    p.confidence_score  = 1.0
SET
    p.display_name   = $display_name,
    p.investor_type  = $investor_type,
    p.country        = $country,
    p.sector_tags    = $sector_tags,
    p.stage_tags     = $stage_tags,
    p.role_tags      = $role_tags,
    p.last_observed_at = datetime($now_iso)
RETURN p.canonical_id AS id
"""

# linkedin: MERGE on (platform, handle) which is uniqueness-constrained.
CYPHER_LINKEDIN_IDENTITY = """
MATCH (p:Person {canonical_id: $canonical_id})
MERGE (i:PlatformIdentity {platform: 'linkedin', handle: $linkedin_slug})
ON CREATE SET
    i.profile_url      = $linkedin_url,
    i.verified_via     = 'csv_import',
    i.confidence       = 0.9,
    i.first_observed_at = datetime($now_iso)
MERGE (p)-[:HAS_IDENTITY]->(i)
"""

CYPHER_TWITTER_IDENTITY = """
MATCH (p:Person {canonical_id: $canonical_id})
MERGE (i:PlatformIdentity {platform: 'twitter', handle: $twitter_handle})
ON CREATE SET
    i.profile_url      = $twitter_url,
    i.verified_via     = 'csv_import',
    i.confidence       = 0.9,
    i.first_observed_at = datetime($now_iso)
MERGE (p)-[:HAS_IDENTITY]->(i)
"""

# WatchlistMembership = WATCHED_BY edge with properties.
CYPHER_WATCHED_BY = """
MATCH (p:Person {canonical_id: $canonical_id})
MATCH (u:User {id: $user_id})
MERGE (p)-[r:WATCHED_BY]->(u)
ON CREATE SET
    r.added_at = datetime($now_iso),
    r.tier     = 'reference'
SET
    r.archetype = $investor_type
"""


def iter_rows(path: Path) -> Iterable[dict[str, str]]:
    with open(path, encoding="utf-8") as f:
        yield from csv.DictReader(f)


def execute_for_row(session, row: dict[str, str], user_id: str, now_iso: str) -> None:
    params = build_person_params(row, now_iso)
    params["user_id"] = user_id

    session.run(CYPHER_PERSON, **params)
    if params["linkedin_slug"]:
        session.run(CYPHER_LINKEDIN_IDENTITY, **params)
    if params["twitter_handle"]:
        session.run(CYPHER_TWITTER_IDENTITY, **params)
    session.run(CYPHER_WATCHED_BY, **params)


def run_dry(input_path: Path, user_id: str, sample: int = 3) -> int:
    """Print what would be sent for the first `sample` rows, without connecting."""
    now_iso = datetime.now(timezone.utc).isoformat()
    rows = list(iter_rows(input_path))
    print(f"Dry run: would process {len(rows)} rows. Showing first {sample}:\n")
    for r in rows[:sample]:
        params = build_person_params(r, now_iso)
        params["user_id"] = user_id
        print(f"  {r['display_name']!r}")
        print(f"    canonical_id = {params['canonical_id']}")
        print(f"    role_tags    = {params['role_tags']}")
        print(f"    li_slug      = {params['linkedin_slug']!r}")
        print(f"    tw_handle    = {params['twitter_handle']!r}")
        print()
    return 0


def run(input_path: Path, user_id: str, batch_size: int) -> int:
    try:
        from dotenv import load_dotenv
        from neo4j import GraphDatabase
        from neo4j.exceptions import AuthError, ServiceUnavailable
    except ImportError as exc:
        print(f"ERROR: missing dependency ({exc.name}). Run: pip install -r requirements.txt", file=sys.stderr)
        return 2

    load_dotenv(ROOT / ".env")
    import os
    uri = os.getenv("NEO4J_URI")
    user = os.getenv("NEO4J_USER")
    password = os.getenv("NEO4J_PASSWORD")
    if not all([uri, user, password]):
        print("ERROR: NEO4J_URI / NEO4J_USER / NEO4J_PASSWORD missing from .env", file=sys.stderr)
        return 2

    if not input_path.exists():
        print(f"ERROR: input file not found: {input_path}. Run clean_investor_csv.py first.", file=sys.stderr)
        return 2

    now_iso = datetime.now(timezone.utc).isoformat()

    try:
        driver = GraphDatabase.driver(uri, auth=(user, password))
        with driver.session() as session:
            session.run(CYPHER_USER, user_id=user_id, now_iso=now_iso).consume()

            rows = list(iter_rows(input_path))
            total = len(rows)
            print(f"Loading {total} reference investors into Neo4j...")
            for i, row in enumerate(rows, start=1):
                execute_for_row(session, row, user_id, now_iso)
                if i % batch_size == 0 or i == total:
                    print(f"  {i:>4}/{total}")
        driver.close()
    except AuthError as exc:
        print(f"ERROR: Neo4j auth failed: {exc}", file=sys.stderr)
        return 1
    except ServiceUnavailable as exc:
        print(f"ERROR: Neo4j unreachable: {exc}", file=sys.stderr)
        return 1

    print("\nLoad complete. Verify with the queries in data/README.md.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=ROOT / "data" / "investors_clean.csv")
    parser.add_argument("--user-id", default="demo")
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--dry-run", action="store_true", help="Print sample params, do not connect")
    args = parser.parse_args()

    if args.dry_run:
        return run_dry(args.input, args.user_id)
    return run(args.input, args.user_id, args.batch_size)


if __name__ == "__main__":
    sys.exit(main())
