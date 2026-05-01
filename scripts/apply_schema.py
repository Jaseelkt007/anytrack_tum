"""Apply Cypher schema constraints/indexes to the connected Neo4j instance.

Usage:
    python scripts/apply_schema.py

Reads connection from .env (NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD).
Idempotent: every statement uses IF NOT EXISTS.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_FILE = ROOT / "scripts" / "schema.cypher"


def split_statements(cypher_text: str) -> list[str]:
    """Split a Cypher file into individual statements on `;` boundaries.

    Comments (`//...`) and blank lines are stripped.
    """
    cleaned_lines: list[str] = []
    for raw in cypher_text.splitlines():
        line = raw.split("//", 1)[0].rstrip()
        if line.strip():
            cleaned_lines.append(line)
    body = "\n".join(cleaned_lines)
    return [stmt.strip() for stmt in body.split(";") if stmt.strip()]


def main() -> int:
    try:
        from dotenv import load_dotenv
        from neo4j import GraphDatabase
        from neo4j.exceptions import ServiceUnavailable, AuthError
    except ImportError as exc:
        print(f"ERROR: missing dependency ({exc.name}). Run: pip install -r requirements.txt", file=sys.stderr)
        return 2

    load_dotenv(ROOT / ".env")

    uri = os.getenv("NEO4J_URI")
    user = os.getenv("NEO4J_USER")
    password = os.getenv("NEO4J_PASSWORD")

    missing = [k for k, v in {"NEO4J_URI": uri, "NEO4J_USER": user, "NEO4J_PASSWORD": password}.items() if not v]
    if missing:
        print(f"ERROR: missing env vars: {', '.join(missing)}. Copy .env.example to .env and fill in.", file=sys.stderr)
        return 2

    if not SCHEMA_FILE.exists():
        print(f"ERROR: schema file not found at {SCHEMA_FILE}", file=sys.stderr)
        return 2

    statements = split_statements(SCHEMA_FILE.read_text())
    print(f"Applying {len(statements)} schema statements to {uri}...")

    try:
        driver = GraphDatabase.driver(uri, auth=(user, password))
        with driver.session() as session:
            for i, stmt in enumerate(statements, start=1):
                first_line = stmt.splitlines()[0][:80]
                try:
                    session.run(stmt).consume()
                    print(f"  [{i}/{len(statements)}] OK  {first_line}")
                except Exception as exc:
                    print(f"  [{i}/{len(statements)}] FAIL  {first_line}\n        {exc}", file=sys.stderr)
                    return 1
        driver.close()
    except AuthError as exc:
        print(f"ERROR: Neo4j auth failed — check NEO4J_USER/NEO4J_PASSWORD. {exc}", file=sys.stderr)
        return 1
    except ServiceUnavailable as exc:
        print(f"ERROR: Neo4j unreachable at {uri}. {exc}", file=sys.stderr)
        return 1

    print("Schema applied. Verify in Neo4j Browser with: SHOW CONSTRAINTS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
