"""Shared dataclasses for the Source abstraction (sub-project #2).

Kept deliberately small: `WatcherInfo` describes one active watcher, `CrawlResult`
is what a Source returns after crawling that watcher. Per-platform raw events
(stars, follows, retweets, ...) are persisted directly to edge_event by the Source
implementation, so we don't need a typed event union here.

`LeasedResources` (sub-project #4): the worker hands a Source whatever
infrastructure it needs to run the crawl. Sources that don't need them (e.g.
GitHubSource — it talks to api.github.com directly through token rotation)
ignore the kwarg.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from infra.accounts import LeasedAccount
    from infra.proxies import LeasedProxy


@dataclass(frozen=True)
class WatcherInfo:
    """One active watcher with their per-platform handle.

    `canonical_id` is the Postgres `person.id` UUID. `handle` is the
    platform-specific identifier (github username, twitter handle without @,
    linkedin slug, etc.). Sources only care about the handle.
    """

    canonical_id: uuid.UUID
    display_name: str
    handle: str  # platform-specific (caller already filtered to the right platform)


@dataclass
class CrawlResult:
    """Aggregate counts a Source returns after crawling one watcher.

    Sources may track additional per-source counters via `extras` (e.g.,
    `extras={"repos_observed": 12, "skipped_orgs": 4}`).
    """

    watcher_canonical_id: uuid.UUID
    source: str
    follows_observed: int = 0
    stars_observed: int = 0
    other_observed: int = 0
    errors: int = 0
    extras: dict[str, int | str] = field(default_factory=dict)

    def total(self) -> int:
        return self.follows_observed + self.stars_observed + self.other_observed


@dataclass
class LeasedResources:
    """Bundle of leased infrastructure handed to a Source by the worker.

    Set fields are guaranteed alive for the duration of one crawl; the worker
    releases them and reports outcomes after the Source returns. Sources may
    ignore any field they don't need.
    """
    account: "LeasedAccount | None" = None
    proxy: "LeasedProxy | None" = None
    lease_id: int | None = None  # crawl_lease.id — for outcome correlation
