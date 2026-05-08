"""Bootstrap idempotency test.

Asserts the bootstrap script can be run twice without errors and without
creating duplicate rows.
"""
from __future__ import annotations

import os
import subprocess
import sys

import pytest


pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="needs DATABASE_URL",
)


def _bootstrap() -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "scripts.bootstrap_demo_data"],
        capture_output=True,
        text=True,
        env=os.environ,
    )


def _row_counts() -> dict[str, int]:
    """Use the synchronous psycopg URL to keep the test simple."""
    import psycopg

    url = os.environ["DATABASE_URL_DIRECT"].replace("postgresql+psycopg://", "postgresql://")
    counts: dict[str, int] = {}
    with psycopg.connect(url) as conn:
        with conn.cursor() as cur:
            for table in ("person", "platform_identity", "watchlist_member"):
                cur.execute(f"SELECT COUNT(*) FROM {table}")
                row = cur.fetchone()
                counts[table] = row[0] if row else 0
    return counts


def test_bootstrap_is_idempotent():
    r1 = _bootstrap()
    assert r1.returncode == 0, r1.stderr
    counts1 = _row_counts()

    r2 = _bootstrap()
    assert r2.returncode == 0, r2.stderr
    counts2 = _row_counts()

    assert counts1 == counts2, f"row counts changed across runs: {counts1} vs {counts2}"
    assert counts1["person"] > 0
