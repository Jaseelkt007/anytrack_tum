"""Map Neo4j rows into the frontend's TypeScript-compatible shapes.

The shapes mirror /mnt/d/signal-convergence/src/data/types.ts. Field names use
the same casing the frontend expects (camelCase) so JSON deserializes into the
existing TS types without an adapter layer.
"""

from __future__ import annotations

import hashlib
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
        return f"Followed by {n} watchlist members on GitHub"
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

def map_alert(row: dict[str, Any], *, window_days: int = 90) -> dict[str, Any]:
    founder = {
        "id":             row["founder_id"],
        "name":           row["founder_name"],
        "headline":       f"Followed by {row['distinct_watchers']} watchlist members on GitHub",
        "location":       "Unknown",
        "company":        None,
        "companyUrl":     None,
        "linkedinUrl":    None,
        "twitterHandle":  None,
        "githubUsername": row.get("github_handle"),
        "initials":       initials_of(row["founder_name"]),
        "avatarColor":    avatar_color_for(row["founder_id"]),
    }
    signals = [
        map_signal(
            founder_id=row["founder_id"],
            founder_handle=row.get("github_handle"),
            signal=s,
        )
        for s in (row.get("signals") or [])
    ]
    triggered = max((s["occurredAt"] for s in signals if s["occurredAt"]), default=None) or _now_iso()

    return {
        "id":          f"alert-{row['founder_id'][:8]}",
        "founder":     founder,
        "signals":     signals,
        "windowDays":  window_days,
        "triggeredAt": triggered,
    }


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
