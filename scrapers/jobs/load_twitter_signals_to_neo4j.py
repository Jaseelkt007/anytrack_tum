"""Deferred — Twitter signal loader.

The original Cypher-based loader was deleted as part of the v2 Postgres
migration. The new twitter pipeline lives behind sub-project #2 (Source
abstraction + Procrastinate queue) and #5 (LinkedIn + Twitter own-infra
scraping). Until those land, this module raises explicitly to keep import
chains honest.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class LoadResult:
    signals_read: int = 0
    edges_upserted: int = 0
    target_persons_resolved: dict[str, int] = field(default_factory=dict)
    skipped_unknown_watcher: int = 0
    errors: int = 0


def load_signals(*args, **kwargs) -> LoadResult:
    raise NotImplementedError(
        "load_twitter_signals_to_neo4j is deferred to sub-project #2/#5; "
        "the v2 Postgres twitter loader is not yet implemented."
    )
