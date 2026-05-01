"""Unit tests for the M9.5.2 classifier (no Gemini calls — uses FakeLLM).

Run:
    python intelligence/dossier/test_classifier.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from intelligence.dossier.classifier import (
    Classification,
    LLMClassifier,
    parse_classification,
    validate_grounding,
)
from intelligence.dossier.enrichment import (
    ConvergenceEvidence,
    EnrichmentBundle,
    GitHubProfile,
    KBMatch,
    OwnedRepo,
    TargetPersonRef,
    TwitterProfile,
)


def _anton_bundle(*, kb_known: bool = False) -> EnrichmentBundle:
    return EnrichmentBundle(
        target_person=TargetPersonRef(
            canonical_id="anton-id", display_name="Anton Osika",
            role_tags=["founder_candidate"],
            identities=[
                {"platform": "github", "handle": "AntonOsika",
                 "profile_url": "https://github.com/AntonOsika"},
            ],
        ),
        github_profile=GitHubProfile(
            handle="AntonOsika", profile_url="https://github.com/AntonOsika",
            name="Anton Osika", bio="Founder Lovable.dev",
            location="Stockholm", company="@lovable-dev",
            public_repos=145, followers=3278, following=200,
        ),
        owned_repos=[OwnedRepo(
            full_name="AntonOsika/gpt-engineer",
            html_url="https://github.com/AntonOsika/gpt-engineer",
            stars=55231, language="Python",
            description="Specify what you want it to build",
        )],
        twitter_profile=TwitterProfile(
            handle="antonosika", profile_url="https://x.com/antonosika",
            display_name="Anton Osika", followers_count=8000,
        ),
        recent_tweets=[],
        convergence_evidence=ConvergenceEvidence(
            event_id="cv-demo-anton-id-2024-11-01",
            distinct_member_count=4, score=4.05,
            window_start="2024-01-01T00:00:00+00:00",
            window_end="2024-11-01T00:00:00+00:00",
            signal_type_counts={"STARRED_REPO": 4},
            evidence_rows=[],
        ),
        cross_platform_followers=[],
        kb_match=KBMatch(
            is_known=kb_known,
            investor_type="Angel" if kb_known else None,
        ),
        user_id="demo",
        gathered_at="2026-05-01T00:00:00+00:00",
    )


class CannedLLM:
    def __init__(self, classification: Classification):
        self._c = classification
        self.calls: int = 0

    def classify(self, bundle: EnrichmentBundle) -> Classification:
        self.calls += 1
        return self._c


# --- Tests --------------------------------------------------------------

def test_parse_classification_clean_json():
    raw = '''{
      "role": "founder", "confidence": 0.92,
      "narrative": "Anton built gpt-engineer.",
      "key_signals": [{"claim": "55K stars", "supporting_url": "https://github.com/AntonOsika/gpt-engineer"}],
      "recommended_action": "warm_intro_via_max_stoiber",
      "cross_check_kb": {"is_known_investor": false, "investor_type": null, "agreement_with_kb": "kb_silent"}
    }'''
    c = parse_classification(raw)
    assert c.role == "founder" and c.confidence == 0.92
    assert len(c.key_signals) == 1
    assert c.cross_check_kb["is_known_investor"] is False
    print("  OK  parse_classification: clean JSON")


def test_parse_classification_fenced_json():
    raw = '```json\n{"role": "investor", "confidence": 0.7, "narrative": "x", "key_signals": [], "recommended_action": "monitor", "cross_check_kb": {}}\n```'
    c = parse_classification(raw)
    assert c.role == "investor" and c.confidence == 0.7
    print("  OK  parse_classification: tolerates ```json fences")


def test_parse_classification_invalid_role_normalized():
    raw = '{"role": "founder_candidate", "confidence": 0.9, "narrative": "x", "key_signals": [], "recommended_action": "monitor", "cross_check_kb": {}}'
    c = parse_classification(raw)
    assert c.role == "unclear"
    print("  OK  parse_classification: invalid role -> 'unclear'")


def test_parse_classification_garbage():
    c = parse_classification("model said yes maybe")
    assert c.role == "unclear" and c.confidence == 0.0
    assert "unparseable" in c.narrative
    print("  OK  parse_classification: garbage -> unclear@0.0")


def test_validate_grounding_passes_when_url_in_bundle():
    b = _anton_bundle()
    c = Classification(
        role="founder", confidence=0.9,
        narrative="Anton built gpt-engineer with 55K stars.",
        key_signals=[{
            "claim": "owns gpt-engineer",
            "supporting_url": "https://github.com/AntonOsika/gpt-engineer",
        }],
        recommended_action="monitor",
        cross_check_kb={"is_known_investor": False, "investor_type": None, "agreement_with_kb": "kb_silent"},
    )
    issues = validate_grounding(b, c)
    assert issues == [], issues
    print("  OK  validate_grounding: passes when key_signal URL is in bundle")


def test_validate_grounding_catches_invented_url():
    b = _anton_bundle()
    c = Classification(
        role="founder", confidence=0.9,
        narrative="x",
        key_signals=[{
            "claim": "fake claim",
            "supporting_url": "https://example.com/totally-not-in-bundle",
        }],
        recommended_action="monitor",
        cross_check_kb={"is_known_investor": False, "investor_type": None, "agreement_with_kb": "kb_silent"},
    )
    issues = validate_grounding(b, c)
    assert any("ungrounded URL" in i for i in issues), issues
    print("  OK  validate_grounding: flags invented supporting_url as ungrounded")


def test_validate_grounding_catches_kb_disagreement():
    b = _anton_bundle(kb_known=True)
    c = Classification(
        role="founder",   # WRONG — KB says investor
        confidence=0.95,
        narrative="x",
        key_signals=[],
        recommended_action="monitor",
        cross_check_kb={"is_known_investor": True, "investor_type": "Angel", "agreement_with_kb": "disagree"},
    )
    issues = validate_grounding(b, c)
    assert any("KB says known investor" in i for i in issues), issues
    print("  OK  validate_grounding: catches role disagreement when KB has ground truth")


def test_classify_uses_supplied_llm():
    """The module-level classify() function dispatches to the LLM we pass in."""
    from intelligence.dossier.classifier import classify
    b = _anton_bundle()
    canned = Classification(
        role="founder", confidence=0.92,
        narrative="anton",
        key_signals=[],
        recommended_action="warm_intro_via_max",
        cross_check_kb={"is_known_investor": False, "investor_type": None, "agreement_with_kb": "kb_silent"},
    )
    llm = CannedLLM(canned)
    c = classify(b, llm=llm)
    assert c.role == "founder" and c.confidence == 0.92
    assert llm.calls == 1
    print("  OK  classify() dispatches to supplied LLM")


# --- Test runner --------------------------------------------------------

TESTS = [
    test_parse_classification_clean_json,
    test_parse_classification_fenced_json,
    test_parse_classification_invalid_role_normalized,
    test_parse_classification_garbage,
    test_validate_grounding_passes_when_url_in_bundle,
    test_validate_grounding_catches_invented_url,
    test_validate_grounding_catches_kb_disagreement,
    test_classify_uses_supplied_llm,
]


def main() -> int:
    print(f"Running {len(TESTS)} M9.5.2 classifier tests...\n")
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
