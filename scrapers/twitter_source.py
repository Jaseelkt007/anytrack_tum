"""Twitter Source — stub.

The Scrapebadger-based v0.1 ingest was deleted as part of the v2 migration.
The new Twitter pipeline (own-infra browser scraping with account pool +
proxy router) lands in sub-project #5 alongside LinkedIn. Until then, this
class registers correctly but raises on `crawl_watcher` so misconfigurations
surface loudly instead of silently producing zero events.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from sqlalchemy.ext.asyncio import AsyncSession

from scrapers.base import Source
from scrapers.types import CrawlResult, WatcherInfo


@dataclass
class TwitterSource(Source):
    name: ClassVar[str] = "twitter"

    async def crawl_watcher(
        self,
        session: AsyncSession,
        *,
        watcher: WatcherInfo,
        org_id: str,
        max_pages: int | None = None,
    ) -> CrawlResult:
        raise NotImplementedError(
            "TwitterSource is deferred to sub-project #5 (own-infra scraping). "
            "Until then, do not enqueue twitter crawl jobs."
        )
