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
