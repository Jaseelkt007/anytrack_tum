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

    For sources with `requires_resources=True` we lease an account + proxy
    via infra.accounts/infra.proxies, audit the lease in `crawl_lease`, and
    report outcomes back to the pools. For sources that don't need them
    (GitHub today) we skip the lease dance entirely.

    Idempotent: re-running for the same watcher only advances last_seen_at on
    existing edge_event rows. Designed to be retryable.
    """
    from sqlalchemy import text as sa_text

    from scrapers.registry import get_source
    from scrapers.types import LeasedResources, WatcherInfo

    source = get_source(source_name)
    watcher_uuid = uuid.UUID(watcher_canonical_id)
    watcher = WatcherInfo(
        canonical_id=watcher_uuid,
        display_name=watcher_display_name,
        handle=watcher_handle,
    )

    if not getattr(source, "requires_resources", False):
        async with session_scope() as session:
            result = await source.crawl_watcher(
                session, watcher=watcher, org_id=org_id, max_pages=max_pages,
            )
    else:
        from infra.accounts import (
            NoAccountAvailable,
            checkout_account,
            report_account_outcome,
        )
        from infra.proxies import (
            NoProxyAvailable,
            pick_proxy,
            report_proxy_outcome,
        )

        # Stage 1: lease resources in their own committed transaction so other
        # workers see the bumped used_today / last_used_at right away.
        async with session_scope() as lease_session:
            try:
                account = await checkout_account(
                    lease_session, source=source_name, watcher_id=watcher_uuid,
                )
            except NoAccountAvailable as exc:
                logger.warning("no account available for %s: %s", source_name, exc)
                raise

            try:
                proxy = await pick_proxy(
                    lease_session, watcher_id=watcher_uuid,
                )
            except NoProxyAvailable:
                proxy = None  # some sources may run direct; let Source decide

            lease_row = (await lease_session.execute(
                sa_text("""
                    INSERT INTO crawl_lease
                      (account_id, proxy_id, watcher_person_id, source, status)
                    VALUES (:a, :p, :w, :s, 'held')
                    RETURNING id
                """),
                {"a": account.id,
                 "p": proxy.id if proxy else None,
                 "w": watcher_uuid,
                 "s": source_name},
            )).first()
            lease_id = lease_row[0]

        resources = LeasedResources(account=account, proxy=proxy, lease_id=lease_id)

        # Stage 2: do the crawl. Resource outcomes are reported in their own
        # session so they survive even if the crawl session rolls back.
        crawl_outcome = "released"
        ban = False
        try:
            async with session_scope() as session:
                result = await source.crawl_watcher(
                    session, watcher=watcher, org_id=org_id,
                    max_pages=max_pages, resources=resources,
                )
        except Exception:
            crawl_outcome = "failed"
            raise
        finally:
            async with session_scope() as outcome_session:
                await report_account_outcome(
                    outcome_session, account.id,
                    success=(crawl_outcome == "released"), ban=ban,
                )
                if proxy is not None:
                    await report_proxy_outcome(
                        outcome_session, proxy.id,
                        success=(crawl_outcome == "released"), ban=ban,
                    )
                await outcome_session.execute(
                    sa_text("""
                        UPDATE crawl_lease
                        SET status = :status, released_at = now(), outcome = :outcome
                        WHERE id = :id
                    """),
                    {"id": lease_id, "status": crawl_outcome, "outcome": crawl_outcome},
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
