"""Source Protocol — the pluggable boundary for every platform we ingest from.

Each platform (github, twitter, linkedin, ...) implements one `Source` class.
The orchestrator (and the worker job) only knows about Sources, not about
platform-specific clients. Adding a new platform = add a new class + register it
in scrapers.registry.
"""
from __future__ import annotations

from typing import ClassVar, Protocol, runtime_checkable

from sqlalchemy.ext.asyncio import AsyncSession

from scrapers.types import CrawlResult, LeasedResources, WatcherInfo


@runtime_checkable
class Source(Protocol):
    """A platform-specific crawler.

    Implementations:
      - GitHubSource    → scrapers/github_source.py
      - TwitterSource   → scrapers/twitter_source.py (stub until #5)
      - LinkedInSource  → arrives in sub-project #5

    `requires_resources` declares whether the Source needs leased account /
    proxy / browser. The worker only goes through the lease dance for sources
    that say True (LinkedIn, future Twitter). GitHub-via-PyGithub returns False
    and uses its own internal token rotation.
    """

    name: ClassVar[str]
    """Stable identifier — must match `edge_event.source` values
    ('github' | 'twitter' | 'linkedin' | ...)."""

    requires_resources: ClassVar[bool] = False

    async def crawl_watcher(
        self,
        session: AsyncSession,
        *,
        watcher: WatcherInfo,
        org_id: str,
        max_pages: int | None = None,
        resources: LeasedResources | None = None,
    ) -> CrawlResult:
        """Fetch all observable signals for one watcher, write them to edge_event.

        Implementations are responsible for:
          - calling their platform-specific HTTP/SDK client
          - upserting referenced Persons/Repositories via scrapers.persistence
          - writing one edge_event row per observed signal (idempotent UPSERT)
          - returning aggregate counts in CrawlResult

        `resources`: present iff `requires_resources == True`. Sources that
        don't need it can ignore the kwarg.

        Idempotency: re-running for the same watcher should produce the same
        edges, with `last_seen_at` advancing on re-observation.
        """
        ...
