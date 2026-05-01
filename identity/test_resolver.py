"""Unit tests for the identity resolver and its tiers.

No Neo4j, no Gemini. Uses Fake implementations of IdentityLookup,
CandidateFinder, and LLMArbiter so every tier can be exercised in isolation.

Run:
    python identity/test_resolver.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from identity.bio_link_extractor import extract_platform_links
from identity.candidate_finder import (
    CandidatePerson,
    handle_similarity,
    normalize_name,
    score_candidate,
)
from identity.llm_arbiter import LLMVerdict, parse_verdict
from identity.resolver import (
    LLM_MERGE_CONFIDENCE_THRESHOLD,
    NAMESPACE,
    OverrideIndex,
    Resolver,
    platform_person_id,
)


# --- Fakes ------------------------------------------------------------------

class FakeIdentityLookup:
    def __init__(self, mapping: dict[tuple[str, str], str] | None = None):
        self._mapping = mapping or {}

    def find_canonical_id(self, platform: str, handle: str) -> str | None:
        return self._mapping.get((platform, handle.lower()))


class FakeCandidateFinder:
    def __init__(self, candidates: list[CandidatePerson] | None = None):
        self._candidates = candidates or []

    def find(self, platform: str, handle: str, profile_blob: dict) -> list[CandidatePerson]:
        return list(self._candidates)


class FakeLLMArbiter:
    def __init__(self, verdict: LLMVerdict):
        self._verdict = verdict
        self.calls: list[tuple] = []

    def judge(self, new_platform, new_handle, new_profile, candidates):
        self.calls.append((new_platform, new_handle, [c.canonical_id for c in candidates]))
        return self._verdict


def _make_resolver(
    *,
    overrides: list[dict] | None = None,
    lookup: dict | None = None,
    candidates: list[CandidatePerson] | None = None,
    arbiter: FakeLLMArbiter | None = None,
) -> tuple[Resolver, FakeLLMArbiter | None]:
    return Resolver(
        overrides=OverrideIndex(overrides or []),
        identity_lookup=FakeIdentityLookup(lookup or {}),
        candidate_finder=FakeCandidateFinder(candidates or []),
        llm_arbiter=arbiter,
    ), arbiter


# --- Bio-link extractor -----------------------------------------------------

def test_bio_extracts_github_url():
    out = extract_platform_links("Find me at https://github.com/AntonOsika")
    assert out == [("github", "antonosika")], out
    print("  OK  bio extracts GitHub URL")


def test_bio_extracts_twitter_url():
    out = extract_platform_links("twitter.com/sama")
    assert out == [("twitter", "sama")], out
    print("  OK  bio extracts twitter.com URL")


def test_bio_extracts_x_dot_com():
    out = extract_platform_links("https://x.com/karpathy")
    assert out == [("twitter", "karpathy")], out
    print("  OK  bio extracts x.com URL as twitter")


def test_bio_extracts_linkedin_url():
    out = extract_platform_links("https://www.linkedin.com/in/anton-osika/")
    assert out == [("linkedin", "anton-osika")], out
    print("  OK  bio extracts LinkedIn URL")


def test_bio_extracts_markdown_link():
    out = extract_platform_links("[my gh](https://github.com/jaseelkt) and [tw](x.com/jaseelkt)")
    assert ("github", "jaseelkt") in out
    assert ("twitter", "jaseelkt") in out
    print("  OK  bio extracts URLs from markdown link syntax")


def test_bio_dedupes_repeated_urls():
    out = extract_platform_links("github.com/foo and github.com/foo and https://github.com/foo")
    assert out == [("github", "foo")], out
    print("  OK  bio extractor de-duplicates repeated URLs")


def test_bio_skips_reserved_paths():
    out = extract_platform_links("Visit x.com/home and github.com/orgs and twitter.com/explore")
    assert out == [], out
    print("  OK  bio extractor skips reserved paths (home, orgs, explore)")


def test_bio_case_insensitive():
    out = extract_platform_links("HTTPS://GITHUB.COM/AntonOsika")
    assert out == [("github", "antonosika")], out
    print("  OK  bio extractor is case-insensitive, lowercases handle")


def test_bio_empty_or_none():
    assert extract_platform_links(None) == []
    assert extract_platform_links("") == []
    assert extract_platform_links("just some text with no links") == []
    print("  OK  bio extractor returns [] for empty/None/no-link input")


# --- Override index ---------------------------------------------------------

def test_override_exact_match():
    idx = OverrideIndex([{
        "github_handle": "AntonOsika",
        "linkedin_slug": "antonosika",
        "twitter_handle": "antonosika",
        "display_name": "Anton Osika",
    }])
    assert idx.lookup("github", "AntonOsika") == platform_person_id("github", "antonosika")
    print("  OK  override lookup hits on exact platform+handle")


def test_override_case_insensitive():
    idx = OverrideIndex([{
        "github_handle": "AntonOsika", "linkedin_slug": "", "twitter_handle": "",
        "display_name": "Anton Osika",
    }])
    cid_lower = idx.lookup("github", "antonosika")
    cid_upper = idx.lookup("github", "ANTONOSIKA")
    assert cid_lower is not None
    assert cid_lower == cid_upper
    print("  OK  override lookup is case-insensitive")


def test_override_cross_platform_same_id():
    idx = OverrideIndex([{
        "github_handle": "AntonOsika",
        "linkedin_slug": "antonosika",
        "twitter_handle": "antonosika",
        "display_name": "Anton Osika",
    }])
    gh = idx.lookup("github", "antonosika")
    li = idx.lookup("linkedin", "antonosika")
    tw = idx.lookup("twitter", "antonosika")
    assert gh == li == tw
    print(f"  OK  override returns same canonical_id across all 3 platforms ({gh[:8]})")


def test_override_missing_csv_safe():
    idx = OverrideIndex.from_csv(Path("/tmp/definitely-does-not-exist.csv"))
    assert idx.lookup("github", "anyone") is None
    assert len(idx) == 0
    print("  OK  OverrideIndex.from_csv handles missing file gracefully")


# --- platform_person_id determinism / Phase 1 compatibility ------------------

def test_platform_person_id_matches_phase1_github():
    """The github branch must produce the same id as scrapers/cypher.py:github_person_id."""
    from scrapers.cypher import github_person_id as phase1_github_id
    handle = "AntonOsika"
    assert platform_person_id("github", handle) == phase1_github_id(handle)
    print("  OK  platform_person_id('github', h) == Phase 1 github_person_id(h)")


def test_namespace_unchanged():
    import uuid as _uuid
    assert NAMESPACE == _uuid.UUID("8e1b3f2a-1c5d-4e7f-9a8b-2c3d4e5f6a7b")
    print("  OK  NAMESPACE UUID is the same constant as Phase 1")


# --- Resolver tier 1 (override) ---------------------------------------------

def test_resolver_tier1_override_hit():
    resolver, _ = _make_resolver(overrides=[{
        "github_handle": "AntonOsika",
        "linkedin_slug": "antonosika",
        "twitter_handle": "antonosika",
        "display_name": "Anton Osika",
    }])
    result = resolver.resolve("twitter", "antonosika", {"display_name": "Anton Osika"})
    assert result.tier == "override", result.tier
    assert result.confidence == 1.0
    expected = platform_person_id("github", "antonosika")
    assert result.canonical_id == expected
    print("  OK  Resolver Tier 1: override CSV hit returns the canonical id")


# --- Resolver tier 2 (bio link) ---------------------------------------------

def test_resolver_tier2_bio_link_hit():
    """Twitter handle 'foo'; bio links to github.com/bar; bar exists in graph."""
    existing_id = "abc-123-existing"
    resolver, _ = _make_resolver(
        lookup={("github", "bar"): existing_id},
        candidates=[],
    )
    profile = {
        "display_name": "Foo Bar",
        "bio": "I'm @bar on github: https://github.com/bar",
    }
    result = resolver.resolve("twitter", "foo", profile)
    assert result.tier == "bio_link", result.tier
    assert result.canonical_id == existing_id
    print("  OK  Resolver Tier 2: bio link to existing Person merges correctly")


def test_resolver_tier2_self_reference_skipped():
    """A github profile whose bio links to its own github URL must NOT self-merge."""
    resolver, _ = _make_resolver(lookup={})  # no existing matches
    profile = {"bio": "https://github.com/foo"}
    result = resolver.resolve("github", "foo", profile)
    assert result.tier == "fresh", result.tier
    print("  OK  Resolver Tier 2: self-platform self-handle bio link is skipped")


# --- Resolver tier 3 (candidate + LLM) --------------------------------------

def test_resolver_tier3_no_candidates_no_llm_call():
    arbiter = FakeLLMArbiter(LLMVerdict("same", 1.0, "should not run", "x"))
    resolver, _ = _make_resolver(candidates=[], arbiter=arbiter)
    result = resolver.resolve("twitter", "neverseen", {"display_name": "Some Person"})
    assert result.tier == "fresh", result.tier
    assert arbiter.calls == [], "LLM must not be called when no candidates"
    print("  OK  Resolver Tier 3: no candidates -> fresh, LLM never invoked")


def test_resolver_tier3_llm_says_same_high_confidence_merges():
    cand = CandidatePerson(
        canonical_id="cand-1", display_name="Anton Osika",
        identities=[("github", "AntonOsika")],
        match_reason="name+handle overlap", score=0.9,
    )
    arbiter = FakeLLMArbiter(LLMVerdict("same", 0.95, "same person", "cand-1"))
    resolver, arb = _make_resolver(candidates=[cand], arbiter=arbiter)
    result = resolver.resolve("twitter", "antonosika", {"display_name": "Anton Osika"})
    assert result.tier == "llm_match", result.tier
    assert result.canonical_id == "cand-1"
    assert len(arb.calls) == 1
    print("  OK  Resolver Tier 3: LLM same@0.95 -> merge to candidate")


def test_resolver_tier3_llm_low_confidence_does_not_merge():
    cand = CandidatePerson(
        canonical_id="cand-2", display_name="Anton O.",
        identities=[("github", "antonO")], match_reason="weak", score=0.55,
    )
    arbiter = FakeLLMArbiter(LLMVerdict("same", 0.6, "maybe", "cand-2"))
    resolver, _ = _make_resolver(candidates=[cand], arbiter=arbiter)
    result = resolver.resolve("twitter", "antonosika", {"display_name": "Anton Osika"})
    assert result.tier == "fresh", result.tier
    assert result.canonical_id != "cand-2"
    assert "LLM did not merge" in result.reasoning
    print(f"  OK  Resolver Tier 3: LLM same@0.60 below threshold ({LLM_MERGE_CONFIDENCE_THRESHOLD}) -> fresh")


def test_resolver_tier3_llm_says_different():
    cand = CandidatePerson(
        canonical_id="cand-3", display_name="Anton Other",
        identities=[("twitter", "antonosika")], match_reason="handle", score=0.85,
    )
    arbiter = FakeLLMArbiter(LLMVerdict("different", 0.9, "different people", None))
    resolver, _ = _make_resolver(candidates=[cand], arbiter=arbiter)
    result = resolver.resolve("twitter", "antonosika", {"display_name": "Different Person"})
    assert result.tier == "fresh", result.tier
    assert result.canonical_id != "cand-3"
    print("  OK  Resolver Tier 3: LLM 'different' -> fresh, no merge")


def test_resolver_tier3_llm_unknown():
    cand = CandidatePerson(
        canonical_id="cand-4", display_name="Some Person",
        identities=[("twitter", "x")], match_reason="weak", score=0.5,
    )
    arbiter = FakeLLMArbiter(LLMVerdict("unknown", 0.0, "insufficient evidence", None))
    resolver, _ = _make_resolver(candidates=[cand], arbiter=arbiter)
    result = resolver.resolve("twitter", "y", {"display_name": "Some Person"})
    assert result.tier == "fresh"
    print("  OK  Resolver Tier 3: LLM 'unknown' -> fresh (conservative)")


def test_resolver_tier3_no_arbiter_configured():
    cand = CandidatePerson(
        canonical_id="cand-5", display_name="x",
        identities=[("twitter", "x")], match_reason="r", score=0.6,
    )
    resolver, _ = _make_resolver(candidates=[cand], arbiter=None)
    result = resolver.resolve("twitter", "x", {"display_name": "x"})
    assert result.tier == "fresh"
    assert "no LLM arbiter configured" in result.reasoning
    print("  OK  Resolver Tier 3: no LLM arbiter -> fresh (conservative)")


# --- LLM verdict parsing ----------------------------------------------------

def test_parse_verdict_clean_json():
    raw = '{"decision":"same","confidence":0.92,"candidate_canonical_id":"abc","reasoning":"yes"}'
    v = parse_verdict(raw)
    assert v.decision == "same" and v.confidence == 0.92 and v.candidate_canonical_id == "abc"
    print("  OK  parse_verdict: clean JSON")


def test_parse_verdict_fenced():
    raw = '```json\n{"decision":"different","confidence":0.7,"candidate_canonical_id":null,"reasoning":"no"}\n```'
    v = parse_verdict(raw)
    assert v.decision == "different" and v.confidence == 0.7
    print("  OK  parse_verdict: tolerates ```json fences")


def test_parse_verdict_garbage():
    v = parse_verdict("the model said yes maybe")
    assert v.decision == "unknown" and v.confidence == 0.0
    print("  OK  parse_verdict: garbage input -> unknown@0.0")


def test_parse_verdict_invalid_decision_normalized():
    raw = '{"decision":"definitely","confidence":0.9}'
    v = parse_verdict(raw)
    assert v.decision == "unknown"
    print("  OK  parse_verdict: invalid decision string -> unknown")


# --- Candidate finder pure helpers ------------------------------------------

def test_normalize_name_strips_accents():
    assert normalize_name("Élie Habib") == "elie habib"
    assert normalize_name("  Anton   Osika  ") == "anton osika"
    print("  OK  normalize_name: accent-strip + whitespace collapse")


def test_handle_similarity():
    assert handle_similarity("antonosika", "antonosika") == 1.0
    assert handle_similarity("anton", "antonosika") == 0.85  # substring
    assert handle_similarity("foo", "bar") < 0.5
    print("  OK  handle_similarity: exact / substring / disjoint")


def test_score_candidate_name_only():
    score, _ = score_candidate("anton osika", "totallydifferent",
                               "anton osika", [("twitter", "totallydifferent")])
    assert score >= 0.9
    print(f"  OK  score_candidate: exact name match scores high ({score:.2f})")


def test_score_candidate_no_match():
    score, _ = score_candidate("alice smith", "alice",
                               "bob jones", [("github", "bob")])
    assert score < 0.5
    print(f"  OK  score_candidate: unrelated name+handle scores low ({score:.2f})")


# --- Test runner ------------------------------------------------------------

TESTS = [
    test_bio_extracts_github_url,
    test_bio_extracts_twitter_url,
    test_bio_extracts_x_dot_com,
    test_bio_extracts_linkedin_url,
    test_bio_extracts_markdown_link,
    test_bio_dedupes_repeated_urls,
    test_bio_skips_reserved_paths,
    test_bio_case_insensitive,
    test_bio_empty_or_none,
    test_override_exact_match,
    test_override_case_insensitive,
    test_override_cross_platform_same_id,
    test_override_missing_csv_safe,
    test_platform_person_id_matches_phase1_github,
    test_namespace_unchanged,
    test_resolver_tier1_override_hit,
    test_resolver_tier2_bio_link_hit,
    test_resolver_tier2_self_reference_skipped,
    test_resolver_tier3_no_candidates_no_llm_call,
    test_resolver_tier3_llm_says_same_high_confidence_merges,
    test_resolver_tier3_llm_low_confidence_does_not_merge,
    test_resolver_tier3_llm_says_different,
    test_resolver_tier3_llm_unknown,
    test_resolver_tier3_no_arbiter_configured,
    test_parse_verdict_clean_json,
    test_parse_verdict_fenced,
    test_parse_verdict_garbage,
    test_parse_verdict_invalid_decision_normalized,
    test_normalize_name_strips_accents,
    test_handle_similarity,
    test_score_candidate_name_only,
    test_score_candidate_no_match,
]


def main() -> int:
    print(f"Running {len(TESTS)} identity-resolver tests...\n")
    failures = 0
    for t in TESTS:
        try:
            t()
        except AssertionError as e:
            failures += 1
            print(f"  FAIL  {t.__name__}: {e}")
        except Exception as e:
            failures += 1
            print(f"  ERROR {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(TESTS) - failures}/{len(TESTS)} passed")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
