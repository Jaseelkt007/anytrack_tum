"""Procrastinate periodic tasks (cron-like).

These replace the APScheduler hooks that lived in backend/scheduler.py. The
worker process picks up the schedules; the API process stays scheduler-free.

Schedules are conservative defaults — tunable via env at deploy time once we
move past local dev.
"""
from __future__ import annotations

import os

from worker.app import app
from worker.jobs import dispatch_pipeline_sweep, recompute_convergence


def _hours(env_key: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(env_key, default)))
    except ValueError:
        return default


PIPELINE_INTERVAL_HOURS = _hours("PIPELINE_INTERVAL_HOURS", 6)
CONVERGENCE_INTERVAL_HOURS = _hours("CONVERGENCE_INTERVAL_HOURS", 1)


@app.periodic(cron=f"0 */{PIPELINE_INTERVAL_HOURS} * * *")
@app.task(name="anytrace.periodic.pipeline_sweep", queue="scrape")
async def periodic_pipeline_sweep(timestamp: int) -> dict:
    """Periodic full sweep — fan out crawls + recompute convergence."""
    return await dispatch_pipeline_sweep(sources=["github"])


@app.periodic(cron=f"0 */{CONVERGENCE_INTERVAL_HOURS} * * *")
@app.task(name="anytrace.periodic.convergence_recompute", queue="intel")
async def periodic_convergence_recompute(timestamp: int) -> dict:
    """Light periodic — recompute convergence on the existing edge_event data
    even if no new crawls ran (handy after rule edits)."""
    return await recompute_convergence()
