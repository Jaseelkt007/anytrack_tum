"""Run the 6 M2 acceptance Cypher queries from data/README.md and report PASS/FAIL.

Usage:
    python scripts/verify_m2.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

CHECKS = [
    (
        "Total reference Persons",
        "MATCH (p:Person)-[:WATCHED_BY {tier:'reference'}]->(:User {id:'demo'}) RETURN count(p) AS n",
        1000,
    ),
    (
        "Angel count",
        "MATCH (p:Person {investor_type:'Angel'}) RETURN count(p) AS n",
        79,
    ),
    (
        "Persons with LinkedIn identity",
        "MATCH (p:Person)-[:HAS_IDENTITY]->(:PlatformIdentity {platform:'linkedin'}) RETURN count(DISTINCT p) AS n",
        133,
    ),
    (
        "Persons with Twitter identity",
        "MATCH (p:Person)-[:HAS_IDENTITY]->(:PlatformIdentity {platform:'twitter'}) RETURN count(DISTINCT p) AS n",
        129,
    ),
    (
        "Angels with Twitter (Phase 2 seed pool)",
        "MATCH (p:Person {investor_type:'Angel'})-[:HAS_IDENTITY]->(:PlatformIdentity {platform:'twitter'}) RETURN count(DISTINCT p) AS n",
        76,
    ),
    (
        "Big VCs with any platform identity (expected gap)",
        "MATCH (p:Person {investor_type:'VC - Big fund'})-[:HAS_IDENTITY]->(:PlatformIdentity) RETURN count(DISTINCT p) AS n",
        0,
    ),
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
    print(f"{'#':<3} {'Check':<55} {'Expected':>9} {'Actual':>8}  Status")
    print("-" * 90)
    with driver.session() as session:
        for i, (label, query, expected) in enumerate(CHECKS, start=1):
            actual = session.run(query).single()["n"]
            ok = actual == expected
            status = "PASS" if ok else "FAIL"
            print(f"{i:<3} {label:<55} {expected:>9} {actual:>8}  {status}")
            if not ok:
                failed += 1
    driver.close()

    print("-" * 90)
    if failed:
        print(f"{failed} check(s) failed. M2 acceptance NOT met.")
        return 1
    print(f"All {len(CHECKS)} checks passed. M2 acceptance MET.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
