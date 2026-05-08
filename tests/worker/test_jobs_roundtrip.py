"""Job round-trip — verify the recompute_convergence task body runs against
the live database. The full enqueue→worker→drain path is exercised manually
(see commit df95089: 4 jobs ran to `succeeded`, edge_event went 175→375).
"""
from __future__ import annotations

import os

import pytest


pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="needs DATABASE_URL to run convergence end-to-end",
)


@pytest.mark.asyncio
async def test_recompute_convergence_runs_against_live_db():
    """Calls the task body directly so we exercise the same code path as the
    worker without spinning up a worker process."""
    from worker.jobs import recompute_convergence

    res = await recompute_convergence(org_id="demo", user_id="demo")

    assert res["org_id"] == "demo"
    assert res["user_id"] == "demo"
    assert isinstance(res["fired"], int)


@pytest.mark.asyncio
async def test_crawl_watcher_for_source_idempotent_on_unknown_source():
    """The task layer routes via the registry; unknown sources surface a
    KeyError so a misconfigured periodic schedule fails loudly."""
    from worker.jobs import crawl_watcher_for_source

    with pytest.raises(KeyError):
        await crawl_watcher_for_source(
            source_name="linkedin",  # not registered until #5
            watcher_canonical_id="00000000-0000-0000-0000-000000000000",
            watcher_handle="x", watcher_display_name="x",
        )
