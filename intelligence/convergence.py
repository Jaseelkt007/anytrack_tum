"""Convergence detection — the core intelligence layer.

A "convergence" fires when ≥ N distinct active watchers have signal edges to the
same target Person within a sliding time window. Two signal types contribute:

  1. FOLLOWS_ON_GITHUB: watcher -> target Person directly
  2. STARRED_REPO + OWNS_REPO: watcher -> Repository <- target Person (the repo owner)

Phase 1 scoring (kept simple — Bayesian + Cox come in Phase 2):
    score = distinct_member_count
          + recency_bonus            (0..1, more recent edges weighted more)
          + member_quality_placeholder  (constant 0 in Phase 1)

CLI:
    python -m intelligence.convergence                  # default 90d, N=2
    python -m intelligence.convergence --window 365 --min-members 2 --persist
    python -m intelligence.convergence --as-of 2023-11-01  # backtest mode
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

logger = logging.getLogger("intelligence.convergence")


# --- Data class -------------------------------------------------------------

@dataclass
class ConvergenceEvent:
    """One convergence signal. Persisted as a ConvergenceEvent node in Neo4j."""

    target_id: str
    target_name: str
    user_id: str
    fired_at: str
    window_start: str
    window_end: str
    distinct_member_count: int
    member_ids: list[str]
    member_names: list[str]
    score: float
    score_breakdown: dict[str, float] = field(default_factory=dict)
    first_signal_at: Optional[str] = None
    last_signal_at: Optional[str] = None
    signal_type_counts: dict[str, int] = field(default_factory=dict)
    evidence: list[dict[str, Any]] = field(default_factory=list)

    @property
    def event_id(self) -> str:
        # Stable id so re-runs MERGE the same event
        return f"cv-{self.user_id}-{self.target_id}-{self.window_end[:10]}"


# --- Cypher -----------------------------------------------------------------
# One unified query: find every (watcher, target) edge of either signal type
# inside the window, then aggregate per target. The `target` is always a Person.

UNIFIED_CONVERGENCE_QUERY = """
// FOLLOWS_ON_GITHUB signals
MATCH (w:Person)-[:WATCHED_BY {tier: 'active'}]->(:User {id: $user_id})
MATCH (w)-[edge:FOLLOWS_ON_GITHUB]->(target:Person)
WHERE edge.first_seen_at >= datetime($window_start)
  AND edge.first_seen_at <= datetime($window_end)
  AND NOT (target)-[:WATCHED_BY {tier: 'active'}]->(:User {id: $user_id})
RETURN target, w, edge.first_seen_at AS edge_at,
       'FOLLOWS_ON_GITHUB' AS signal_type,
       NULL AS repo_full_name, NULL AS repo_url

UNION

// STARRED_REPO signals — watcher stars a repo whose owner is a Person we know
MATCH (w:Person)-[:WATCHED_BY {tier: 'active'}]->(:User {id: $user_id})
MATCH (w)-[edge:STARRED_REPO]->(repo:Repository)
MATCH (target:Person)-[:OWNS_REPO]->(repo)
WHERE edge.first_seen_at >= datetime($window_start)
  AND edge.first_seen_at <= datetime($window_end)
  AND NOT (target)-[:WATCHED_BY {tier: 'active'}]->(:User {id: $user_id})
  AND target <> w
RETURN target, w, edge.first_seen_at AS edge_at,
       'STARRED_REPO' AS signal_type,
       repo.full_name AS repo_full_name, repo.html_url AS repo_url
"""

# Aggregation runs in Python (more flexible than UNION+aggregate inside Cypher).


# --- ConvergenceEvent persistence -------------------------------------------

UPSERT_CONVERGENCE_EVENT = """
MERGE (c:ConvergenceEvent {id: $id})
SET
    c.target_person_id        = $target_id,
    c.user_id                 = $user_id,
    c.fired_at                = datetime($fired_at),
    c.window_start            = datetime($window_start),
    c.window_end              = datetime($window_end),
    c.distinct_member_count   = $distinct_member_count,
    c.member_ids              = $member_ids,
    c.score                   = $score,
    c.score_breakdown_json    = $score_breakdown_json,
    c.first_signal_at         = CASE WHEN $first_signal_at IS NULL THEN NULL ELSE datetime($first_signal_at) END,
    c.last_signal_at          = CASE WHEN $last_signal_at  IS NULL THEN NULL ELSE datetime($last_signal_at)  END,
    c.signal_type_counts_json = $signal_type_counts_json,
    c.evidence_json           = $evidence_json
WITH c
MATCH (target:Person {canonical_id: $target_id})
MERGE (c)-[:ABOUT]->(target)
"""

DELETE_STALE_CONVERGENCE_EVENTS = """
// Remove events that share the same window_end_date as the current run
// but whose target is not in the new set. Preserves historical snapshots
// (different date suffix) while keeping the current view in sync with the rule.
MATCH (c:ConvergenceEvent {user_id: $user_id})
WHERE c.id ENDS WITH $window_end_date
  AND NOT c.target_person_id IN $current_target_ids
DETACH DELETE c
"""


# --- Pure-function scoring (testable without Neo4j) ------------------------

def compute_score(distinct_member_count: int,
                  newest_edge_iso: Optional[str],
                  window_end_iso: str,
                  window_days: int,
                  *,
                  weight_distinct_members: float = 1.0,
                  weight_recency: float = 1.0,
                  weight_member_quality: float = 0.0,
                  member_quality_value: float = 0.0) -> tuple[float, dict[str, float]]:
    """Phase 1 score = weighted linear combination. Returns (score, breakdown).

    Components:
      - distinct_members : N distinct watchers (dominant term)
      - recency          : 0 if oldest edge in window, 1 if at window_end
      - member_quality   : Phase-2 placeholder (Bayesian precision); 0 in Phase 1
    """
    recency_value = 0.0
    if newest_edge_iso and window_days > 0:
        try:
            newest = _parse_iso(newest_edge_iso)
            end = _parse_iso(window_end_iso)
            age_days = max(0.0, (end - newest).total_seconds() / 86400.0)
            recency_value = max(0.0, 1.0 - (age_days / window_days))
        except (ValueError, TypeError):
            pass

    breakdown = {
        "distinct_members": float(distinct_member_count) * weight_distinct_members,
        "recency":          recency_value * weight_recency,
        "member_quality":   member_quality_value * weight_member_quality,
    }
    score = sum(breakdown.values())
    return score, breakdown


def _parse_iso(s: str) -> datetime:
    """Parse an ISO-8601 string. Handles trailing 'Z' and Neo4j nanosecond fractions."""
    s = s.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    # Truncate fractional seconds to 6 digits (Python's max)
    if "." in s:
        head, _, tail = s.partition(".")
        # tail looks like "123456789+00:00" — keep up to 6 fractional digits, then tz
        frac = ""
        rest = tail
        for ch in tail:
            if ch.isdigit():
                frac += ch
                rest = rest[1:]
            else:
                break
        frac = frac[:6].ljust(6, "0")
        s = f"{head}.{frac}{rest}"
    return datetime.fromisoformat(s)


# --- Aggregation -----------------------------------------------------------

def aggregate(rows: list[dict[str, Any]],
              user_id: str,
              window_start: str,
              window_end: str,
              window_days: int,
              rule) -> list[ConvergenceEvent]:
    """Group raw signal rows by target and produce ConvergenceEvent objects.

    `rule` is an intelligence.rule.AlertRule (kept untyped here to avoid a
    circular import). Filters by signal_types, applies min_distinct_watchers,
    weights via compute_score, sorts by rule.sort_by, truncates by rule.limit.
    """
    allowed_signals = set(rule.signal_types)

    by_target: dict[str, dict[str, Any]] = {}
    for r in rows:
        target = r.get("target")
        if target is None:
            continue
        signal_type = r.get("signal_type")
        if signal_type and signal_type not in allowed_signals:
            continue

        target_id = target["canonical_id"]
        target_name = target.get("display_name") or target_id

        bucket = by_target.setdefault(target_id, {
            "target_id": target_id,
            "target_name": target_name,
            "members": {},
            "evidence": [],
            "newest_iso": None,
            "oldest_iso": None,
            "signal_type_counts": {},
        })

        watcher = r.get("w") or {}
        watcher_id = watcher["canonical_id"]
        watcher_name = watcher.get("display_name") or watcher_id
        bucket["members"][watcher_id] = watcher_name

        edge_iso = _iso_str(r.get("edge_at"))
        bucket["evidence"].append({
            "watcher_id":   watcher_id,
            "watcher_name": watcher_name,
            "signal_type":  signal_type,
            "edge_at":      edge_iso,
            "repo_full_name": r.get("repo_full_name"),
            "repo_url":       r.get("repo_url"),
        })

        if signal_type:
            bucket["signal_type_counts"][signal_type] = bucket["signal_type_counts"].get(signal_type, 0) + 1

        if edge_iso:
            if bucket["newest_iso"] is None or edge_iso > bucket["newest_iso"]:
                bucket["newest_iso"] = edge_iso
            if bucket["oldest_iso"] is None or edge_iso < bucket["oldest_iso"]:
                bucket["oldest_iso"] = edge_iso

    fired_at = datetime.now(timezone.utc).isoformat()
    events: list[ConvergenceEvent] = []
    for tid, b in by_target.items():
        n = len(b["members"])
        if n < rule.min_distinct_watchers:
            continue
        member_ids = list(b["members"].keys())
        member_names = list(b["members"].values())
        score, breakdown = compute_score(
            n, b["newest_iso"], window_end, window_days,
            weight_distinct_members=rule.weight_distinct_members,
            weight_recency=rule.weight_recency,
            weight_member_quality=rule.weight_member_quality,
            member_quality_value=0.0,    # Phase 2 hook
        )
        if score < rule.min_score:
            continue
        events.append(ConvergenceEvent(
            target_id=tid,
            target_name=b["target_name"],
            user_id=user_id,
            fired_at=fired_at,
            window_start=window_start,
            window_end=window_end,
            distinct_member_count=n,
            member_ids=member_ids,
            member_names=member_names,
            score=score,
            score_breakdown=breakdown,
            first_signal_at=b["oldest_iso"],
            last_signal_at=b["newest_iso"],
            signal_type_counts=b["signal_type_counts"],
            evidence=b["evidence"],
        ))

    if rule.sort_by == "watcher_count":
        events.sort(key=lambda e: (-e.distinct_member_count, e.target_name))
    elif rule.sort_by == "recency":
        events.sort(key=lambda e: ((e.last_signal_at or ""), e.target_name), reverse=True)
    else:  # default: score
        events.sort(key=lambda e: (-e.score, e.target_name))

    return events[: rule.limit]


def _iso_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    if hasattr(value, "iso_format"):
        return value.iso_format()
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


# --- Live runner ------------------------------------------------------------

def find_convergences(driver, *, user_id: str = "demo",
                      as_of: Optional[datetime] = None,
                      rule=None) -> list[ConvergenceEvent]:
    """Compute ConvergenceEvents from the current Neo4j state.

    `rule` is an intelligence.rule.AlertRule. If None, uses the persisted rule
    for `user_id` (or DEFAULT_RULE if no rule saved).
    """
    if rule is None:
        from intelligence.rule import get_rule
        rule = get_rule(user_id)

    end = (as_of or datetime.now(timezone.utc)).astimezone(timezone.utc)
    start = end - timedelta(days=rule.window_days)
    window_start, window_end = start.isoformat(), end.isoformat()

    with driver.session() as session:
        rows = list(session.run(
            UNIFIED_CONVERGENCE_QUERY,
            user_id=user_id,
            window_start=window_start,
            window_end=window_end,
        ))

    raw: list[dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        target = d.get("target")
        watcher = d.get("w")
        raw.append({
            "target":         dict(target) if target else None,
            "w":              dict(watcher) if watcher else None,
            "edge_at":        d.get("edge_at"),
            "signal_type":    d.get("signal_type"),
            "repo_full_name": d.get("repo_full_name"),
            "repo_url":       d.get("repo_url"),
        })

    return aggregate(raw, user_id=user_id, window_start=window_start,
                     window_end=window_end, window_days=rule.window_days, rule=rule)


def persist_events(driver, events: list[ConvergenceEvent], *, user_id: str, window_end_iso: str) -> None:
    """Idempotently MERGE each event and prune stale ones for this window."""
    import json as _json
    with driver.session() as session:
        current_ids = [e.target_id for e in events]
        for e in events:
            session.run(
                UPSERT_CONVERGENCE_EVENT,
                id=e.event_id,
                target_id=e.target_id,
                user_id=user_id,
                fired_at=e.fired_at,
                window_start=e.window_start,
                window_end=e.window_end,
                distinct_member_count=e.distinct_member_count,
                member_ids=e.member_ids,
                score=e.score,
                score_breakdown_json=_json.dumps(e.score_breakdown, default=str),
                first_signal_at=e.first_signal_at,
                last_signal_at=e.last_signal_at,
                signal_type_counts_json=_json.dumps(e.signal_type_counts, default=str),
                evidence_json=_json.dumps(e.evidence, default=str),
            ).consume()
        session.run(
            DELETE_STALE_CONVERGENCE_EVENTS,
            user_id=user_id,
            window_end_date=window_end_iso[:10],
            current_target_ids=current_ids,
        ).consume()


# --- CLI --------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user", default="demo")
    parser.add_argument("--window", type=int, default=None, help="override AlertRule.window_days")
    parser.add_argument("--min-members", type=int, default=None, help="override AlertRule.min_distinct_watchers")
    parser.add_argument("--as-of", type=str, default=None,
                        help="ISO date for the window end (backtest mode)")
    parser.add_argument("--persist", action="store_true",
                        help="MERGE ConvergenceEvent nodes into Neo4j")
    parser.add_argument("--limit-print", type=int, default=20)
    parser.add_argument("--save-rule", action="store_true",
                        help="Save the resolved rule (with overrides) back to data/alert_rules.json")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    try:
        from dotenv import load_dotenv
        from neo4j import GraphDatabase
    except ImportError as exc:
        print(f"ERROR: missing dependency ({exc.name}). Run: pip install -r requirements.txt", file=sys.stderr)
        return 2

    load_dotenv(ROOT / ".env")
    from intelligence.rule import get_rule, save_rule

    rule = get_rule(args.user)
    # Apply CLI overrides
    if args.window is not None:
        rule.window_days = args.window
    if args.min_members is not None:
        rule.min_distinct_watchers = args.min_members
    errs = rule.validate()
    if errs:
        print("ERROR: invalid rule:", errs, file=sys.stderr)
        return 2
    if args.save_rule:
        save_rule(args.user, rule)
        print(f"Saved rule for user={args.user}")

    driver = GraphDatabase.driver(
        os.environ["NEO4J_URI"],
        auth=(os.environ["NEO4J_USER"], os.environ["NEO4J_PASSWORD"]),
    )

    as_of_dt = None
    if args.as_of:
        as_of_dt = datetime.fromisoformat(args.as_of)
        if as_of_dt.tzinfo is None:
            as_of_dt = as_of_dt.replace(tzinfo=timezone.utc)

    events = find_convergences(driver, user_id=args.user, as_of=as_of_dt, rule=rule)

    print(f"\nWindow: {(as_of_dt or datetime.now(timezone.utc)).isoformat()} - {rule.window_days}d back")
    print(f"Min members: {rule.min_distinct_watchers}  Signal types: {rule.signal_types}  Sort: {rule.sort_by}")
    print(f"Convergences fired: {len(events)}")
    print()
    print(f"{'#':<3} {'score':>7} {'N':>3} {'target':<30} watchers")
    print("-" * 100)
    for i, e in enumerate(events[:args.limit_print], start=1):
        watcher_str = ", ".join(e.member_names[:5])
        if len(e.member_names) > 5:
            watcher_str += f", +{len(e.member_names)-5} more"
        print(f"{i:<3} {e.score:>7.2f} {e.distinct_member_count:>3}  {e.target_name[:30]:<30} {watcher_str}")

    if args.persist:
        end = (as_of_dt or datetime.now(timezone.utc)).astimezone(timezone.utc)
        persist_events(driver, events, user_id=args.user, window_end_iso=end.isoformat())
        print(f"\nPersisted {len(events)} ConvergenceEvent nodes.")

    driver.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
