"""Pure-function tests for the v2 scoring components."""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

from intelligence.scoring import (
    Contribution,
    ScoreInputs,
    collapse_clusters,
    founder_prior_multiplier,
    half_life_for,
    score_v2,
    surprise_factor,
    time_decay,
    watcher_weight,
)


# --- watcher_weight ---------------------------------------------------------

def test_watcher_weight_uses_archetype_table():
    assert watcher_weight(
        archetype="angel_operator",
        archetype_weights={"angel_operator": 3.0, "vc_partner": 2.0},
    ) == 3.0


def test_watcher_weight_falls_back_to_default_for_unknown_archetype():
    assert watcher_weight(
        archetype="bogus",
        archetype_weights={"angel_operator": 3.0},
        default=1.5,
    ) == 1.5


def test_watcher_weight_override_wins_over_archetype():
    # Even with a high-archetype weight, a specific override takes precedence
    assert watcher_weight(
        archetype="angel_operator",
        archetype_weights={"angel_operator": 3.0},
        override=10.0,
    ) == 10.0


def test_watcher_weight_handles_negative_clamp():
    assert watcher_weight(
        archetype=None, archetype_weights={}, override=-5.0,
    ) == 0.0


# --- time_decay -------------------------------------------------------------

def test_time_decay_at_zero_age_is_one():
    now = datetime.now(timezone.utc)
    assert time_decay(observed_at=now, window_end=now, half_life_days=30) == 1.0


def test_time_decay_at_one_half_life_is_half():
    now = datetime.now(timezone.utc)
    one_hl_ago = now - timedelta(days=14)
    assert time_decay(observed_at=one_hl_ago, window_end=now, half_life_days=14) == 0.5


def test_time_decay_at_two_half_lives_is_quarter():
    now = datetime.now(timezone.utc)
    two_hl_ago = now - timedelta(days=28)
    decay = time_decay(observed_at=two_hl_ago, window_end=now, half_life_days=14)
    assert math.isclose(decay, 0.25, rel_tol=1e-9)


def test_time_decay_unknown_observed_returns_zero():
    now = datetime.now(timezone.utc)
    assert time_decay(observed_at=None, window_end=now, half_life_days=14) == 0.0


def test_half_life_for_uses_lookup():
    half_lives = {"github_follow": 30.0, "github_star": 14.0}
    assert half_life_for("github", "follow", half_lives=half_lives, default=999) == 30.0
    assert half_life_for("github", "star", half_lives=half_lives, default=999) == 14.0
    # missing falls back
    assert half_life_for("twitter", "like", half_lives=half_lives, default=42) == 42


# --- surprise_factor --------------------------------------------------------

def test_surprise_high_volume_watcher_lower_than_selective():
    selective = surprise_factor(
        watcher_outbound_count=50, population_size=100_000, alpha=1, beta=50,
    )
    noisy = surprise_factor(
        watcher_outbound_count=10_000, population_size=100_000, alpha=1, beta=50,
    )
    assert selective > noisy
    # selective is roughly log1p(100001/100) ≈ 6.9
    assert 6.5 < selective < 7.5
    # noisy is roughly log1p(100001/10050) ≈ 2.4
    assert 2.0 < noisy < 3.0


# --- collapse_clusters ------------------------------------------------------

def test_collapse_clusters_collapses_within_window():
    base = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    contribs = [
        Contribution("w1", "t", "github", base, contrib=2.0, cluster_eligible=True),
        Contribution("w2", "t", "github", base + timedelta(minutes=10), contrib=5.0, cluster_eligible=True),
        Contribution("w3", "t", "github", base + timedelta(minutes=20), contrib=3.0, cluster_eligible=True),
    ]
    out = collapse_clusters(contribs, window_minutes=60)
    # All three within the same hour → one bucket, max 5.0
    assert len(out) == 1
    assert out[0].contrib == 5.0


def test_collapse_clusters_does_not_collapse_ineligible_contributions():
    """Follows carry crawl-time, not real timestamps. They must pass through."""
    base = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    contribs = [
        Contribution("w1", "t", "github", base, contrib=2.0, cluster_eligible=False),
        Contribution("w2", "t", "github", base, contrib=5.0, cluster_eligible=False),
    ]
    out = collapse_clusters(contribs, window_minutes=60)
    # Both ineligible → both pass through
    assert len(out) == 2
    assert sorted(c.contrib for c in out) == [2.0, 5.0]


def test_collapse_clusters_keeps_separate_clusters_apart():
    base = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    contribs = [
        Contribution("w1", "t", "github", base, contrib=2.0, cluster_eligible=True),
        Contribution("w2", "t", "github", base + timedelta(hours=2), contrib=5.0, cluster_eligible=True),
    ]
    out = collapse_clusters(contribs, window_minutes=60)
    # Two clusters; both kept (max in each)
    assert len(out) == 2
    assert sorted(c.contrib for c in out) == [2.0, 5.0]


def test_collapse_clusters_disabled_when_window_zero():
    base = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    contribs = [
        Contribution("w1", "t", "github", base, contrib=2.0, cluster_eligible=True),
        Contribution("w2", "t", "github", base + timedelta(minutes=1), contrib=5.0, cluster_eligible=True),
    ]
    out = collapse_clusters(contribs, window_minutes=0)
    assert len(out) == 2  # no collapse


def test_collapse_clusters_separates_by_target_and_source():
    base = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    contribs = [
        Contribution("w1", "tA", "github", base, contrib=2.0, cluster_eligible=True),
        Contribution("w2", "tB", "github", base, contrib=3.0, cluster_eligible=True),
        Contribution("w3", "tA", "twitter", base, contrib=4.0, cluster_eligible=True),
    ]
    out = collapse_clusters(contribs, window_minutes=60)
    # Three distinct (target,source) groups → no collapse across them
    assert len(out) == 3


# --- founder_prior_multiplier ----------------------------------------------

def test_founder_prior_below_threshold_is_neutral():
    assert founder_prior_multiplier(max_owned_repo_stars=0) == 1.0
    assert founder_prior_multiplier(max_owned_repo_stars=50, min_stars=100) == 1.0


def test_founder_prior_grows_with_stars():
    p100 = founder_prior_multiplier(max_owned_repo_stars=100)
    p1k  = founder_prior_multiplier(max_owned_repo_stars=1_000)
    p10k = founder_prior_multiplier(max_owned_repo_stars=10_000)
    assert 1.0 <= p100 < p1k < p10k


def test_founder_prior_capped_above_max():
    p_at_cap   = founder_prior_multiplier(max_owned_repo_stars=10_000, max_cap=10_000)
    p_above    = founder_prior_multiplier(max_owned_repo_stars=100_000, max_cap=10_000)
    assert math.isclose(p_at_cap, p_above)


# --- score_v2 (composition) -------------------------------------------------

def _rule_with(**kwargs):
    """Build a minimal AlertRule-like object with just what score_v2 needs."""
    from intelligence.rule import AlertRule
    r = AlertRule()
    for k, v in kwargs.items():
        setattr(r, k, v)
    return r


def test_score_v2_zero_contributions_is_zero():
    sc = score_v2(ScoreInputs(target_id="t"), rule=_rule_with())
    assert sc.score == 0.0


def test_score_v2_applies_founder_prior_multiplicatively():
    rule = _rule_with(independence_window_minutes=0)
    contribs = [
        Contribution("w1", "t", "github", datetime.now(timezone.utc), contrib=10.0),
        Contribution("w2", "t", "github", datetime.now(timezone.utc), contrib=5.0),
    ]
    sc_no_stars = score_v2(
        ScoreInputs(target_id="t", contributions=contribs, max_owned_repo_stars=0),
        rule=rule,
    )
    sc_lots = score_v2(
        ScoreInputs(target_id="t", contributions=contribs, max_owned_repo_stars=5_000),
        rule=rule,
    )
    assert sc_no_stars.score == 15.0  # raw, prior=1.0
    assert sc_lots.score > sc_no_stars.score
    # prior is multiplicative: lots / no_stars ≈ founder_prior
    ratio = sc_lots.score / sc_no_stars.score
    assert math.isclose(ratio, sc_lots.founder_prior, rel_tol=1e-9)


def test_score_v2_independence_collapses_burst():
    base = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    contribs = [
        Contribution("w1", "t", "github", base, contrib=2.0, cluster_eligible=True),
        Contribution("w2", "t", "github", base + timedelta(minutes=5), contrib=3.0, cluster_eligible=True),
        Contribution("w3", "t", "github", base + timedelta(minutes=10), contrib=4.0, cluster_eligible=True),
    ]
    rule = _rule_with(independence_window_minutes=60)
    sc = score_v2(ScoreInputs(target_id="t", contributions=contribs), rule=rule)
    # All in same cluster → max contrib survives
    assert sc.raw == 4.0
    assert sc.cluster_count == 1


def test_score_v2_follows_are_not_collapsed_even_with_same_observed_at():
    """Crawl-time follows must remain independent — that is the entire point
    of the cluster_eligible flag."""
    crawl_time = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    contribs = [
        Contribution("w1", "t", "github", crawl_time, contrib=10.0, cluster_eligible=False),
        Contribution("w2", "t", "github", crawl_time, contrib=10.0, cluster_eligible=False),
    ]
    rule = _rule_with(independence_window_minutes=60)
    sc = score_v2(ScoreInputs(target_id="t", contributions=contribs), rule=rule)
    # Both pass through — raw = 20, not 10
    assert sc.raw == 20.0
    assert sc.cluster_count == 2
