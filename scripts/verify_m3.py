"""M3 acceptance checks. Run after scrapers.pipeline has populated Neo4j.

Checks (per PHASE_1_PLAN.md M3 acceptance criteria):
  1. Hundreds of Repository nodes exist
  2. Thousands of STARRED_REPO edges exist
  3. STARRED_REPO edges have first_seen_at populated (historical timestamps preserved)
  4. At least one historical edge from before 2024 exists (proves backtest depth)
  5. Re-running the pipeline does not duplicate edges (idempotency)
     — checked by comparing counts before/after a re-run; this script only
        reports the current count and asks the operator to re-run if uncertain.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

CHECKS = [
    ("Repository nodes >= 100",
     "MATCH (r:Repository) RETURN count(r) AS n",
     lambda n: n >= 100),
    ("STARRED_REPO edges >= 1000",
     "MATCH ()-[e:STARRED_REPO]->() RETURN count(e) AS n",
     lambda n: n >= 1000),
    ("STARRED_REPO edges with first_seen_at >= 1000",
     "MATCH ()-[e:STARRED_REPO]->() WHERE e.first_seen_at IS NOT NULL RETURN count(e) AS n",
     lambda n: n >= 1000),
    ("At least one STARRED_REPO from before 2024",
     "MATCH ()-[e:STARRED_REPO]->() WHERE e.first_seen_at < datetime('2024-01-01T00:00:00Z') RETURN count(e) AS n",
     lambda n: n >= 1),
    ("FOLLOWS_ON_GITHUB edges >= 100",
     "MATCH ()-[e:FOLLOWS_ON_GITHUB]->() RETURN count(e) AS n",
     lambda n: n >= 100),
    ("Persons with github identity >= active watchlist count",
     """MATCH (p:Person)-[:HAS_IDENTITY]->(:PlatformIdentity {platform:'github'})
        RETURN count(DISTINCT p) AS n""",
     lambda n: n >= 28),
    ("STARRED_REPO has both first_seen_at and last_seen_at",
     """MATCH ()-[e:STARRED_REPO]->()
        WHERE e.first_seen_at IS NOT NULL AND e.last_seen_at IS NOT NULL
        RETURN count(e) AS n""",
     lambda n: n >= 1000),
]


def main() -> int:
    from dotenv import load_dotenv
    from neo4j import GraphDatabase
    load_dotenv(ROOT / ".env")

    driver = GraphDatabase.driver(
        os.environ["NEO4J_URI"],
        auth=(os.environ["NEO4J_USER"], os.environ["NEO4J_PASSWORD"]),
    )

    failed = 0
    print(f"{'#':<3} {'Check':<55} {'Actual':>10}  Status")
    print("-" * 80)
    with driver.session() as session:
        for i, (label, query, predicate) in enumerate(CHECKS, start=1):
            actual = session.run(query).single()["n"]
            ok = predicate(actual)
            status = "PASS" if ok else "FAIL"
            print(f"{i:<3} {label:<55} {actual:>10}  {status}")
            if not ok:
                failed += 1
    driver.close()

    print("-" * 80)
    if failed:
        print(f"{failed} check(s) failed. M3 acceptance NOT met.")
        return 1
    print(f"All {len(CHECKS)} checks passed. M3 acceptance MET.")
    print("\nReminder: idempotency must be re-verified by re-running the pipeline ")
    print("and confirming STARRED_REPO + FOLLOWS_ON_GITHUB counts do not grow.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
