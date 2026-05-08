"""Map Neo4j rows into the frontend's TypeScript-compatible shapes.

The shapes mirror /mnt/d/signal-convergence/src/data/types.ts. Field names use
the same casing the frontend expects (camelCase) so JSON deserializes into the
existing TS types without an adapter layer.
"""

from __future__ import annotations

import hashlib
import json as _json
from typing import Any, Optional

# investor_type (from CSV / Neo4j) → frontend tier
INVESTOR_TYPE_TO_TIER = {
    "Angel":                   "angel",
    "VC - Big fund":           "vc",
    "VC - Medium-Sized Fund":  "microvc",
    "VC - Small fund":         "microvc",
    "angel_operator":          "angel",   # our M2.5 augmentations
}


def tier_for(investor_type: Optional[str], archetype: Optional[str] = None) -> str:
    """Map Neo4j investor_type / archetype to one of 'vc' | 'microvc' | 'angel'."""
    if investor_type and investor_type in INVESTOR_TYPE_TO_TIER:
        return INVESTOR_TYPE_TO_TIER[investor_type]
    if archetype and archetype in INVESTOR_TYPE_TO_TIER:
        return INVESTOR_TYPE_TO_TIER[archetype]
    return "angel"


def initials_of(name: str) -> str:
    parts = [p for p in (name or "").strip().split() if p]
    return "".join(p[0] for p in parts[:2]).upper() or "?"


def avatar_color_for(seed: str) -> str:
    """Deterministic HSL string in the format expected by the frontend ('H S% L%')."""
    if not seed:
        seed = "fallback"
    digest = hashlib.sha1(seed.encode("utf-8")).digest()
    hue = digest[0] * 360 // 256                  # 0..359
    sat = 55 + (digest[1] % 25)                   # 55..79
    lit = 50 + (digest[2] % 15)                   # 50..64
    return f"{hue} {sat}% {lit}%"


def title_for(investor_type: Optional[str], archetype: Optional[str] = None) -> str:
    if archetype == "angel_operator":
        return "Angel · Operator"
    if investor_type:
        return investor_type
    return "Investor"


def group_for(country: Optional[str]) -> str:
    if not country:
        return "Unknown"
    # Trim long country names for the badge.
    return country if len(country) <= 18 else country[:15] + "…"


# --- Investor mapper --------------------------------------------------------

def map_investor(row: dict[str, Any]) -> dict[str, Any]:
    """Neo4j row → frontend Investor object."""
    return {
        "id":              row["id"],
        "name":            row["name"] or "Unknown",
        "title":           title_for(row.get("investor_type"), row.get("archetype")),
        "firm":            None,  # we don't track firm separately in Phase 1
        "tier":            tier_for(row.get("investor_type"), row.get("archetype")),
        "avatarColor":     avatar_color_for(row["id"]),
        "linkedinUrl":     row.get("linkedin_url"),
        "twitterHandle":   row.get("twitter_handle"),
        "githubUsername":  row.get("github_handle"),
        "group":           group_for(row.get("country")),
    }


# --- Founder mapper ---------------------------------------------------------

def map_founder(row: dict[str, Any]) -> dict[str, Any]:
    """Neo4j row → frontend Founder object."""
    name = row["name"] or row.get("github_handle") or "Unknown"
    handle = row.get("github_handle")
    return {
        "id":             row["id"],
        "name":           name,
        "headline":       _founder_headline(row),
        "location":       row.get("country") or "Unknown",
        "company":        None,
        "companyUrl":     None,
        "linkedinUrl":    row.get("linkedin_url"),
        "twitterHandle":  row.get("twitter_handle"),
        "githubUsername": handle,
        "initials":       initials_of(name),
        "avatarColor":    avatar_color_for(row["id"]),
    }


def _founder_headline(row: dict[str, Any]) -> str:
    n = row.get("watcher_count")
    if n:
        return f"Convergence: {n} watchlist members signaled on GitHub"
    return "GitHub-active founder candidate"


# --- Signal mapper ----------------------------------------------------------
# Each Cypher signal entry → frontend Signal object.

def map_signal(*, founder_id: str, founder_handle: Optional[str], signal: dict[str, Any]) -> dict[str, Any]:
    """One row from a convergence signals collection → frontend Signal."""
    edge_type = signal.get("edge_type") or signal.get("type")
    watcher_id = signal.get("watcher_id") or ""
    watcher_name = signal.get("watcher_name") or "Unknown"
    occurred = _iso(signal.get("first_seen_at"))

    if edge_type == "FOLLOWS_ON_GITHUB":
        target_label = f"followed @{founder_handle}" if founder_handle else f"followed {founder_id[:8]}"
        url = f"https://github.com/{founder_handle}" if founder_handle else "#"
        action = "followed"
    elif edge_type == "STARRED_REPO":
        repo = signal.get("repo_full_name") or "(unknown)"
        target_label = f"starred {repo}"
        url = signal.get("repo_html_url") or "#"
        action = "starred"
    else:
        target_label = "engaged"
        url = "#"
        action = "endorsed"

    return {
        "id":         f"sig-{watcher_id[:8]}-{founder_id[:8]}-{edge_type}",
        "investorId": watcher_id,
        "platform":   "github",
        "action":     action,
        "target":     f"{watcher_name} {target_label}",
        "url":        url,
        "occurredAt": occurred,
    }


# --- ConvergenceAlert mapper ------------------------------------------------

_PROMINENCE_LABEL = "GitHub repo prominence"


def explain_score(breakdown: dict, *,
                  distinct_member_count: int,
                  target_prominence_stars: int = 0) -> list[dict[str, Any]]:
    """Turn a raw score_breakdown dict into UI-friendly bullet items.

    Each item: { key, label, value, description }. The frontend can render
    this as a tooltip / expandable list without needing to know the formula.

    Reads the v2 score_breakdown produced by intelligence.scoring.score_v2:
      raw_sum_of_contribs, founder_prior_multiplier, distinct_member_count,
      raw_signal_count, post_independence_count, max_owned_repo_stars.
    Also tolerates the legacy v0.1 keys (distinct_members, recency,
    target_prominence, member_quality) so old persisted rows still render.
    """
    items: list[dict[str, Any]] = []
    n = int(breakdown.get("distinct_member_count")
            or breakdown.get("distinct_members")
            or distinct_member_count or 0)
    items.append({
        "key": "distinct_members",
        "label": "Distinct watchers",
        "value": float(n),
        "description": (
            f"{n} watcher{'s' if n != 1 else ''} from your network "
            f"engaged with this target within the time window"
        ),
    })

    raw_sum = float(breakdown.get("raw_sum_of_contribs") or 0.0)
    if raw_sum > 0:
        items.append({
            "key": "raw_sum",
            "label": "Weighted contribution",
            "value": raw_sum,
            "description": (
                "Sum of (watcher tier × time decay × surprise) across all signals"
            ),
        })

    raw_n = int(float(breakdown.get("raw_signal_count") or 0))
    post_n = int(float(breakdown.get("post_independence_count") or 0))
    if raw_n > post_n > 0:
        collapsed = raw_n - post_n
        items.append({
            "key": "independence",
            "label": "Independence collapse",
            "value": float(collapsed),
            "description": (
                f"{collapsed} signal{'s' if collapsed != 1 else ''} collapsed "
                "into a single contribution (same-event burst dampened)"
            ),
        })

    fp = float(breakdown.get("founder_prior_multiplier")
               or breakdown.get("target_prominence") or 0.0)
    stars = int(target_prominence_stars
                or float(breakdown.get("max_owned_repo_stars") or 0))
    if fp > 1.0 or stars > 0:
        items.append({
            "key": "founder_prior",
            "label": _PROMINENCE_LABEL,
            "value": fp if fp > 0 else 1.0,
            "description": (
                f"Target owns a repo with ~{stars:,} stars"
                if stars else
                "Target owns a high-star repository on GitHub"
            ),
        })

    return items


def map_alert(row: dict[str, Any], *, window_days: int = 90, rank: Optional[int] = None) -> dict[str, Any]:
    """Map a ConvergenceEvent row + decoded evidence_json into a frontend ConvergenceAlert.

    Returns the legacy ConvergenceAlert shape (for backwards compatibility with
    the original Lovable types) PLUS a `meta` object with the new fields:
    score, scoreBreakdown, rank, firstSignalAt, lastSignalAt, signalTypeCounts,
    windowStart, windowEnd. Frontends can adopt these incrementally.
    """
    import json
    founder = {
        "id":             row["founder_id"],
        "name":           row["founder_name"],
        "headline":       f"Convergence: {row['distinct_watchers']} watchlist members signaled",
        "location":       "Unknown",
        "company":        None,
        "companyUrl":     None,
        "linkedinUrl":    None,
        "twitterHandle":  None,
        "githubUsername": row.get("github_handle"),
        "initials":       initials_of(row["founder_name"]),
        "avatarColor":    avatar_color_for(row["founder_id"]),
    }

    evidence = _safe_json(row.get("evidence_json"), default=[])
    breakdown = _safe_json(row.get("score_breakdown_json"), default={})
    type_counts = _safe_json(row.get("signal_type_counts_json"), default={})

    # Frontend ConvergenceMeta.scoreBreakdown still types `distinct_members`,
    # `recency`, `member_quality` for backwards compatibility. Project them
    # from the v2 keys so existing Lovable UI panels keep rendering. We keep
    # the v2 keys alongside so new UIs can read the rich shape.
    distinct_members_compat = (
        breakdown.get("distinct_member_count")
        or breakdown.get("distinct_members")
        or row.get("distinct_watchers") or 0
    )
    breakdown = {
        **breakdown,
        "distinct_members": float(distinct_members_compat),
        # Recency is folded into per-contribution time decay in v2; expose 0
        # rather than fabricating a single number that no longer exists.
        "recency": float(breakdown.get("recency") or 0.0),
        "member_quality": float(breakdown.get("member_quality") or 0.0),
    }

    signals = []
    for ev in evidence:
        signal_type = ev.get("signal_type") or "FOLLOWS_ON_GITHUB"
        signals.append(map_signal(
            founder_id=row["founder_id"],
            founder_handle=row.get("github_handle"),
            signal={
                "edge_type":      signal_type,
                "watcher_id":     ev.get("watcher_id"),
                "watcher_name":   ev.get("watcher_name"),
                "first_seen_at":  ev.get("edge_at"),
                "repo_full_name": ev.get("repo_full_name"),
                "repo_html_url":  ev.get("repo_url"),
            },
        ))

    triggered = _iso(row.get("fired_at")) or max((s["occurredAt"] for s in signals if s["occurredAt"]), default=None) or _now_iso()

    return {
        "id":          f"alert-{row['founder_id'][:8]}",
        "founder":     founder,
        "signals":     signals,
        "windowDays":  window_days,
        "triggeredAt": triggered,
        # New rich fields — frontends can adopt incrementally.
        "meta": {
            "score":            float(row.get("score") or 0.0),
            "scoreBreakdown":   breakdown,
            "scoreExplanation": explain_score(
                breakdown,
                distinct_member_count=int(row.get("distinct_watchers") or 0),
                target_prominence_stars=int(row.get("target_prominence_stars") or 0),
            ),
            "rank":             rank,
            "distinctMembers":  int(row.get("distinct_watchers") or 0),
            "firstSignalAt":    _iso(row.get("first_signal_at")),
            "lastSignalAt":     _iso(row.get("last_signal_at")),
            "signalTypeCounts": type_counts,
            "windowStart":      _iso(row.get("window_start")),
            "windowEnd":        _iso(row.get("window_end")),
        },
    }


def _safe_json(value: Any, *, default: Any) -> Any:
    import json
    if value is None:
        return default
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


# --- Helpers ----------------------------------------------------------------

def _iso(value: Any) -> str:
    """Coerce Neo4j DateTime / Python datetime / string to ISO 8601 string."""
    if value is None:
        return ""
    if hasattr(value, "iso_format"):
        return value.iso_format()
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()
