"""One-shot cleanup for the dedupe + org-noise fixes.

Run AFTER deploying the writer-side fixes (lowercased handles in
scrapers/cypher.py, org skip in scrapers/pipeline.py) so the bad state
doesn't get re-created mid-run.

Stages (each is idempotent and re-runnable):

  1. Consolidate case-variant PlatformIdentity nodes per (Person, platform).
     Only touches :HAS_IDENTITY edges and :PlatformIdentity nodes. Never
     deletes FOLLOWS_ON_GITHUB / STARRED_REPO / WATCHED_BY edges.

  2. Drop ConvergenceEvent nodes whose target is now an active watcher (these
     are the stale events that were keeping watchers in /api/founders).

  3. Optional: backfill Person.entity_type from PlatformIdentity.kind so the
     entity_type='User' query filters know what to do with pre-fix data.

  4. Optional: hard-prune Person nodes that are confirmed orgs/bots, never
     active watchers, and have no other-platform identity. Skip with
     --no-prune-orgs if you want soft-tag-only.

Usage:
    python scripts/cleanup_dupes_and_orgs.py --user-id demo
    python scripts/cleanup_dupes_and_orgs.py --user-id demo --no-prune-orgs
    python scripts/cleanup_dupes_and_orgs.py --dry-run

Dry-run prints counts but performs no writes.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


# --- Stage 1 ---------------------------------------------------------------

# 1a. For every (Person, platform) with multiple PlatformIdentity nodes,
# pick the lowercased one as the keeper. Re-point :HAS_IDENTITY edges from
# any other Person that happened to attach to a duplicate. Delete the dups.
CONSOLIDATE_CASE_VARIANT_PIS = """
MATCH (p:Person)-[:HAS_IDENTITY]->(i:PlatformIdentity)
WITH p, i.platform AS platform, collect(DISTINCT i) AS ids
WHERE size(ids) > 1
WITH p, platform, ids,
     [x IN ids WHERE x.handle = toLower(x.handle)] AS lowers
WITH p, platform, ids,
     CASE WHEN size(lowers) > 0 THEN lowers[0] ELSE ids[0] END AS keeper
UNWIND ids AS dup
WITH p, keeper, dup
WHERE dup <> keeper
OPTIONAL MATCH (other:Person)-[h:HAS_IDENTITY]->(dup)
WHERE other <> p
FOREACH (_ IN CASE WHEN other IS NULL THEN [] ELSE [1] END |
  MERGE (other)-[:HAS_IDENTITY]->(keeper)
)
DELETE h
WITH dup
DETACH DELETE dup
"""

# 1b. After consolidation, normalize any keeper whose handle still has uppercase.
NORMALIZE_KEEPER_CASE = """
MATCH (p:Person)-[h:HAS_IDENTITY]->(i:PlatformIdentity)
WHERE i.handle <> toLower(i.handle)
MERGE (j:PlatformIdentity {platform: i.platform, handle: toLower(i.handle)})
  ON CREATE SET
    j.handle_original   = coalesce(i.handle_original, i.handle),
    j.profile_url       = i.profile_url,
    j.verified_via      = i.verified_via,
    j.confidence        = i.confidence,
    j.kind              = i.kind,
    j.first_observed_at = i.first_observed_at
MERGE (p)-[:HAS_IDENTITY]->(j)
DELETE h
WITH i
WHERE NOT (()-[:HAS_IDENTITY]->(i))
DETACH DELETE i
"""


# --- Stage 2 ---------------------------------------------------------------

DROP_STALE_FOUNDER_EVENTS = """
MATCH (c:ConvergenceEvent {user_id: $user_id})-[:ABOUT]->(p:Person)
WHERE (p)-[:WATCHED_BY {tier: 'active'}]->(:User {id: $user_id})
DETACH DELETE c
RETURN count(c) AS dropped
"""


# --- Stage 3 ---------------------------------------------------------------

BACKFILL_ENTITY_TYPE = """
MATCH (p:Person)
WHERE p.entity_type IS NULL
OPTIONAL MATCH (p)-[:HAS_IDENTITY]->(i:PlatformIdentity {platform: 'github'})
WITH p, head(collect(DISTINCT i.kind)) AS gh_kind
SET p.entity_type = coalesce(gh_kind, 'User')
RETURN count(p) AS updated
"""


# --- Stage 4 ---------------------------------------------------------------

# Hard-prune Person nodes that are: confirmed Organization or Bot on github,
# never an investor (no WATCHED_BY edge of any tier), and have no LinkedIn /
# Twitter identity attached. The ConvergenceEvent ABOUT them is also removed.
PRUNE_ORG_NOISE = """
MATCH (p:Person)-[:HAS_IDENTITY]->(i:PlatformIdentity {platform: 'github'})
WHERE coalesce(i.kind, 'User') IN ['Organization', 'Bot']
  AND NOT (p)-[:WATCHED_BY]->(:User)
  AND NOT (p)-[:HAS_IDENTITY]->(:PlatformIdentity {platform: 'linkedin'})
  AND NOT (p)-[:HAS_IDENTITY]->(:PlatformIdentity {platform: 'twitter'})
WITH p, collect(DISTINCT i) AS gh_ids
OPTIONAL MATCH (c:ConvergenceEvent)-[:ABOUT]->(p)
WITH p, gh_ids, collect(DISTINCT c) AS events
FOREACH (ev IN events | DETACH DELETE ev)
FOREACH (gi IN gh_ids | DETACH DELETE gi)
DETACH DELETE p
"""

# Helper: list current likely-org Person nodes so the operator can review
# before or after pruning.
LIST_LIKELY_ORG_NOISE = """
MATCH (p:Person)-[:HAS_IDENTITY]->(i:PlatformIdentity {platform: 'github'})
WHERE coalesce(i.kind, 'User') IN ['Organization', 'Bot']
  AND NOT (p)-[:WATCHED_BY]->(:User)
RETURN p.canonical_id AS id, p.display_name AS name, i.handle AS gh_handle, i.kind AS kind
ORDER BY p.display_name
LIMIT 200
"""


# --- Sanity report ---------------------------------------------------------

REPORT_DUP_PIS = """
MATCH (p:Person)-[:HAS_IDENTITY]->(i:PlatformIdentity)
WITH p, i.platform AS platform, count(i) AS n
WHERE n > 1
RETURN p.canonical_id AS id, p.display_name AS name, platform, n
ORDER BY n DESC
LIMIT 50
"""


# --- Driver ----------------------------------------------------------------

def run(user_id: str, dry_run: bool, prune_orgs: bool) -> int:
    try:
        from dotenv import load_dotenv
        from neo4j import GraphDatabase
    except ImportError as exc:
        print(f"ERROR: missing dependency ({exc.name})", file=sys.stderr)
        return 2

    load_dotenv(ROOT / ".env")
    uri = os.getenv("NEO4J_URI")
    user = os.getenv("NEO4J_USER")
    password = os.getenv("NEO4J_PASSWORD")
    if not all([uri, user, password]):
        print("ERROR: NEO4J_URI / NEO4J_USER / NEO4J_PASSWORD missing from .env", file=sys.stderr)
        return 2

    driver = GraphDatabase.driver(uri, auth=(user, password))
    try:
        with driver.session() as session:
            # ---- Pre-report
            print("=== Pre-cleanup: Persons with duplicate PIs per platform ===")
            for r in session.run(REPORT_DUP_PIS):
                print(f"  {r['name']:<40} {r['platform']:<10} n={r['n']}")

            print("\n=== Likely org/bot noise (top 200) ===")
            n_orgs = 0
            for r in session.run(LIST_LIKELY_ORG_NOISE):
                n_orgs += 1
                print(f"  {r['gh_handle']:<30} kind={r['kind']:<13} {r['name']}")
            print(f"  total: {n_orgs}")

            if dry_run:
                print("\n[dry-run] no writes performed.")
                return 0

            # ---- Stage 1
            print("\n=== Stage 1a: consolidate case-variant PlatformIdentity nodes ===")
            session.run(CONSOLIDATE_CASE_VARIANT_PIS).consume()
            print("=== Stage 1b: re-point any remaining mixed-case PIs ===")
            session.run(NORMALIZE_KEEPER_CASE).consume()

            # ---- Stage 2
            print("\n=== Stage 2: drop stale ConvergenceEvents for active watchers ===")
            res = session.run(DROP_STALE_FOUNDER_EVENTS, user_id=user_id).single()
            print(f"  dropped: {res['dropped'] if res else 0}")

            # ---- Stage 3
            print("\n=== Stage 3: backfill Person.entity_type ===")
            res = session.run(BACKFILL_ENTITY_TYPE).single()
            print(f"  backfilled entity_type on: {res['updated'] if res else 0} Persons")

            # ---- Stage 4
            if prune_orgs:
                print("\n=== Stage 4: hard-prune confirmed orgs/bots ===")
                session.run(PRUNE_ORG_NOISE).consume()
                print("  done.")
            else:
                print("\n=== Stage 4: skipped (--no-prune-orgs). Soft-tag only. ===")

            # ---- Post-report
            print("\n=== Post-cleanup: Persons with duplicate PIs per platform ===")
            any_dup = False
            for r in session.run(REPORT_DUP_PIS):
                any_dup = True
                print(f"  {r['name']:<40} {r['platform']:<10} n={r['n']}")
            if not any_dup:
                print("  (none)")

        return 0
    finally:
        driver.close()


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--user-id", default="demo")
    p.add_argument("--dry-run", action="store_true", help="Report only; no writes")
    p.add_argument("--no-prune-orgs", action="store_true",
                   help="Skip hard-prune of org Persons (soft-tag only)")
    args = p.parse_args()
    return run(user_id=args.user_id, dry_run=args.dry_run, prune_orgs=not args.no_prune_orgs)


if __name__ == "__main__":
    sys.exit(main())
