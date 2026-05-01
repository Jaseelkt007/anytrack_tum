"""AlertRule — the editable, per-user policy that drives convergence detection.

Persisted as JSON in `data/alert_rules.json`, keyed by user_id. Each user can
have their own rule. Missing users fall back to DEFAULT_RULE.

This is the only place where alert thresholds, signal-type filters, and
score weights live. Change a value here, re-run convergence (`POST
/api/alerts/recompute`) and every downstream surface (the alerts API, the
graph endpoint, the persisted ConvergenceEvent rows) updates accordingly.

Phase 1 fields are intentionally minimal. Phase 2 will add Bayesian-precision
weighting, archetype multipliers, sector/thesis match, etc. — every new
field gets a sane default so old rule files still load.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
RULES_FILE = ROOT / "data" / "alert_rules.json"

# Signal types the detector currently knows about. Extend as new edge types ship.
KNOWN_SIGNAL_TYPES = ("FOLLOWS_ON_GITHUB", "STARRED_REPO", "FOLLOWS_ON_TWITTER")
KNOWN_SORT_KEYS = ("score", "watcher_count", "recency")


@dataclass
class AlertRule:
    """A user-editable convergence policy. All fields have defaults."""

    # Trigger criteria
    min_distinct_watchers: int = 2
    window_days: int = 365
    signal_types: list[str] = field(default_factory=lambda: list(KNOWN_SIGNAL_TYPES))

    # Scoring weights (linear combination)
    weight_distinct_members: float = 1.0
    weight_recency: float = 1.0
    weight_member_quality: float = 0.0     # placeholder — M11 attaches Bayesian here
    # GitHub-prominence boost: bonus for targets who own a high-star repo. Per
    # Omar's "for OSS, when very high value people start a repo it's exceptional"
    # framing. Bonus is log-scaled and capped to avoid runaway weighting.
    weight_target_prominence: float = 1.0
    prominence_min_stars: int = 100        # below this, bonus = 0
    prominence_max_stars_cap: int = 10000  # at/above this, bonus is capped

    # Filters
    exclude_active_watchers: bool = True   # don't fire on a watcher being followed by other watchers
    min_score: float = 0.0
    role_tag_filter: list[str] = field(default_factory=list)  # empty = all roles
    twitter_signal_min_confidence: float = 0.0  # filter out FOLLOWS_ON_TWITTER edges below this confidence

    # Output shaping
    sort_by: str = "score"                  # one of KNOWN_SORT_KEYS
    limit: int = 100

    # Email digest config (M12-email). When `notify_email` is None or
    # `notify_enabled` is False, the scheduler's digest job is a no-op.
    notify_email: str | None = None
    notify_enabled: bool = True
    notify_daily_cap: int = 5                       # max dossiers per email
    notify_min_score: float = 7.0                   # convergence-event score floor
    notify_min_confidence: float = 0.90             # classifier confidence floor
    notify_classifications: list[str] = field(
        default_factory=lambda: ["founder"]
    )

    # --- Validation ----------------------------------------------------

    def validate(self) -> list[str]:
        """Return a list of human-readable error strings. Empty list = valid."""
        errs: list[str] = []
        if self.min_distinct_watchers < 1:
            errs.append("min_distinct_watchers must be >= 1")
        if self.window_days < 1:
            errs.append("window_days must be >= 1")
        if not self.signal_types:
            errs.append("signal_types must include at least one type")
        for st in self.signal_types:
            if st not in KNOWN_SIGNAL_TYPES:
                errs.append(f"unknown signal_type {st!r}; allowed: {list(KNOWN_SIGNAL_TYPES)}")
        if self.sort_by not in KNOWN_SORT_KEYS:
            errs.append(f"sort_by must be one of {list(KNOWN_SORT_KEYS)}")
        if self.limit < 1 or self.limit > 1000:
            errs.append("limit must be in [1, 1000]")
        for w_name in ("weight_distinct_members", "weight_recency",
                       "weight_member_quality", "weight_target_prominence"):
            if getattr(self, w_name) < 0:
                errs.append(f"{w_name} must be >= 0")
        if not (0.0 <= self.twitter_signal_min_confidence <= 1.0):
            errs.append("twitter_signal_min_confidence must be in [0, 1]")
        if self.prominence_min_stars < 0:
            errs.append("prominence_min_stars must be >= 0")
        if self.prominence_max_stars_cap < self.prominence_min_stars:
            errs.append("prominence_max_stars_cap must be >= prominence_min_stars")
        # Email digest validation
        if self.notify_email is not None:
            email = (self.notify_email or "").strip()
            if email and ("@" not in email or "." not in email or " " in email):
                errs.append(f"notify_email {email!r} is not a valid email format")
        if self.notify_daily_cap < 0:
            errs.append("notify_daily_cap must be >= 0")
        if not (0.0 <= self.notify_min_confidence <= 1.0):
            errs.append("notify_min_confidence must be in [0, 1]")
        if self.notify_min_score < 0:
            errs.append("notify_min_score must be >= 0")
        for c in self.notify_classifications:
            if c not in ("founder", "investor", "operator", "unclear", "not_relevant"):
                errs.append(f"unknown notify_classification {c!r}")
        return errs

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


DEFAULT_RULE = AlertRule()


# --- Persistence -----------------------------------------------------------

def load_rules() -> dict[str, AlertRule]:
    """Load all per-user rules from RULES_FILE. Returns empty dict if file missing."""
    if not RULES_FILE.exists():
        return {}
    try:
        raw = json.loads(RULES_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    out: dict[str, AlertRule] = {}
    for user_id, payload in (raw or {}).items():
        try:
            out[user_id] = _from_dict(payload)
        except (TypeError, ValueError):
            continue
    return out


def get_rule(user_id: str) -> AlertRule:
    """Get the rule for a user, falling back to defaults."""
    return load_rules().get(user_id, DEFAULT_RULE)


def save_rule(user_id: str, rule: AlertRule) -> None:
    """Persist rule for `user_id`. Validates before writing."""
    errs = rule.validate()
    if errs:
        raise ValueError(f"invalid rule: {'; '.join(errs)}")
    rules = load_rules()
    rules[user_id] = rule
    RULES_FILE.parent.mkdir(parents=True, exist_ok=True)
    RULES_FILE.write_text(json.dumps(
        {u: r.to_dict() for u, r in rules.items()},
        indent=2,
        sort_keys=True,
    ))


def _from_dict(d: dict[str, Any]) -> AlertRule:
    """Construct an AlertRule from a dict, ignoring unknown keys (forward-compat)."""
    known = {f.name for f in AlertRule.__dataclass_fields__.values()}
    filtered = {k: v for k, v in d.items() if k in known}
    return AlertRule(**filtered)


def update_rule_partial(user_id: str, patch: dict[str, Any]) -> AlertRule:
    """Apply a partial update to a user's rule. Used by the PUT endpoint."""
    current = get_rule(user_id)
    merged = {**current.to_dict(), **patch}
    new = _from_dict(merged)
    errs = new.validate()
    if errs:
        raise ValueError(f"invalid rule after patch: {'; '.join(errs)}")
    save_rule(user_id, new)
    return new
