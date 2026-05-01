"""Load FOLLOWS_ON_TWITTER edges into Neo4j from a JSONL signals file.

Pairs with scrapers/jobs/fetch_twitter_followings.py (M8). The flow:

    Scrapebadger CLI                        load_twitter_signals_to_neo4j (this)
        │                                        │
        ▼                                        ▼
   data/scrapebadger_twitter_follow_signals.jsonl  ───► Neo4j
                                                         FOLLOWS_ON_TWITTER edges
                                                         + Person upserts via M7 resolver

Every NEW target Person is routed through identity.resolver.resolve_identity
so a Twitter handle that is the same real person as an existing GitHub or
LinkedIn Person gets merged onto the same canonical_id (per PHASE_2_PLAN.md M7).

Idempotent: re-running on the same JSONL file produces the same edges
(only last_seen_at advances). It is therefore safe to run after every
Scrapebadger CLI invocation.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from scrapers import cypher
from identity.resolver import Resolver, build_default_resolver

logger = logging.getLogger(__name__)


@dataclass
class LoadResult:
    signals_read: int = 0
    edges_upserted: int = 0
    target_persons_resolved: dict[str, int] = field(default_factory=dict)
    skipped_unknown_watcher: int = 0
    errors: int = 0


def _iter_signals(path: Path) -> Iterable[dict]:
    if not path.exists():
        return
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                logger.warning("skipping malformed JSONL line in %s", path)


def _split_actor(actor_or_target: str) -> tuple[str, str]:
    """'twitter:naga' -> ('twitter', 'naga'). Tolerates missing prefix."""
    if ":" in actor_or_target:
        platform, _, handle = actor_or_target.partition(":")
        return platform.lower(), handle.lower()
    return "twitter", actor_or_target.lower()


def _resolve_watcher(session, handle: str) -> str | None:
    rec = session.run(
        cypher.QUERY_PERSON_BY_TWITTER_HANDLE, handle=handle,
    ).single()
    return rec["canonical_id"] if rec else None


def load_signals(
    session,
    signals_file: Path,
    *,
    resolver: Resolver | None = None,
    now_iso: str | None = None,
) -> LoadResult:
    if now_iso is None:
        from datetime import datetime, timezone
        now_iso = datetime.now(timezone.utc).isoformat()
    if resolver is None:
        resolver = build_default_resolver(session)

    result = LoadResult()
    target_tier_counts: dict[str, int] = {}

    for sig in _iter_signals(signals_file):
        result.signals_read += 1
        try:
            actor_platform, actor_handle = _split_actor(sig["actor"])
            target_platform, target_handle = _split_actor(sig["target"])
            if actor_platform != "twitter" or target_platform != "twitter":
                logger.warning("non-twitter signal skipped: %s -> %s",
                               sig.get("actor"), sig.get("target"))
                continue

            watcher_id = _resolve_watcher(session, actor_handle)
            if not watcher_id:
                result.skipped_unknown_watcher += 1
                logger.debug("skipping signal: actor twitter:%s not in graph", actor_handle)
                continue

            metadata = sig.get("metadata") or {}
            target_resolution = resolver.resolve("twitter", target_handle, {
                "display_name": metadata.get("target_display_name") or "",
                "name":         metadata.get("target_display_name") or "",
                "url":          metadata.get("target_url") or f"https://x.com/{target_handle}",
            })
            target_tier_counts[target_resolution.tier] = (
                target_tier_counts.get(target_resolution.tier, 0) + 1
            )
            session.run(
                cypher.UPSERT_PERSON_BY_TWITTER,
                canonical_id=target_resolution.canonical_id,
                handle=target_handle,
                profile_url=metadata.get("target_url") or f"https://x.com/{target_handle}",
                display_name=metadata.get("target_display_name") or target_handle,
                now_iso=now_iso,
            )

            session.run(
                cypher.MERGE_FOLLOWS_TWITTER,
                watcher_id=watcher_id,
                target_id=target_resolution.canonical_id,
                observed_at=sig["observed_at"],
                confidence=float(sig.get("confidence", 0.0)),
                evidence_url=sig.get("evidence_url") or "",
                timing_basis=metadata.get("timing_basis") or "",
            )
            result.edges_upserted += 1
        except Exception:
            result.errors += 1
            logger.exception("failed to load signal %s", sig.get("id"))

    result.target_persons_resolved = target_tier_counts
    return result
