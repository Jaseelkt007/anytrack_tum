"""Unit tests for the pure logic in load_investor_reference.py.

No Neo4j needed — these test ID determinism, role tag derivation, and the
parameter shape going into Cypher MERGE.

Run:
    python scripts/test_load_investor_reference.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from load_investor_reference import (
    NAMESPACE,
    build_person_params,
    canonical_id_for,
    role_tags_for,
    split_pipe,
    stable_key,
)


def test_canonical_id_is_deterministic():
    row = {
        "display_name": "Naval Ravikant",
        "investor_type": "Angel",
        "linkedin_slug": "naval",
        "twitter_handle": "naval",
    }
    a = canonical_id_for(row)
    b = canonical_id_for(row)
    assert a == b, "same row must produce same id"
    assert a != "00000000-0000-0000-0000-000000000000"
    print(f"  OK  deterministic id: {a}")


def test_canonical_id_uses_linkedin_first():
    a = canonical_id_for({
        "display_name": "X", "investor_type": "Angel",
        "linkedin_slug": "alice", "twitter_handle": "bob",
    })
    b = canonical_id_for({
        "display_name": "Different Name", "investor_type": "VC - Big fund",
        "linkedin_slug": "alice", "twitter_handle": "different",
    })
    assert a == b, "linkedin_slug should dominate the id key"
    print("  OK  linkedin_slug dominates id key")


def test_canonical_id_falls_back_through_priority():
    li_only = canonical_id_for({"display_name": "A", "investor_type": "Angel", "linkedin_slug": "x"})
    tw_only = canonical_id_for({"display_name": "A", "investor_type": "Angel", "twitter_handle": "x"})
    name_only = canonical_id_for({"display_name": "A", "investor_type": "Angel"})
    # All three must be different — "x" as li-slug, tw-handle, or just name should not collide.
    assert len({li_only, tw_only, name_only}) == 3, "fallback tiers must produce distinct ids"
    print("  OK  li/tw/name fallback tiers produce distinct ids")


def test_role_tags_for_angel_includes_angel():
    assert role_tags_for({"investor_type": "Angel"}) == ["investor", "angel"]
    assert role_tags_for({"investor_type": "VC - Small fund"}) == ["investor"]
    assert role_tags_for({"investor_type": ""}) == ["investor"]
    print("  OK  role_tags_for(Angel) includes 'angel'")


def test_split_pipe():
    assert split_pipe("") == []
    assert split_pipe("a|b|c") == ["a", "b", "c"]
    assert split_pipe("a||b") == ["a", "b"]
    print("  OK  split_pipe handles empty and double-separators")


def test_build_person_params_full():
    row = {
        "display_name": "Naval Ravikant",
        "investor_type": "Angel",
        "country": "United States",
        "sector_tags": "AI/ML|SaaS|Consumer",
        "stage_tags": "Pre-Seed|Seed",
        "linkedin_url": "https://www.linkedin.com/in/naval/",
        "linkedin_slug": "naval",
        "twitter_handle": "naval",
    }
    params = build_person_params(row, "2026-05-01T12:00:00+00:00")

    assert params["display_name"] == "Naval Ravikant"
    assert params["investor_type"] == "Angel"
    assert params["country"] == "United States"
    assert params["sector_tags"] == ["AI/ML", "SaaS", "Consumer"]
    assert params["stage_tags"] == ["Pre-Seed", "Seed"]
    assert params["role_tags"] == ["investor", "angel"]
    assert params["linkedin_slug"] == "naval"
    assert params["linkedin_url"] == "https://www.linkedin.com/in/naval/"
    assert params["twitter_handle"] == "naval"
    assert params["twitter_url"] == "https://twitter.com/naval"
    assert params["now_iso"] == "2026-05-01T12:00:00+00:00"
    print("  OK  full params built correctly")


def test_build_person_params_empty_optional_fields():
    """Big VCs in the dataset have no LinkedIn or Twitter — ensure params are None,
    not empty strings (Cypher MERGE compares values; '' would create empty
    PlatformIdentity nodes with handle '')."""
    row = {
        "display_name": "Sequoia Capital",
        "investor_type": "VC - Big fund",
        "country": "",
        "sector_tags": "",
        "stage_tags": "",
        "linkedin_url": "",
        "linkedin_slug": "",
        "twitter_handle": "",
    }
    params = build_person_params(row, "2026-05-01T12:00:00+00:00")
    assert params["country"] is None
    assert params["sector_tags"] == []
    assert params["linkedin_slug"] is None
    assert params["linkedin_url"] is None
    assert params["twitter_handle"] is None
    assert params["twitter_url"] is None
    print("  OK  empty-string fields normalized to None / [] for safe MERGE")


def test_namespace_constant_unchanged():
    """If anyone changes NAMESPACE, every existing canonical_id is invalidated.
    This test exists to make that decision require explicit intent."""
    expected = "8e1b3f2a-1c5d-4e7f-9a8b-2c3d4e5f6a7b"
    assert str(NAMESPACE) == expected, "NAMESPACE constant changed — all IDs invalidated. Update intentionally."
    print("  OK  NAMESPACE constant unchanged")


def test_stable_key_normalizes_case():
    a = stable_key({"display_name": "Joe", "investor_type": "Angel", "linkedin_slug": "Naval"})
    b = stable_key({"display_name": "Joe", "investor_type": "Angel", "linkedin_slug": "naval"})
    assert a == b, "linkedin_slug should be case-folded for stable id"
    print("  OK  stable_key folds case")


def main() -> int:
    tests = [
        test_canonical_id_is_deterministic,
        test_canonical_id_uses_linkedin_first,
        test_canonical_id_falls_back_through_priority,
        test_role_tags_for_angel_includes_angel,
        test_split_pipe,
        test_build_person_params_full,
        test_build_person_params_empty_optional_fields,
        test_namespace_constant_unchanged,
        test_stable_key_normalizes_case,
    ]
    print(f"Running {len(tests)} unit tests for load_investor_reference.py:")
    failed = 0
    for t in tests:
        try:
            t()
        except AssertionError as exc:
            print(f"  FAIL  {t.__name__}: {exc}")
            failed += 1
        except Exception as exc:
            print(f"  ERROR  {t.__name__}: {type(exc).__name__}: {exc}")
            failed += 1
    print()
    if failed:
        print(f"{failed} test(s) failed.")
        return 1
    print(f"All {len(tests)} tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
