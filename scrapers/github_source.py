"""GitHub Source implementation — follows + stars.

Wraps the existing GitHubClient (PyGithub-based, multi-token rotation) and the
persistence helpers in scrapers.persistence. Idempotent: re-runs for the same
watcher only advance `last_seen_at` on existing edges.

Migrated from scrapers/pipeline.py:ingest_stars/ingest_follows so the pipeline
no longer carries platform logic — it only orchestrates.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import ClassVar

from sqlalchemy.ext.asyncio import AsyncSession

from scrapers.base import Source
from scrapers.github_client import GitHubClient
from scrapers.jobs.fetch_following import fetch_following
from scrapers.jobs.fetch_starred_repos import fetch_starred
from scrapers.persistence import (
    link_repo_owner,
    record_edge_event,
    upsert_person_by_github,
    upsert_repository,
)
from scrapers.types import CrawlResult, LeasedResources, WatcherInfo

logger = logging.getLogger("scrapers.github_source")


def _gather_tokens() -> list[str]:
    """Collect GITHUB_TOKEN[_2|_3|_4] env vars for token rotation."""
    tokens: list[str] = []
    primary = os.environ.get("GITHUB_TOKEN")
    if primary:
        tokens.append(primary)
    for suffix in ("_2", "_3", "_4"):
        extra = os.environ.get(f"GITHUB_TOKEN{suffix}")
        if extra:
            tokens.append(extra)
    return tokens


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


@dataclass
class GitHubSource(Source):
    """Source impl for github.com (follows + stars)."""

    name: ClassVar[str] = "github"
    requires_resources: ClassVar[bool] = False

    skip_stars: bool = False
    skip_follows: bool = False
    _client: GitHubClient | None = field(default=None, repr=False)

    @property
    def client(self) -> GitHubClient:
        if self._client is None:
            tokens = _gather_tokens()
            if not tokens:
                raise RuntimeError(
                    "GITHUB_TOKEN not set in env — GitHubSource cannot run"
                )
            self._client = GitHubClient(tokens=tokens)
        return self._client

    async def crawl_watcher(
        self,
        session: AsyncSession,
        *,
        watcher: WatcherInfo,
        org_id: str,
        max_pages: int | None = None,
        resources: LeasedResources | None = None,  # ignored — GitHub uses its own token pool
    ) -> CrawlResult:
        result = CrawlResult(
            watcher_canonical_id=watcher.canonical_id,
            source=self.name,
        )

        if not self.skip_stars:
            try:
                repos, stars = await self._ingest_stars(
                    session, watcher=watcher, org_id=org_id, max_pages=max_pages,
                )
                result.stars_observed = stars
                result.extras["repos_observed"] = repos
            except Exception as exc:
                logger.exception("stars failed for %s: %s", watcher.handle, exc)
                result.errors += 1

        if not self.skip_follows:
            try:
                follows, skipped = await self._ingest_follows(
                    session, watcher=watcher, org_id=org_id, max_pages=max_pages,
                )
                result.follows_observed = follows
                if skipped:
                    result.extras["skipped_non_user_follows"] = skipped
            except Exception as exc:
                logger.exception("follows failed for %s: %s", watcher.handle, exc)
                result.errors += 1

        return result

    async def _ingest_stars(
        self,
        session: AsyncSession,
        *,
        watcher: WatcherInfo,
        org_id: str,
        max_pages: int | None,
    ) -> tuple[int, int]:
        """Returns (repos_observed, stars_observed)."""
        repos = 0
        stars = 0
        for event in fetch_starred(self.client, watcher.handle, max_pages=max_pages):
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
                source=self.name,
                action_type="star",
                watcher_person_id=watcher.canonical_id,
                target_kind="repository",
                target_repo_id=str(event.repo_github_id),
                observed_at=_parse_iso_or_now(event.starred_at),
                evidence_url=event.repo_html_url,
            )
            stars += 1

            await link_repo_owner(
                session,
                org_id=org_id,
                repo_github_id=str(event.repo_github_id),
                owner_handle=event.repo_owner,
            )
        return repos, stars

    async def _ingest_follows(
        self,
        session: AsyncSession,
        *,
        watcher: WatcherInfo,
        org_id: str,
        max_pages: int | None,
    ) -> tuple[int, int]:
        """Returns (follows_observed, non_user_skipped)."""
        follows = 0
        skipped = 0
        now = datetime.now(timezone.utc)
        for entry in fetch_following(self.client, watcher.handle, max_pages=max_pages):
            if entry.type != "User":
                skipped += 1
                continue

            followed_id = await upsert_person_by_github(
                session,
                org_id=org_id,
                handle=entry.handle,
                display_name=entry.handle,
                profile_url=entry.profile_url,
                kind=entry.type,
            )
            await record_edge_event(
                session,
                org_id=org_id,
                source=self.name,
                action_type="follow",
                watcher_person_id=watcher.canonical_id,
                target_kind="person",
                target_person_id=followed_id,
                observed_at=now,
                evidence_url=entry.profile_url,
            )
            follows += 1
        return follows, skipped
