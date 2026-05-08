"""Round-trip migration test.

Skipped if DATABASE_URL_DIRECT is not set, so CI without a DB doesn't fail.
Run locally with the docker-compose Postgres on port 5433 (see docker-compose.yml)
or against any disposable Neon dev branch.
"""
from __future__ import annotations

import os
import subprocess
import sys

import pytest


pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL_DIRECT"),
    reason="needs DATABASE_URL_DIRECT pointing at a disposable branch",
)


def _alembic(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        capture_output=True,
        text=True,
        env=os.environ,
    )


def test_alembic_upgrade_then_downgrade_clean():
    up = _alembic("upgrade", "head")
    assert up.returncode == 0, up.stderr

    down = _alembic("downgrade", "base")
    assert down.returncode == 0, down.stderr

    up2 = _alembic("upgrade", "head")
    assert up2.returncode == 0, up2.stderr
