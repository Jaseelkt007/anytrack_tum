"""Convergence scoring v2 — composable components.

Pure functions only. The runner (intelligence.convergence.aggregate) wires
these together. Replacing this file is how we ship the moat: every change
to scoring goes through here.

Final formula:

    for each (watcher, signal):
        weight   = watcher_weight(archetype, override)
        decay    = 0.5 ** (age_days / source_half_life_days)
        surprise = log1p((pop + alpha) / (watcher_outbound_count + beta))
        contrib  = weight * decay * surprise

    independence: signals on the same target+source within
                  rule.independence_window_minutes collapse to max-contrib

    raw          = sum(collapsed_contribs)
    founder_prior = 1 + log10(max(1, max_owned_repo_stars / prominence_min)) capped
    score        = raw * founder_prior

This sits behind sub-project #3 in docs/superpowers/specs/2026-05-08-anytrace-v2-architecture-design.md §4.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable


# --- watcher weight ---------------------------------------------------------

def watcher_weight(
    *,
    archetype: str | None,
    archetype_weights: dict[str, float],
    override: float | None = None,
    default: float = 1.0,
) -> float:
    """Per-watcher multiplier. Override > archetype lookup > default."""
    if override is not None:
        return max(0.0, float(override))
    if archetype and archetype in archetype_weights:
        return max(0.0, float(archetype_weights[archetype]))
    return default


# --- time decay -------------------------------------------------------------

def time_decay(
    *,
    observed_at: datetime | None,
    window_end: datetime,
    half_life_days: float,
) -> float:
    """Exponential decay: fresh = ~1.0; one half-life ago = 0.5."""
    if observed_at is None or half_life_days <= 0:
        return 0.0
    age_seconds = (window_end - observed_at).total_seconds()
    age_days = max(0.0, age_seconds / 86400.0)
    return 0.5 ** (age_days / half_life_days)


def half_life_for(source: str, action_type: str, *,
                   half_lives: dict[str, float], default: float) -> float:
    """Lookup half-life in days for a (source, action_type) pair."""
    key = f"{source}_{action_type}"
    return float(half_lives.get(key, default))


# --- surprise (Bayesian-smoothed base rate) --------------------------------

def surprise_factor(
    *,
    watcher_outbound_count: int,
    population_size: int,
    alpha: float,
    beta: float,
) -> float:
    """A high-volume watcher (follows 10K accounts) generates lower surprise
    per signal than a selective watcher (follows 80 accounts).

    Returns log1p((pop + alpha) / (watcher_outbound + beta)). Always > 0.

    Examples (pop=100k, alpha=1, beta=50):
      watcher_outbound = 50    -> log1p(100001/100)   ≈ 6.9
      watcher_outbound = 1_000 -> log1p(100001/1050)  ≈ 4.6
      watcher_outbound = 10000 -> log1p(100001/10050) ≈ 2.4
    """
    numer = population_size + alpha
    denom = max(1.0, watcher_outbound_count + beta)
    return math.log1p(numer / denom)


# --- independence cluster ---------------------------------------------------

@dataclass
class Contribution:
    """One (watcher → target) contribution before clustering."""
    watcher_id: str
    target_id: str
    source: str
    observed_at: datetime | None
    contrib: float


def collapse_clusters(
    contribs: Iterable[Contribution],
    *,
    window_minutes: int,
) -> list[Contribution]:
    """Collapse same-target+source signals observed within `window_minutes`
    of each other into one contribution (the max). This dampens viral cascades
    where many watchers follow/star within the same hour.

    `window_minutes` <= 0 disables clustering.
    """
    items = list(contribs)
    if window_minutes <= 0 or not items:
        return items

    # Group by (target, source). Within each group, sort by time and bucket.
    by_key: dict[tuple[str, str], list[Contribution]] = {}
    for c in items:
        by_key.setdefault((c.target_id, c.source), []).append(c)

    out: list[Contribution] = []
    window_seconds = window_minutes * 60
    for group in by_key.values():
        group.sort(key=lambda c: (c.observed_at or datetime.min))
        cluster: list[Contribution] = []
        cluster_start: datetime | None = None
        for c in group:
            if (cluster_start is None
                    or c.observed_at is None
                    or (c.observed_at - cluster_start).total_seconds() > window_seconds):
                if cluster:
                    out.append(_pick_max(cluster))
                cluster = [c]
                cluster_start = c.observed_at
            else:
                cluster.append(c)
        if cluster:
            out.append(_pick_max(cluster))
    return out


def _pick_max(cluster: list[Contribution]) -> Contribution:
    """Within a cluster of same-event signals, the strongest one wins."""
    return max(cluster, key=lambda c: c.contrib)


# --- founder prior ----------------------------------------------------------

def founder_prior_multiplier(
    *,
    max_owned_repo_stars: int,
    min_stars: int = 100,
    max_cap: int = 10_000,
) -> float:
    """Multiplicative bonus.

      stars < min_stars  -> 1.0  (no bonus, neutral)
      stars = 100        -> ~1.0
      stars = 1000       -> ~3.0
      stars >= max_cap   -> ~4.0 (capped — log10(max_cap)+1)
    """
    if max_owned_repo_stars < min_stars:
        return 1.0
    cap_bonus = math.log10(1 + max_cap) - 1.0
    raw_bonus = math.log10(1 + max_owned_repo_stars) - 1.0
    return 1.0 + max(0.0, min(cap_bonus, raw_bonus))


# --- top-level v2 score -----------------------------------------------------

@dataclass
class ScoreInputs:
    """Everything aggregate() collects per (target × signal) — fed into score_v2."""

    target_id: str
    contributions: list[Contribution] = field(default_factory=list)
    max_owned_repo_stars: int = 0


@dataclass
class ScoreOutput:
    score: float
    raw: float
    founder_prior: float
    cluster_count: int
    breakdown: dict[str, float]


def score_v2(inputs: ScoreInputs, *, rule, prior: dict | None = None) -> ScoreOutput:
    """Wire all components together for one target.

    `rule` is an intelligence.rule.AlertRule. `prior` is an optional override
    bag for testing (lets tests fix prior multipliers without touching star
    counts).
    """
    collapsed = collapse_clusters(
        inputs.contributions,
        window_minutes=rule.independence_window_minutes,
    )
    raw = sum(c.contrib for c in collapsed)
    if prior is not None and "founder_prior" in prior:
        fp = float(prior["founder_prior"])
    else:
        fp = founder_prior_multiplier(
            max_owned_repo_stars=inputs.max_owned_repo_stars,
            min_stars=rule.prominence_min_stars,
            max_cap=rule.prominence_max_stars_cap,
        )
    score = raw * fp
    return ScoreOutput(
        score=score,
        raw=raw,
        founder_prior=fp,
        cluster_count=len(collapsed),
        breakdown={
            "raw_sum_of_contribs":       raw,
            "founder_prior_multiplier":  fp,
            "raw_signal_count":          float(len(inputs.contributions)),
            "post_independence_count":   float(len(collapsed)),
        },
    )
