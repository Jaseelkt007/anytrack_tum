"""Pipeline orchestrator.

Two modes:

  --queue (default)  : enqueue one crawl_watcher_for_source job per active
                        watcher into Procrastinate, then a recompute job.
                        A running worker (`python -m worker`) drains them.

  --inline           : run the same crawls in-process, sequentially. Useful
                        for local dev and CI when you don't want to spin up
                        a worker.

Usage:
    python -m scrapers.pipeline                           # queue, github only
    python -m scrapers.pipeline --inline --limit 2        # run inline against 2 watchers
    python -m scrapers.pipeline --sources github,twitter  # multi-source enqueue (twitter currently stubs)
    python -m scrapers.pipeline --skip-stars              # follows-only (inline + queue both honor it)
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from db.engine import dispose_engine, session_scope
from scrapers.registry import get_source, known_sources
from scrapers.types import WatcherInfo

logger = logging.getLogger("scrapers.pipeline")


async def _watchers_for_source(
    session: AsyncSession, *, source_name: str, user_id: str,
) -> list[WatcherInfo]:
    """Active watchlist members with a platform identity for `source_name`."""
    if source_name not in known_sources():
        raise ValueError(f"unknown source: {source_name}")
    rows = (await session.execute(text("""
        SELECT p.id, p.display_name, pi.handle
        FROM watchlist_member wm
        JOIN person p ON p.id = wm.person_id
        JOIN platform_identity pi
          ON pi.person_id = p.id AND pi.platform = :platform
        WHERE wm.user_id = :u AND wm.tier = 'active'
        ORDER BY p.display_name
    """), {"platform": source_name, "u": user_id})).all()
    return [
        WatcherInfo(canonical_id=r[0], display_name=r[1], handle=r[2])
        for r in rows
    ]


async def _run_inline(
    *,
    user_id: str,
    org_id: str,
    sources: list[str],
    limit: int | None,
    max_pages: int | None,
    skip_stars: bool,
    skip_follows: bool,
    only_handles: set[str] | None,
) -> int:
    total = 0
    async with session_scope() as session:
        for src_name in sources:
            src = get_source(src_name)
            # Per-source feature flags — only github currently honors them.
            if hasattr(src, "skip_stars"):
                src.skip_stars = skip_stars  # type: ignore[attr-defined]
            if hasattr(src, "skip_follows"):
                src.skip_follows = skip_follows  # type: ignore[attr-defined]

            watchers = await _watchers_for_source(
                session, source_name=src_name, user_id=user_id,
            )
            if only_handles:
                watchers = [w for w in watchers if w.handle.lower() in only_handles]
            if limit:
                watchers = watchers[:limit]

            print(f"[{src_name}] watchers to process: {len(watchers)}")
            for i, w in enumerate(watchers, start=1):
                print(f"  [{i}/{len(watchers)}] {w.display_name} ({w.handle})")
                try:
                    result = await src.crawl_watcher(
                        session, watcher=w, org_id=org_id, max_pages=max_pages,
                    )
                    print(f"      stars={result.stars_observed} follows={result.follows_observed} errors={result.errors}")
                    await session.commit()
                    total += result.total()
                except NotImplementedError as exc:
                    print(f"      SKIP: {exc}")
                    await session.rollback()
                except Exception as exc:
                    logger.error("failed for %s: %s", w.handle, exc)
                    await session.rollback()
                    continue

    await dispose_engine()
    print(f"\nInline run complete. total signals observed: {total}")
    return 0


async def _run_queue(
    *,
    user_id: str,
    org_id: str,
    sources: list[str],
    limit: int | None,
    max_pages: int | None,
    only_handles: set[str] | None,
) -> int:
    """Enqueue one crawl_watcher_for_source job per (source × watcher).

    The worker process (`python -m worker`) drains the jobs. A recompute job
    is enqueued at the end so convergence updates after the crawls land.
    """
    from worker.app import app
    from worker.jobs import crawl_watcher_for_source, recompute_convergence

    enqueued = 0
    async with app.open_async():
        async with session_scope() as session:
            for src_name in sources:
                if src_name not in known_sources():
                    print(f"WARN: skipping unknown source {src_name!r}", file=sys.stderr)
                    continue
                watchers = await _watchers_for_source(
                    session, source_name=src_name, user_id=user_id,
                )
                if only_handles:
                    watchers = [w for w in watchers if w.handle.lower() in only_handles]
                if limit:
                    watchers = watchers[:limit]

                for w in watchers:
                    await crawl_watcher_for_source.defer_async(
                        source_name=src_name,
                        watcher_canonical_id=str(w.canonical_id),
                        watcher_handle=w.handle,
                        watcher_display_name=w.display_name,
                        org_id=org_id,
                        max_pages=max_pages,
                    )
                    enqueued += 1
                print(f"[{src_name}] enqueued {len(watchers)} crawl jobs")

        await recompute_convergence.defer_async(org_id=org_id, user_id=user_id)

    await dispose_engine()
    print(f"\nEnqueued {enqueued} crawl jobs + 1 recompute. Run a worker to drain:")
    print("    python -m worker")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user-id", default="demo")
    parser.add_argument("--org-id", default="demo")
    parser.add_argument(
        "--sources", default="github",
        help="comma-separated list of source names (e.g. 'github' or 'github,twitter')",
    )
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--only-handles", type=str, default=None)
    parser.add_argument("--max-pages", type=int, default=None)
    parser.add_argument("--skip-stars", action="store_true")
    parser.add_argument("--skip-follows", action="store_true")
    parser.add_argument(
        "--inline", action="store_true",
        help="Run crawls in-process instead of enqueueing them (dev/CI mode).",
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")

    sources = [s.strip() for s in args.sources.split(",") if s.strip()]
    only = None
    if args.only_handles:
        only = {h.strip().lower() for h in args.only_handles.split(",") if h.strip()}

    if args.inline:
        return asyncio.run(_run_inline(
            user_id=args.user_id, org_id=args.org_id,
            sources=sources, limit=args.limit, max_pages=args.max_pages,
            skip_stars=args.skip_stars, skip_follows=args.skip_follows,
            only_handles=only,
        ))
    return asyncio.run(_run_queue(
        user_id=args.user_id, org_id=args.org_id,
        sources=sources, limit=args.limit, max_pages=args.max_pages,
        only_handles=only,
    ))


if __name__ == "__main__":
    sys.exit(main())
