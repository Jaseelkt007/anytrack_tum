"""GitHub ingestion pipeline orchestrator.

Reads tier='active' watchlist members from Postgres (each must have a github
platform_identity), fetches their stars and follows from GitHub, writes
append-only events into edge_event.

Usage:
    python -m scrapers.pipeline                       # full sweep, default user 'demo'
    python -m scrapers.pipeline --limit 3             # cap to N members for fast iteration
    python -m scrapers.pipeline --skip-stars          # only fetch follows
    python -m scrapers.pipeline --skip-follows        # only fetch stars
    python -m scrapers.pipeline --user-id demo --max-pages 2
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from db.engine import dispose_engine, session_scope
from scrapers.github_client import GitHubClient
from scrapers.jobs.fetch_following import fetch_following
from scrapers.jobs.fetch_starred_repos import fetch_starred
from scrapers.persistence import (
    fetch_active_watchlist_with_github,
    gh_person_id,
    link_repo_owner,
    record_edge_event,
    upsert_person_by_github,
    upsert_repository,
)

logger = logging.getLogger("scrapers.pipeline")


def _gather_tokens() -> list[str]:
    """Collect any present GITHUB_TOKEN[_2|_3|_4] env vars."""
    tokens: list[str] = []
    primary = os.environ.get("GITHUB_TOKEN")
    if primary:
        tokens.append(primary)
    for suffix in ("_2", "_3", "_4"):
        extra = os.environ.get(f"GITHUB_TOKEN{suffix}")
        if extra:
            tokens.append(extra)
    return tokens


async def ingest_stars(session: AsyncSession, client: GitHubClient,
                        watcher: dict, *, org_id: str,
                        max_pages: int | None) -> tuple[int, int]:
    """Returns (repos_observed, stars_observed)."""
    handle = watcher["github_handle"]
    repos = 0
    stars = 0
    for event in fetch_starred(client, handle, max_pages=max_pages):
        await upsert_repository(
            session,
            github_id=str(event.repo_github_id),
            owner_handle=event.repo_owner,
            name=event.repo_name,
            full_name=event.repo_full_name,
            description=event.repo_description,
            language=event.repo_language,
            star_count=event.repo_star_count,
            html_url=event.repo_html_url,
        )
        repos += 1

        await record_edge_event(
            session,
            org_id=org_id,
            source="github",
            action_type="star",
            watcher_person_id=watcher["canonical_id"],
            target_kind="repository",
            target_repo_id=str(event.repo_github_id),
            observed_at=event.starred_at if isinstance(event.starred_at, datetime)
                else _parse_iso_or_now(event.starred_at),
            evidence_url=event.repo_html_url,
        )
        stars += 1

        # Opportunistic: link the owner if they're/become a known Person.
        await link_repo_owner(
            session,
            org_id=org_id,
            repo_github_id=str(event.repo_github_id),
            owner_handle=event.repo_owner,
        )
    return repos, stars


async def ingest_follows(session: AsyncSession, client: GitHubClient,
                          watcher: dict, *, org_id: str,
                          max_pages: int | None) -> int:
    handle = watcher["github_handle"]
    follows = 0
    skipped_non_user = 0
    now = datetime.now(timezone.utc)
    for entry in fetch_following(client, handle, max_pages=max_pages):
        if entry.type != "User":
            skipped_non_user += 1
            continue

        followed_id = await upsert_person_by_github(
            session,
            org_id=org_id,
            handle=entry.handle,
            display_name=entry.handle,  # bare handle until enriched
            profile_url=entry.profile_url,
            kind=entry.type,
        )

        await record_edge_event(
            session,
            org_id=org_id,
            source="github",
            action_type="follow",
            watcher_person_id=watcher["canonical_id"],
            target_kind="person",
            target_person_id=followed_id,
            observed_at=now,
            evidence_url=entry.profile_url,
        )
        follows += 1

    if skipped_non_user:
        logger.info("skipped %d non-User follows for %s", skipped_non_user, handle)
    return follows


def _parse_iso_or_now(value) -> datetime:
    if isinstance(value, datetime):
        return value
    if not value:
        return datetime.now(timezone.utc)
    try:
        s = value
        if isinstance(s, str) and s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s) if isinstance(s, str) else datetime.now(timezone.utc)
    except ValueError:
        return datetime.now(timezone.utc)


async def _async_run(*, user_id: str, org_id: str, limit: int | None,
                      max_pages: int | None, skip_stars: bool, skip_follows: bool,
                      only_handles: set[str] | None) -> int:
    tokens = _gather_tokens()
    if not tokens:
        print("ERROR: GITHUB_TOKEN not set in .env", file=sys.stderr)
        return 2

    client = GitHubClient(tokens=tokens)

    total_repos = total_stars = total_follows = 0

    async with session_scope() as session:
        members = await fetch_active_watchlist_with_github(session, user_id=user_id)
        if not members:
            print("WARN: no tier='active' watchlist members with a GitHub identity found.",
                  file=sys.stderr)
            print("      Run scripts/bootstrap_demo_data.py first.", file=sys.stderr)
            return 1

        if only_handles:
            members = [m for m in members
                       if (m.get("github_handle") or "").lower() in only_handles]
        if limit:
            members = members[:limit]

        print(f"Active watchlist members to process: {len(members)}")
        for i, m in enumerate(members, start=1):
            label = f"{m['display_name']} ({m['github_handle']})"
            print(f"[{i}/{len(members)}] {label}")
            try:
                if not skip_stars:
                    repos, stars = await ingest_stars(
                        session, client, m, org_id=org_id, max_pages=max_pages,
                    )
                    total_repos += repos
                    total_stars += stars
                    print(f"    stars: {stars}")
                if not skip_follows:
                    f = await ingest_follows(
                        session, client, m, org_id=org_id, max_pages=max_pages,
                    )
                    total_follows += f
                    print(f"    follows: {f}")
                # Commit per-watcher so a partial failure doesn't lose hours of work.
                await session.commit()
            except Exception as exc:
                logger.error("failed for %s: %s", label, exc)
                await session.rollback()
                continue

    await dispose_engine()

    print()
    print(f"Pipeline complete.  repos_upserted={total_repos}  stars={total_stars}  follows={total_follows}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user-id", default="demo")
    parser.add_argument("--org-id", default="demo")
    parser.add_argument("--limit", type=int, default=None,
                        help="cap number of watchlist members processed")
    parser.add_argument("--only-handles", type=str, default=None,
                        help="comma-separated github handles to restrict to")
    parser.add_argument("--max-pages", type=int, default=None,
                        help="cap pages per fetch (useful for fast iteration)")
    parser.add_argument("--skip-stars", action="store_true")
    parser.add_argument("--skip-follows", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")

    only = None
    if args.only_handles:
        only = {h.strip().lower() for h in args.only_handles.split(",") if h.strip()}

    return asyncio.run(_async_run(
        user_id=args.user_id, org_id=args.org_id,
        limit=args.limit, max_pages=args.max_pages,
        skip_stars=args.skip_stars, skip_follows=args.skip_follows,
        only_handles=only,
    ))


if __name__ == "__main__":
    sys.exit(main())
