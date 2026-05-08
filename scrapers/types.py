"""Shared dataclasses for the Source abstraction (sub-project #2).

Kept deliberately small: `WatcherInfo` describes one active watcher, `CrawlResult`
is what a Source returns after crawling that watcher. Per-platform raw events
(stars, follows, retweets, ...) are persisted directly to edge_event by the Source
implementation, so we don't need a typed event union here.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field


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
