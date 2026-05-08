"""Procrastinate task definitions.

Tasks are intentionally small and idempotent — each one wraps a single
scraper.Source call against a single watcher, or a single periodic operation
(convergence recompute). The pipeline orchestrator enqueues fan-out: one
crawl_watcher_for_source job per (source, watcher).
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import text

from db.engine import dispose_engine, session_scope
from worker.app import app

logger = logging.getLogger("worker.jobs")


@app.task(name="anytrace.crawl_watcher_for_source", queue="scrape")
async def crawl_watcher_for_source(
    *,
    source_name: str,
    watcher_canonical_id: str,
    watcher_handle: str,
    watcher_display_name: str,
    org_id: str = "demo",
    max_pages: int | None = None,
) -> dict:
    """Crawl one watcher's signals on one platform.

    Idempotent: re-running for the same watcher only advances last_seen_at on
    existing edge_event rows. Designed to be retryable.
    """
    from scrapers.registry import get_source
    from scrapers.types import WatcherInfo

    source = get_source(source_name)
    watcher = WatcherInfo(
        canonical_id=uuid.UUID(watcher_canonical_id),
        display_name=watcher_display_name,
        handle=watcher_handle,
    )

    async with session_scope() as session:
        result = await source.crawl_watcher(
            session, watcher=watcher, org_id=org_id, max_pages=max_pages,
        )

    logger.info(
        "crawl_watcher_for_source done: source=%s watcher=%s follows=%d stars=%d errors=%d",
        source_name, watcher_handle,
        result.follows_observed, result.stars_observed, result.errors,
    )
    return {
        "source": source_name,
        "watcher_handle": watcher_handle,
        "follows": result.follows_observed,
        "stars": result.stars_observed,
        "other": result.other_observed,
        "errors": result.errors,
        "extras": result.extras,
    }


@app.task(name="anytrace.recompute_convergence", queue="intel")
async def recompute_convergence(
    *,
    org_id: str = "demo",
    user_id: str = "demo",
) -> dict:
    """Recompute convergence events for an org/user using their alert rule."""
    from intelligence.convergence import find_convergences, persist_events
    from intelligence.rule import get_rule

    rule = get_rule(user_id)
    end = datetime.now(timezone.utc)

    async with session_scope() as session:
        events = await find_convergences(
            session, user_id=user_id, org_id=org_id, as_of=end, rule=rule,
        )
        await persist_events(
            session, events,
            user_id=user_id, org_id=org_id,
            window_end_iso=end.isoformat(),
        )

    logger.info(
        "recompute_convergence done: org=%s user=%s fired=%d",
        org_id, user_id, len(events),
    )
    return {"org_id": org_id, "user_id": user_id, "fired": len(events)}


@app.task(name="anytrace.dispatch_pipeline_sweep", queue="scrape")
async def dispatch_pipeline_sweep(
    *,
    sources: list[str] | None = None,
    org_id: str = "demo",
    user_id: str = "demo",
    max_pages: int | None = 1,
) -> dict:
    """Fan out one crawl job per (source × active watcher).

    The follow-up `recompute_convergence` is enqueued at the end of the dispatch
    so it runs after the crawl jobs drain. Procrastinate has no native
    after-fanout join; we rely on the queue order + the recompute being cheap +
    idempotent (worst case it runs once before the last crawl finishes and
    re-runs on the next sweep).
    """
    if sources is None:
        sources = ["github"]  # twitter stays out until #5

    enqueued = 0
    async with session_scope() as session:
        # Per-source watchers; for now github is the only live source so we
        # keep this simple (one query per source). Expand when twitter/linkedin
        # land — the watchlist filter changes per source.
        for src in sources:
            if src == "github":
                rows = (await session.execute(text("""
                    SELECT p.id, p.display_name, pi.handle
                    FROM watchlist_member wm
                    JOIN person p ON p.id = wm.person_id
                    JOIN platform_identity pi
                      ON pi.person_id = p.id AND pi.platform = 'github'
                    WHERE wm.user_id = :u AND wm.tier = 'active'
                """), {"u": user_id})).all()
            else:
                rows = []

            for r in rows:
                await crawl_watcher_for_source.defer_async(
                    source_name=src,
                    watcher_canonical_id=str(r[0]),
                    watcher_handle=r[2],
                    watcher_display_name=r[1],
                    org_id=org_id,
                    max_pages=max_pages,
                )
                enqueued += 1

    # Convergence recompute follows the crawl batch.
    await recompute_convergence.defer_async(org_id=org_id, user_id=user_id)

    logger.info(
        "dispatch_pipeline_sweep enqueued: %d crawl jobs (%s) + 1 recompute",
        enqueued, sources,
    )
    return {"crawl_jobs_enqueued": enqueued, "recompute_enqueued": True, "sources": sources}
