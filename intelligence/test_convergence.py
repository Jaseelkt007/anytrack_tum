"""Unit tests for the pure-function pieces of intelligence.convergence.

No Neo4j needed — exercises compute_score and aggregate against a synthetic
mini-graph with 5 watchers and 3 targets where 2 should fire convergence
and 1 should not.

Run:
    python intelligence/test_convergence.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from intelligence.convergence import (
    aggregate,
    compute_score,
    compute_target_prominence,
    _parse_iso,
)
from intelligence.rule import AlertRule


def _rule(min_members: int = 2, **overrides) -> AlertRule:
    r = AlertRule(min_distinct_watchers=min_members)
    for k, v in overrides.items():
        setattr(r, k, v)
    return r


def test_compute_score_basic():
    s, breakdown = compute_score(distinct_member_count=3, newest_edge_iso=None,
                                 window_end_iso="2024-01-01T00:00:00+00:00", window_days=90)
    assert s == 3.0, f"expected 3.0, got {s}"
    assert breakdown["distinct_members"] == 3.0
    assert breakdown["recency"] == 0.0
    print("  OK  base score is just member count when no recency info")


def test_compute_score_recency_bonus_recent():
    s, _ = compute_score(distinct_member_count=2,
                         newest_edge_iso="2024-01-01T00:00:00+00:00",
                         window_end_iso="2024-01-01T00:00:00+00:00",
                         window_days=90)
    assert abs(s - 3.0) < 0.001, f"expected ~3.0, got {s}"
    print(f"  OK  recency bonus saturates at 1.0 for edges at window_end (score={s})")


def test_compute_score_recency_bonus_old():
    s, _ = compute_score(distinct_member_count=2,
                         newest_edge_iso="2023-10-03T00:00:00+00:00",
                         window_end_iso="2024-01-01T00:00:00+00:00",
                         window_days=90)
    assert 2.0 <= s <= 2.05, f"expected ~2.0, got {s}"
    print(f"  OK  recency bonus collapses to ~0 for edges at start of window (score={s})")


def test_compute_score_weights_applied():
    """Doubling weight_recency should double the recency contribution."""
    s_default, b1 = compute_score(2, "2024-01-01T00:00:00+00:00",
                                   "2024-01-01T00:00:00+00:00", 90,
                                   weight_distinct_members=1.0, weight_recency=1.0)
    s_doubled, b2 = compute_score(2, "2024-01-01T00:00:00+00:00",
                                   "2024-01-01T00:00:00+00:00", 90,
                                   weight_distinct_members=1.0, weight_recency=2.0)
    assert abs(s_doubled - s_default - 1.0) < 0.001, \
        f"expected delta of 1.0 from doubling recency weight, got {s_doubled - s_default}"
    print(f"  OK  weight_recency=2.0 adds 1.0 to score over the default")


def test_parse_iso_handles_neo4j_nanoseconds():
    # Neo4j returns 9-digit fractional seconds; Python's fromisoformat only takes 6
    dt = _parse_iso("2026-04-30T23:44:41.560962000+00:00")
    assert dt.year == 2026 and dt.minute == 44, f"unexpected: {dt}"
    print("  OK  ISO parser handles Neo4j 9-digit nanosecond fractions")


def test_aggregate_synthetic_fixture():
    """5 watchers, 3 targets: t1 fires (3 watchers), t2 fires (2 watchers),
    t3 does NOT fire (1 watcher only)."""

    def watcher(i: int) -> dict:
        return {"canonical_id": f"w{i}", "display_name": f"Watcher{i}"}

    def target(i: int) -> dict:
        return {"canonical_id": f"t{i}", "display_name": f"Target{i}"}

    rows = [
        # t1: 3 distinct watchers
        {"target": target(1), "w": watcher(1), "edge_at": "2024-01-15T00:00:00+00:00",
         "signal_type": "FOLLOWS_ON_GITHUB", "repo_full_name": None, "repo_url": None},
        {"target": target(1), "w": watcher(2), "edge_at": "2024-01-20T00:00:00+00:00",
         "signal_type": "FOLLOWS_ON_GITHUB", "repo_full_name": None, "repo_url": None},
        {"target": target(1), "w": watcher(3), "edge_at": "2024-02-01T00:00:00+00:00",
         "signal_type": "STARRED_REPO", "repo_full_name": "x/y", "repo_url": "https://github.com/x/y"},
        # t2: 2 distinct watchers
        {"target": target(2), "w": watcher(2), "edge_at": "2024-01-10T00:00:00+00:00",
         "signal_type": "FOLLOWS_ON_GITHUB", "repo_full_name": None, "repo_url": None},
        {"target": target(2), "w": watcher(4), "edge_at": "2024-01-12T00:00:00+00:00",
         "signal_type": "FOLLOWS_ON_GITHUB", "repo_full_name": None, "repo_url": None},
        # t3: 1 watcher only — must NOT fire
        {"target": target(3), "w": watcher(5), "edge_at": "2024-01-25T00:00:00+00:00",
         "signal_type": "FOLLOWS_ON_GITHUB", "repo_full_name": None, "repo_url": None},
        # noise: same (w2,t2) edge appearing twice — shouldn't double-count members
        {"target": target(2), "w": watcher(2), "edge_at": "2024-01-11T00:00:00+00:00",
         "signal_type": "STARRED_REPO", "repo_full_name": "z/q", "repo_url": "#"},
    ]

    events = aggregate(
        rows,
        user_id="demo",
        window_start="2024-01-01T00:00:00+00:00",
        window_end="2024-03-01T00:00:00+00:00",
        window_days=60,
        rule=_rule(min_members=2),
    )

    by_id = {e.target_id: e for e in events}
    assert "t1" in by_id, "t1 should fire (3 watchers)"
    assert "t2" in by_id, "t2 should fire (2 watchers)"
    assert "t3" not in by_id, "t3 should NOT fire (only 1 watcher)"
    assert by_id["t1"].distinct_member_count == 3, by_id["t1"].distinct_member_count
    assert by_id["t2"].distinct_member_count == 2, by_id["t2"].distinct_member_count
    # t1 has higher score than t2 because more members
    assert by_id["t1"].score > by_id["t2"].score
    # ordering: t1 first (higher score)
    assert events[0].target_id == "t1"
    # evidence preserved: 3 entries for t1, 3 for t2 (incl the duplicate STARRED row)
    assert len(by_id["t1"].evidence) == 3
    assert len(by_id["t2"].evidence) == 3
    print(f"  OK  synthetic fixture: t1 fires (N=3, score={by_id['t1'].score:.2f}), "
          f"t2 fires (N=2, score={by_id['t2'].score:.2f}), t3 does NOT fire")


def test_aggregate_event_id_is_stable():
    """event_id derived from user/target/window_end — must be stable across runs."""
    rows = [
        {"target": {"canonical_id": "t1", "display_name": "T1"},
         "w": {"canonical_id": "w1", "display_name": "W1"},
         "edge_at": "2024-01-15T00:00:00+00:00",
         "signal_type": "FOLLOWS_ON_GITHUB", "repo_full_name": None, "repo_url": None},
        {"target": {"canonical_id": "t1", "display_name": "T1"},
         "w": {"canonical_id": "w2", "display_name": "W2"},
         "edge_at": "2024-01-15T00:00:00+00:00",
         "signal_type": "FOLLOWS_ON_GITHUB", "repo_full_name": None, "repo_url": None},
    ]
    e1 = aggregate(rows, user_id="demo",
                   window_start="2024-01-01T00:00:00+00:00",
                   window_end="2024-03-01T00:00:00+00:00",
                   window_days=60, rule=_rule(min_members=2))[0]
    e2 = aggregate(rows, user_id="demo",
                   window_start="2024-01-01T00:00:00+00:00",
                   window_end="2024-03-01T00:00:00+00:00",
                   window_days=60, rule=_rule(min_members=2))[0]
    assert e1.event_id == e2.event_id, f"event_id differs: {e1.event_id} vs {e2.event_id}"
    print(f"  OK  event_id is stable: {e1.event_id}")


def test_aggregate_filters_signal_types():
    """If rule.signal_types excludes STARRED_REPO, watchers contributing only stars are dropped."""
    rows = [
        # t1 has 2 watchers but each only via STARRED_REPO; should NOT fire when only FOLLOWS allowed
        {"target": {"canonical_id": "t1", "display_name": "T1"},
         "w": {"canonical_id": "w1", "display_name": "W1"},
         "edge_at": "2024-01-15T00:00:00+00:00",
         "signal_type": "STARRED_REPO", "repo_full_name": "x/y", "repo_url": "#"},
        {"target": {"canonical_id": "t1", "display_name": "T1"},
         "w": {"canonical_id": "w2", "display_name": "W2"},
         "edge_at": "2024-01-16T00:00:00+00:00",
         "signal_type": "STARRED_REPO", "repo_full_name": "x/y", "repo_url": "#"},
    ]
    follows_only = _rule(min_members=2, signal_types=["FOLLOWS_ON_GITHUB"])
    events_filtered = aggregate(rows, user_id="demo",
                                window_start="2024-01-01T00:00:00+00:00",
                                window_end="2024-03-01T00:00:00+00:00",
                                window_days=60, rule=follows_only)
    assert events_filtered == [], "STARRED_REPO signals should be dropped when only FOLLOWS allowed"

    both = _rule(min_members=2, signal_types=["FOLLOWS_ON_GITHUB", "STARRED_REPO"])
    events_full = aggregate(rows, user_id="demo",
                            window_start="2024-01-01T00:00:00+00:00",
                            window_end="2024-03-01T00:00:00+00:00",
                            window_days=60, rule=both)
    assert len(events_full) == 1, f"expected 1 event when both signal types allowed, got {len(events_full)}"
    print("  OK  signal_types filter excludes contributions correctly")


def test_aggregate_min_score_filter():
    rows = [
        {"target": {"canonical_id": "t1", "display_name": "T1"},
         "w": {"canonical_id": "w1", "display_name": "W1"},
         "edge_at": "2024-01-15T00:00:00+00:00",
         "signal_type": "FOLLOWS_ON_GITHUB", "repo_full_name": None, "repo_url": None},
        {"target": {"canonical_id": "t1", "display_name": "T1"},
         "w": {"canonical_id": "w2", "display_name": "W2"},
         "edge_at": "2024-01-16T00:00:00+00:00",
         "signal_type": "FOLLOWS_ON_GITHUB", "repo_full_name": None, "repo_url": None},
    ]
    # min_score above what t1 will earn → no events
    high = _rule(min_members=2, min_score=10.0)
    events = aggregate(rows, user_id="demo",
                       window_start="2024-01-01T00:00:00+00:00",
                       window_end="2024-03-01T00:00:00+00:00",
                       window_days=60, rule=high)
    assert events == [], "min_score=10 should filter out a score-3 event"
    print("  OK  min_score filter applied")


def test_known_signal_types_includes_twitter():
    from intelligence.rule import KNOWN_SIGNAL_TYPES
    assert "FOLLOWS_ON_TWITTER" in KNOWN_SIGNAL_TYPES
    print("  OK  KNOWN_SIGNAL_TYPES includes FOLLOWS_ON_TWITTER")


def test_aggregate_twitter_only_convergence():
    """t_twitter has 2 distinct watchers, both via FOLLOWS_ON_TWITTER -> fires."""
    rows = [
        {"target": {"canonical_id": "t_twitter", "display_name": "TwitterOnly"},
         "w": {"canonical_id": "wA", "display_name": "WatcherA"},
         "edge_at": "2024-02-10T00:00:00+00:00",
         "signal_type": "FOLLOWS_ON_TWITTER",
         "repo_full_name": None, "repo_url": None,
         "evidence_url": "https://x.com/WatcherA/following",
         "edge_confidence": 0.91},
        {"target": {"canonical_id": "t_twitter", "display_name": "TwitterOnly"},
         "w": {"canonical_id": "wB", "display_name": "WatcherB"},
         "edge_at": "2024-02-15T00:00:00+00:00",
         "signal_type": "FOLLOWS_ON_TWITTER",
         "repo_full_name": None, "repo_url": None,
         "evidence_url": "https://x.com/WatcherB/following",
         "edge_confidence": 0.91},
    ]
    events = aggregate(rows, user_id="demo",
                       window_start="2024-01-01T00:00:00+00:00",
                       window_end="2024-03-01T00:00:00+00:00",
                       window_days=60, rule=_rule(min_members=2))
    assert len(events) == 1, f"expected 1 twitter-only event, got {len(events)}"
    e = events[0]
    assert e.target_id == "t_twitter"
    assert e.distinct_member_count == 2
    assert e.signal_type_counts == {"FOLLOWS_ON_TWITTER": 2}
    print(f"  OK  twitter-only convergence fires (N=2, score={e.score:.2f})")


def test_aggregate_mixed_source_github_and_twitter():
    """t_mixed has 2 distinct watchers — 1 via GitHub follow, 1 via Twitter follow."""
    rows = [
        {"target": {"canonical_id": "t_mixed", "display_name": "MixedSource"},
         "w": {"canonical_id": "wG", "display_name": "GhWatcher"},
         "edge_at": "2024-02-10T00:00:00+00:00",
         "signal_type": "FOLLOWS_ON_GITHUB",
         "repo_full_name": None, "repo_url": None,
         "evidence_url": None, "edge_confidence": None},
        {"target": {"canonical_id": "t_mixed", "display_name": "MixedSource"},
         "w": {"canonical_id": "wT", "display_name": "TwWatcher"},
         "edge_at": "2024-02-12T00:00:00+00:00",
         "signal_type": "FOLLOWS_ON_TWITTER",
         "repo_full_name": None, "repo_url": None,
         "evidence_url": "https://x.com/TwWatcher/following",
         "edge_confidence": 0.91},
    ]
    events = aggregate(rows, user_id="demo",
                       window_start="2024-01-01T00:00:00+00:00",
                       window_end="2024-03-01T00:00:00+00:00",
                       window_days=60, rule=_rule(min_members=2))
    assert len(events) == 1, f"expected 1 mixed event, got {len(events)}"
    e = events[0]
    assert e.distinct_member_count == 2
    assert e.signal_type_counts == {"FOLLOWS_ON_GITHUB": 1, "FOLLOWS_ON_TWITTER": 1}
    print("  OK  mixed-source convergence fires (1 github + 1 twitter = N=2)")


def test_aggregate_signal_types_excludes_twitter():
    """Same twitter-only rows; rule restricts to GitHub-only signal types -> NO event."""
    rows = [
        {"target": {"canonical_id": "t_t", "display_name": "T"},
         "w": {"canonical_id": "wA", "display_name": "A"},
         "edge_at": "2024-02-10T00:00:00+00:00",
         "signal_type": "FOLLOWS_ON_TWITTER",
         "repo_full_name": None, "repo_url": None,
         "evidence_url": "https://x.com/A/following", "edge_confidence": 0.91},
        {"target": {"canonical_id": "t_t", "display_name": "T"},
         "w": {"canonical_id": "wB", "display_name": "B"},
         "edge_at": "2024-02-12T00:00:00+00:00",
         "signal_type": "FOLLOWS_ON_TWITTER",
         "repo_full_name": None, "repo_url": None,
         "evidence_url": "https://x.com/B/following", "edge_confidence": 0.91},
    ]
    rule_no_tw = _rule(min_members=2,
                       signal_types=["FOLLOWS_ON_GITHUB", "STARRED_REPO"])
    events = aggregate(rows, user_id="demo",
                       window_start="2024-01-01T00:00:00+00:00",
                       window_end="2024-03-01T00:00:00+00:00",
                       window_days=60, rule=rule_no_tw)
    assert events == [], "twitter-only target should not fire when twitter is excluded"
    # And when Twitter IS allowed, it does fire:
    rule_with_tw = _rule(min_members=2,
                         signal_types=["FOLLOWS_ON_GITHUB", "STARRED_REPO", "FOLLOWS_ON_TWITTER"])
    events_with = aggregate(rows, user_id="demo",
                            window_start="2024-01-01T00:00:00+00:00",
                            window_end="2024-03-01T00:00:00+00:00",
                            window_days=60, rule=rule_with_tw)
    assert len(events_with) == 1
    print("  OK  signal_types filter correctly excludes Twitter-only convergences")


def test_aggregate_twitter_evidence_carries_url_and_confidence():
    """Evidence dicts should preserve evidence_url and edge_confidence for twitter rows
    so the frontend can render click-through links and confidence badges."""
    rows = [
        {"target": {"canonical_id": "t1", "display_name": "T"},
         "w": {"canonical_id": "wA", "display_name": "A"},
         "edge_at": "2024-02-10T00:00:00+00:00",
         "signal_type": "FOLLOWS_ON_TWITTER",
         "repo_full_name": None, "repo_url": None,
         "evidence_url": "https://x.com/A/following",
         "edge_confidence": 0.91},
        {"target": {"canonical_id": "t1", "display_name": "T"},
         "w": {"canonical_id": "wB", "display_name": "B"},
         "edge_at": "2024-02-12T00:00:00+00:00",
         "signal_type": "FOLLOWS_ON_TWITTER",
         "repo_full_name": None, "repo_url": None,
         "evidence_url": "https://x.com/B/following",
         "edge_confidence": 0.76},
    ]
    e = aggregate(rows, user_id="demo",
                  window_start="2024-01-01T00:00:00+00:00",
                  window_end="2024-03-01T00:00:00+00:00",
                  window_days=60, rule=_rule(min_members=2))[0]
    urls = sorted(ev["evidence_url"] for ev in e.evidence)
    assert urls == ["https://x.com/A/following", "https://x.com/B/following"]
    confidences = sorted(ev["edge_confidence"] for ev in e.evidence)
    assert confidences == [0.76, 0.91]
    print("  OK  twitter evidence preserves evidence_url and edge_confidence")


def test_aggregate_dedupes_watcher_across_signal_types():
    """Same watcher contributing via both GitHub follow AND Twitter follow counts once."""
    rows = [
        {"target": {"canonical_id": "t1", "display_name": "T"},
         "w": {"canonical_id": "wSAME", "display_name": "SameWatcher"},
         "edge_at": "2024-02-10T00:00:00+00:00",
         "signal_type": "FOLLOWS_ON_GITHUB",
         "repo_full_name": None, "repo_url": None,
         "evidence_url": None, "edge_confidence": None},
        {"target": {"canonical_id": "t1", "display_name": "T"},
         "w": {"canonical_id": "wSAME", "display_name": "SameWatcher"},
         "edge_at": "2024-02-11T00:00:00+00:00",
         "signal_type": "FOLLOWS_ON_TWITTER",
         "repo_full_name": None, "repo_url": None,
         "evidence_url": "https://x.com/SameWatcher/following",
         "edge_confidence": 0.91},
    ]
    # min_members=2 — same watcher on two channels should NOT fire (still N=1)
    events = aggregate(rows, user_id="demo",
                       window_start="2024-01-01T00:00:00+00:00",
                       window_end="2024-03-01T00:00:00+00:00",
                       window_days=60, rule=_rule(min_members=2))
    assert events == [], "same watcher on 2 channels should still count as N=1"
    print("  OK  same watcher across signal types is de-duplicated")


def test_target_prominence_log_scale():
    """Bonus is 0 below threshold, ~1 at 100, ~2 at 1000, ~3 at 10000, capped above."""
    assert compute_target_prominence(0) == 0.0
    assert compute_target_prominence(50) == 0.0
    assert abs(compute_target_prominence(100) - 1.004) < 0.01
    assert abs(compute_target_prominence(1000) - 2.0) < 0.01
    assert abs(compute_target_prominence(10000) - 3.0) < 0.01
    # Above the cap, value clamps
    assert abs(compute_target_prominence(55231) - 3.0) < 0.01
    assert abs(compute_target_prominence(100000) - 3.0) < 0.01
    print("  OK  compute_target_prominence: 0 below threshold, log-scaled, capped above 10K")


def test_compute_score_with_prominence_zero_weight_matches_old_behavior():
    """weight_target_prominence=0 -> identical to old 3-component score."""
    s_no_prom, _ = compute_score(
        4, "2024-01-01T00:00:00+00:00", "2024-01-01T00:00:00+00:00", 90,
        weight_target_prominence=0.0, target_prominence_value=3.0,
    )
    s_old_shape, _ = compute_score(
        4, "2024-01-01T00:00:00+00:00", "2024-01-01T00:00:00+00:00", 90,
    )
    assert abs(s_no_prom - s_old_shape) < 0.001
    print("  OK  weight_target_prominence=0 reproduces pre-M12.5 behavior (backwards compat)")


def test_compute_score_with_prominence_adds_bonus():
    """A target with weight=1 + value=3 gets +3 to score."""
    s_with, breakdown = compute_score(
        4, "2024-01-01T00:00:00+00:00", "2024-01-01T00:00:00+00:00", 90,
        weight_target_prominence=1.0, target_prominence_value=3.0,
    )
    # 4 (members) + 1 (recency at window_end) + 0 (member_quality) + 3 (prominence) = 8
    assert abs(s_with - 8.0) < 0.001
    assert breakdown["target_prominence"] == 3.0
    print(f"  OK  prominence value 3.0 with weight 1.0 adds 3.0 to score (final={s_with:.2f})")


def test_aggregate_uses_prominence_map():
    """Targets with high owned-repo stars rank above targets without."""
    rows = [
        # tA: 2 watchers, no owned repos -> base score 2 + recency
        {"target": {"canonical_id": "tA", "display_name": "TA"},
         "w": {"canonical_id": "wa", "display_name": "WA"},
         "edge_at": "2024-02-25T00:00:00+00:00",
         "signal_type": "FOLLOWS_ON_GITHUB", "repo_full_name": None, "repo_url": None},
        {"target": {"canonical_id": "tA", "display_name": "TA"},
         "w": {"canonical_id": "wb", "display_name": "WB"},
         "edge_at": "2024-02-25T00:00:00+00:00",
         "signal_type": "FOLLOWS_ON_GITHUB", "repo_full_name": None, "repo_url": None},
        # tB: 2 watchers, owns a 30K-star repo -> base + prominence cap
        {"target": {"canonical_id": "tB", "display_name": "TB"},
         "w": {"canonical_id": "wa", "display_name": "WA"},
         "edge_at": "2024-02-25T00:00:00+00:00",
         "signal_type": "STARRED_REPO", "repo_full_name": "x/y", "repo_url": "#"},
        {"target": {"canonical_id": "tB", "display_name": "TB"},
         "w": {"canonical_id": "wc", "display_name": "WC"},
         "edge_at": "2024-02-25T00:00:00+00:00",
         "signal_type": "STARRED_REPO", "repo_full_name": "x/y", "repo_url": "#"},
    ]
    rule = _rule(min_members=2)
    rule.weight_target_prominence = 1.0
    events = aggregate(rows, user_id="demo",
                       window_start="2024-01-01T00:00:00+00:00",
                       window_end="2024-03-01T00:00:00+00:00",
                       window_days=60, rule=rule,
                       target_prominence_stars={"tA": 0, "tB": 30000})
    by_id = {e.target_id: e for e in events}
    assert by_id["tB"].score > by_id["tA"].score, \
        f"tB ({by_id['tB'].score}) should outrank tA ({by_id['tA'].score})"
    # tB should have the cap value (3.0); tA should have 0
    assert abs(by_id["tB"].score_breakdown["target_prominence"] - 3.0) < 0.01
    assert by_id["tA"].score_breakdown["target_prominence"] == 0.0
    # Ordering: tB first (higher score)
    assert events[0].target_id == "tB"
    print(f"  OK  aggregate uses prominence map: tB outranks tA "
          f"({by_id['tB'].score:.2f} > {by_id['tA'].score:.2f})")


def test_aggregate_emits_breakdown_and_signal_counts():
    rows = [
        {"target": {"canonical_id": "t1", "display_name": "T1"},
         "w": {"canonical_id": "w1", "display_name": "W1"},
         "edge_at": "2024-02-25T00:00:00+00:00",  # near window end → recency bonus high
         "signal_type": "FOLLOWS_ON_GITHUB", "repo_full_name": None, "repo_url": None},
        {"target": {"canonical_id": "t1", "display_name": "T1"},
         "w": {"canonical_id": "w2", "display_name": "W2"},
         "edge_at": "2024-02-20T00:00:00+00:00",
         "signal_type": "STARRED_REPO", "repo_full_name": "x/y", "repo_url": "#"},
    ]
    e = aggregate(rows, user_id="demo",
                  window_start="2024-01-01T00:00:00+00:00",
                  window_end="2024-03-01T00:00:00+00:00",
                  window_days=60, rule=_rule(min_members=2))[0]
    assert e.score_breakdown["distinct_members"] == 2.0
    assert e.score_breakdown["recency"] > 0.5  # near window end
    assert e.signal_type_counts == {"FOLLOWS_ON_GITHUB": 1, "STARRED_REPO": 1}
    assert e.first_signal_at == "2024-02-20T00:00:00+00:00"
    assert e.last_signal_at == "2024-02-25T00:00:00+00:00"
    print(f"  OK  breakdown + first/last signal + signal_type_counts populated")


def main() -> int:
    tests = [
        test_compute_score_basic,
        test_compute_score_recency_bonus_recent,
        test_compute_score_recency_bonus_old,
        test_compute_score_weights_applied,
        test_parse_iso_handles_neo4j_nanoseconds,
        test_aggregate_synthetic_fixture,
        test_aggregate_event_id_is_stable,
        test_aggregate_filters_signal_types,
        test_aggregate_min_score_filter,
        test_known_signal_types_includes_twitter,
        test_aggregate_twitter_only_convergence,
        test_aggregate_mixed_source_github_and_twitter,
        test_aggregate_signal_types_excludes_twitter,
        test_aggregate_twitter_evidence_carries_url_and_confidence,
        test_aggregate_dedupes_watcher_across_signal_types,
        test_target_prominence_log_scale,
        test_compute_score_with_prominence_zero_weight_matches_old_behavior,
        test_compute_score_with_prominence_adds_bonus,
        test_aggregate_uses_prominence_map,
        test_aggregate_emits_breakdown_and_signal_counts,
    ]
    print(f"Running {len(tests)} unit tests for intelligence.convergence:")
    failed = 0
    for t in tests:
        try:
            t()
        except AssertionError as exc:
            print(f"  FAIL  {t.__name__}: {exc}")
            failed += 1
        except Exception as exc:
            print(f"  ERROR {t.__name__}: {type(exc).__name__}: {exc}")
            failed += 1
    print()
    if failed:
        print(f"{failed} test(s) failed.")
        return 1
    print(f"All {len(tests)} tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
