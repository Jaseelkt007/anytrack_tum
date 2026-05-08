"""Convergence detection — the core intelligence layer.

A "convergence" fires when ≥ N distinct active watchers have signal edges to the
same target Person within a sliding time window. Three signal types contribute:

  1. FOLLOWS_ON_GITHUB  : watcher --[follow @ github]--> target Person
  2. STARRED_REPO       : watcher --[star @ github]--> Repository <-- target Person (owner)
  3. FOLLOWS_ON_TWITTER : watcher --[follow @ twitter]--> target Person
                          (with confidence threshold; broader watcher pool)

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
import asyncio
import logging
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

logger = logging.getLogger("intelligence.convergence")


# --- Data class -------------------------------------------------------------

@dataclass
class ConvergenceEvent:
    """One convergence signal. Persisted as a row in convergence_event."""

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
        # Stable id so re-runs UPSERT the same event
        return f"cv-{self.user_id}-{self.target_id}-{self.window_end[:10]}"


# --- SQL CTEs ---------------------------------------------------------------
# Three UNIONed branches mirror the previous Cypher: github-follow,
# github-star (via repo owner), and twitter-follow (with confidence + broader
# watcher pool). Returns flat columns; aggregate() does the per-target rollup.

UNIFIED_CONVERGENCE_SQL = text("""
WITH window_signals AS (
    -- Branch 1: github follow signals (active watchers only)
    SELECT
        ee.target_person_id              AS target_id,
        tp.display_name                  AS target_name,
        ee.watcher_person_id             AS watcher_id,
        wp.display_name                  AS watcher_name,
        ee.observed_at                   AS edge_at,
        'FOLLOWS_ON_GITHUB'              AS signal_type,
        NULL::text                       AS repo_full_name,
        NULL::text                       AS repo_url,
        ee.evidence_url                  AS evidence_url,
        ee.edge_confidence               AS edge_confidence
    FROM edge_event ee
    JOIN watchlist_member w
      ON w.person_id = ee.watcher_person_id
     AND w.user_id   = :user_id
     AND w.tier      = 'active'
    JOIN person tp ON tp.id = ee.target_person_id
    JOIN person wp ON wp.id = ee.watcher_person_id
    WHERE ee.target_kind = 'person'
      AND ee.source      = 'github'
      AND ee.action_type = 'follow'
      AND ee.observed_at BETWEEN :window_start AND :window_end
      AND ee.org_id = :org_id
      AND COALESCE(tp.entity_type, 'User') = 'User'
      AND NOT EXISTS (
          SELECT 1 FROM watchlist_member wx
          WHERE wx.person_id = ee.target_person_id
            AND wx.user_id   = :user_id
            AND wx.tier      IN ('active','vip')
      )

    UNION ALL

    -- Branch 2: github star signals attributed to the repo owner
    SELECT
        ro.owner_person_id               AS target_id,
        tp.display_name                  AS target_name,
        ee.watcher_person_id             AS watcher_id,
        wp.display_name                  AS watcher_name,
        ee.observed_at                   AS edge_at,
        'STARRED_REPO'                   AS signal_type,
        r.full_name                      AS repo_full_name,
        r.html_url                       AS repo_url,
        r.html_url                       AS evidence_url,
        ee.edge_confidence               AS edge_confidence
    FROM edge_event ee
    JOIN repository r        ON r.github_id = ee.target_repo_id
    JOIN repository_owner ro ON ro.repo_id  = r.github_id
    JOIN watchlist_member w
      ON w.person_id = ee.watcher_person_id
     AND w.user_id   = :user_id
     AND w.tier      = 'active'
    JOIN person tp ON tp.id = ro.owner_person_id
    JOIN person wp ON wp.id = ee.watcher_person_id
    WHERE ee.target_kind = 'repository'
      AND ee.source      = 'github'
      AND ee.action_type = 'star'
      AND ee.observed_at BETWEEN :window_start AND :window_end
      AND ee.org_id = :org_id
      AND ro.owner_person_id <> ee.watcher_person_id
      AND COALESCE(tp.entity_type, 'User') = 'User'
      AND NOT EXISTS (
          SELECT 1 FROM watchlist_member wx
          WHERE wx.person_id = ro.owner_person_id
            AND wx.user_id   = :user_id
            AND wx.tier      IN ('active','vip')
      )

    UNION ALL

    -- Branch 3: twitter follow signals — all tiered watchers (active/vip/reference)
    SELECT
        ee.target_person_id              AS target_id,
        tp.display_name                  AS target_name,
        ee.watcher_person_id             AS watcher_id,
        wp.display_name                  AS watcher_name,
        ee.observed_at                   AS edge_at,
        'FOLLOWS_ON_TWITTER'             AS signal_type,
        NULL::text                       AS repo_full_name,
        NULL::text                       AS repo_url,
        ee.evidence_url                  AS evidence_url,
        ee.edge_confidence               AS edge_confidence
    FROM edge_event ee
    JOIN watchlist_member w
      ON w.person_id = ee.watcher_person_id
     AND w.user_id   = :user_id
    JOIN person tp ON tp.id = ee.target_person_id
    JOIN person wp ON wp.id = ee.watcher_person_id
    WHERE ee.target_kind = 'person'
      AND ee.source      = 'twitter'
      AND ee.action_type = 'follow'
      AND ee.observed_at BETWEEN :window_start AND :window_end
      AND ee.org_id = :org_id
      AND COALESCE(ee.edge_confidence, 1.0) >= :twitter_min_confidence
      AND COALESCE(tp.entity_type, 'User') = 'User'
      AND NOT EXISTS (
          SELECT 1 FROM watchlist_member wx
          WHERE wx.person_id = ee.target_person_id
            AND wx.user_id   = :user_id
            AND wx.tier      IN ('active','vip')
      )
)
SELECT * FROM window_signals
""")


TARGET_PROMINENCE_SQL = text("""
SELECT
    p.id                                       AS id,
    COALESCE(MAX(r.star_count_observed), 0)    AS max_stars
FROM person p
LEFT JOIN repository_owner ro ON ro.owner_person_id = p.id
LEFT JOIN repository r        ON r.github_id        = ro.repo_id
WHERE p.id = ANY(:ids)
GROUP BY p.id
""")


# --- Pure-function scoring (testable without DB) ---------------------------

def compute_target_prominence(max_owned_repo_stars: int,
                              *, min_stars: int = 100,
                              max_cap: int = 10000) -> float:
    """Log-scaled bonus from the target's most prominent owned repo.

      stars < min_stars  -> 0.0       (noise floor)
      stars = 100        -> ~1.0      (log10(101) - 1)
      stars = 1000       -> ~2.0
      stars = 10000      -> 3.0       (capped at log10(max_cap+1) - 1)
      stars > max_cap    -> capped value (does not grow further)

    Captures Omar's "very high value OSS == exceptional signal" framing while
    preventing 100K-star repos from dominating the inbox.
    """
    import math
    if max_owned_repo_stars < min_stars:
        return 0.0
    cap_value = math.log10(1 + max_cap) - 1.0
    raw = math.log10(1 + max_owned_repo_stars) - 1.0
    return max(0.0, min(cap_value, raw))


def compute_score(distinct_member_count: int,
                  newest_edge_iso: Optional[str],
                  window_end_iso: str,
                  window_days: int,
                  *,
                  weight_distinct_members: float = 1.0,
                  weight_recency: float = 1.0,
                  weight_member_quality: float = 0.0,
                  member_quality_value: float = 0.0,
                  weight_target_prominence: float = 0.0,
                  target_prominence_value: float = 0.0) -> tuple[float, dict[str, float]]:
    """Score = weighted linear combination. Returns (score, breakdown).

    Components:
      - distinct_members  : N distinct watchers (dominant term)
      - recency           : 0 if newest edge at window_start, 1 if at window_end
      - member_quality    : per-watcher Bayesian precision (M11 — placeholder 0 today)
      - target_prominence : log-scaled bonus from target's max owned-repo stars (M12.5)
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
        "distinct_members":  float(distinct_member_count) * weight_distinct_members,
        "recency":           recency_value * weight_recency,
        "member_quality":    member_quality_value * weight_member_quality,
        "target_prominence": target_prominence_value * weight_target_prominence,
    }
    score = sum(breakdown.values())
    return score, breakdown


def _parse_iso(s: str) -> datetime:
    """Parse an ISO-8601 string. Handles trailing 'Z' and nanosecond fractions."""
    s = s.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    if "." in s:
        head, _, tail = s.partition(".")
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
              rule,
              *,
              target_prominence_stars: dict[str, int] | None = None) -> list[ConvergenceEvent]:
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
            "evidence_url":   r.get("evidence_url"),
            "edge_confidence": r.get("edge_confidence"),
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
        prominence_stars = (target_prominence_stars or {}).get(tid, 0)
        prominence_value = compute_target_prominence(
            prominence_stars,
            min_stars=rule.prominence_min_stars,
            max_cap=rule.prominence_max_stars_cap,
        )
        score, breakdown = compute_score(
            n, b["newest_iso"], window_end, window_days,
            weight_distinct_members=rule.weight_distinct_members,
            weight_recency=rule.weight_recency,
            weight_member_quality=rule.weight_member_quality,
            member_quality_value=0.0,
            weight_target_prominence=rule.weight_target_prominence,
            target_prominence_value=prominence_value,
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
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


# --- Runner (Postgres) ------------------------------------------------------

async def find_convergences(session: AsyncSession,
                             *,
                             user_id: str = "demo",
                             org_id: str = "demo",
                             as_of: Optional[datetime] = None,
                             rule=None) -> list[ConvergenceEvent]:
    """Compute ConvergenceEvents from the current Postgres state."""
    if rule is None:
        from intelligence.rule import get_rule
        rule = get_rule(user_id)

    end = (as_of or datetime.now(timezone.utc)).astimezone(timezone.utc)
    start = end - timedelta(days=rule.window_days)
    window_start, window_end = start.isoformat(), end.isoformat()

    result = await session.execute(
        UNIFIED_CONVERGENCE_SQL,
        {
            "org_id": org_id,
            "user_id": user_id,
            "window_start": start,
            "window_end": end,
            "twitter_min_confidence": rule.twitter_signal_min_confidence,
        },
    )
    raw_rows = list(result.mappings())

    raw: list[dict[str, Any]] = []
    target_ids: set[str] = set()
    for r in raw_rows:
        tid = str(r["target_id"])
        wid = str(r["watcher_id"])
        target_ids.add(tid)
        raw.append({
            "target": {"canonical_id": tid, "display_name": r["target_name"]},
            "w":      {"canonical_id": wid, "display_name": r["watcher_name"]},
            "edge_at":         r["edge_at"],
            "signal_type":     r["signal_type"],
            "repo_full_name":  r["repo_full_name"],
            "repo_url":        r["repo_url"],
            "evidence_url":    r["evidence_url"],
            "edge_confidence": r["edge_confidence"],
        })

    target_prominence_stars: dict[str, int] = {}
    if target_ids:
        prom_result = await session.execute(
            TARGET_PROMINENCE_SQL,
            {"ids": list(target_ids)},
        )
        target_prominence_stars = {
            str(row["id"]): int(row["max_stars"] or 0)
            for row in prom_result.mappings()
        }

    return aggregate(
        raw,
        user_id=user_id,
        window_start=window_start,
        window_end=window_end,
        window_days=rule.window_days,
        rule=rule,
        target_prominence_stars=target_prominence_stars,
    )


async def persist_events(session: AsyncSession, events: list[ConvergenceEvent], *,
                          user_id: str, org_id: str, window_end_iso: str) -> None:
    """Idempotently UPSERT each event and prune stale ones for this window."""
    from db.models import ConvergenceEventRow

    current_ids: list[str] = []
    for e in events:
        current_ids.append(e.target_id)
        stmt = (
            pg_insert(ConvergenceEventRow)
            .values(
                id=e.event_id,
                org_id=org_id,
                target_person_id=e.target_id,
                fired_at=_parse_iso(e.fired_at),
                window_start=_parse_iso(e.window_start),
                window_end=_parse_iso(e.window_end),
                distinct_member_count=e.distinct_member_count,
                member_person_ids=e.member_ids,
                score=e.score,
                score_breakdown=e.score_breakdown,
                first_signal_at=_parse_iso(e.first_signal_at) if e.first_signal_at else None,
                last_signal_at=_parse_iso(e.last_signal_at) if e.last_signal_at else None,
                signal_type_counts=e.signal_type_counts,
                evidence=e.evidence,
            )
            .on_conflict_do_update(
                index_elements=["id"],
                set_={
                    "fired_at": _parse_iso(e.fired_at),
                    "distinct_member_count": e.distinct_member_count,
                    "member_person_ids": e.member_ids,
                    "score": e.score,
                    "score_breakdown": e.score_breakdown,
                    "evidence": e.evidence,
                    "first_signal_at": _parse_iso(e.first_signal_at) if e.first_signal_at else None,
                    "last_signal_at": _parse_iso(e.last_signal_at) if e.last_signal_at else None,
                    "signal_type_counts": e.signal_type_counts,
                },
            )
        )
        await session.execute(stmt)

    # Prune stale events for this window: targets that now sit on the watchlist
    # OR current-window events whose target dropped out of the candidate set.
    await session.execute(
        text("""
            DELETE FROM convergence_event ce
            WHERE ce.org_id = :org_id
              AND (
                EXISTS (
                  SELECT 1 FROM watchlist_member wx
                  WHERE wx.person_id = ce.target_person_id
                    AND wx.user_id   = :user_id
                    AND wx.tier      IN ('active','vip')
                )
                OR (
                  ce.id LIKE :prefix
                  AND NOT (ce.target_person_id::text = ANY(:current_ids))
                )
              )
        """),
        {
            "org_id": org_id,
            "user_id": user_id,
            "prefix": f"%{window_end_iso[:10]}",
            "current_ids": current_ids,
        },
    )

    await session.commit()


# --- CLI --------------------------------------------------------------------

async def _run_cli(args: argparse.Namespace) -> int:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")

    from db.engine import dispose_engine, session_scope
    from intelligence.rule import get_rule, save_rule

    rule = get_rule(args.user)
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

    as_of_dt: Optional[datetime] = None
    if args.as_of:
        as_of_dt = datetime.fromisoformat(args.as_of)
        if as_of_dt.tzinfo is None:
            as_of_dt = as_of_dt.replace(tzinfo=timezone.utc)

    async with session_scope() as session:
        events = await find_convergences(
            session,
            user_id=args.user,
            org_id=args.org,
            as_of=as_of_dt,
            rule=rule,
        )

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
            await persist_events(session, events, user_id=args.user, org_id=args.org,
                                  window_end_iso=end.isoformat())
            print(f"\nPersisted {len(events)} ConvergenceEvent rows.")

    await dispose_engine()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user", default="demo")
    parser.add_argument("--org", default="demo")
    parser.add_argument("--window", type=int, default=None, help="override AlertRule.window_days")
    parser.add_argument("--min-members", type=int, default=None, help="override AlertRule.min_distinct_watchers")
    parser.add_argument("--as-of", type=str, default=None,
                        help="ISO date for the window end (backtest mode)")
    parser.add_argument("--persist", action="store_true",
                        help="UPSERT ConvergenceEvent rows into Postgres")
    parser.add_argument("--limit-print", type=int, default=20)
    parser.add_argument("--save-rule", action="store_true",
                        help="Save the resolved rule (with overrides) back to alert_rule")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    return asyncio.run(_run_cli(args))


if __name__ == "__main__":
    sys.exit(main())
