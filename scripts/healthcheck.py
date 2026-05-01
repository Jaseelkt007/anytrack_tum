"""Phase 1 healthcheck — confirms each team member's environment is wired up.

Verifies:
  1. Neo4j is reachable and the credentials work (runs a trivial query).
  2. GitHub PAT is valid and has rate-limit headroom.
  3. Optional checks (Anthropic key) are reported but not required for M1.

Usage:
    python scripts/healthcheck.py

Exit code 0 means M1 acceptance is met for this laptop.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent


def check_neo4j() -> tuple[bool, str]:
    uri = os.getenv("NEO4J_URI")
    user = os.getenv("NEO4J_USER")
    password = os.getenv("NEO4J_PASSWORD")
    if not all([uri, user, password]):
        return False, "missing NEO4J_URI / NEO4J_USER / NEO4J_PASSWORD in .env"

    try:
        from neo4j import GraphDatabase
        from neo4j.exceptions import AuthError, ServiceUnavailable
    except ImportError:
        return False, "neo4j package not installed — run: pip install -r requirements.txt"

    try:
        driver = GraphDatabase.driver(uri, auth=(user, password))
        with driver.session() as session:
            result = session.run("RETURN 1 AS ok").single()
            assert result and result["ok"] == 1
        driver.close()
        return True, f"connected to {uri}"
    except AuthError as exc:
        return False, f"auth failed: {exc}"
    except ServiceUnavailable as exc:
        return False, f"unreachable: {exc}"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


def check_github() -> tuple[bool, str]:
    token = os.getenv("GITHUB_TOKEN")
    if not token:
        return False, "missing GITHUB_TOKEN in .env (create a PAT at github.com/settings/tokens)"

    try:
        import requests
    except ImportError:
        return False, "requests not installed — run: pip install -r requirements.txt"

    try:
        resp = requests.get(
            "https://api.github.com/rate_limit",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
            timeout=10,
        )
        if resp.status_code == 401:
            return False, "PAT rejected (401) — token is invalid or revoked"
        if resp.status_code != 200:
            return False, f"unexpected status {resp.status_code}: {resp.text[:120]}"
        data = resp.json()
        core = data.get("resources", {}).get("core", {})
        remaining = core.get("remaining", "?")
        limit = core.get("limit", "?")
        return True, f"PAT valid, rate limit {remaining}/{limit}"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


def check_anthropic_optional() -> tuple[bool, str]:
    """Optional in M1; required only when 'why now' NLG comes online in Phase 2."""
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key:
        return True, "not set (optional in Phase 1)"
    if not key.startswith("sk-ant-"):
        return False, "key does not look like an Anthropic key (expected sk-ant-...)"
    return True, "present (not validated against API)"


def main() -> int:
    load_dotenv(ROOT / ".env")

    checks = [
        ("Neo4j", check_neo4j, True),
        ("GitHub PAT", check_github, True),
        ("Anthropic (optional)", check_anthropic_optional, False),
    ]

    print("Phase 1 healthcheck")
    print("-" * 50)
    failed_required = False
    for label, fn, required in checks:
        ok, msg = fn()
        status = "OK  " if ok else "FAIL"
        marker = "" if required else "  [optional]"
        print(f"  [{status}] {label}: {msg}{marker}")
        if not ok and required:
            failed_required = True

    print("-" * 50)
    if failed_required:
        print("M1 acceptance NOT met — fix the FAIL lines above.")
        return 1
    print("M1 acceptance met for this laptop. Neo4j OK, GitHub PAT OK.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
