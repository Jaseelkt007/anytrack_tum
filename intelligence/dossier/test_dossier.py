"""Unit tests for M9.5.2 dossier persistence (no Neo4j — uses FakeSession).

Run:
    python intelligence/dossier/test_dossier.py
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from intelligence.dossier.classifier import Classification
from intelligence.dossier.dossier import (
    AUTO_PROMOTE_CONFIDENCE_THRESHOLD,
    QUERY_LATEST_ANY,
    QUERY_LATEST_DRAFT,
    build_or_update,
    compute_bundle_hash,
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


# --- Fakes --------------------------------------------------------------

class FakeResult:
    def __init__(self, rows):
        self._rows = list(rows)
    def single(self):
        return self._rows[0] if self._rows else None
    def data(self):
        return list(self._rows)


class FakeSession:
    """Records all writes; serves canned reads keyed by query substring."""
    def __init__(self):
        self.read_responses: dict[str, list[dict]] = {}
        self.writes: list[tuple[str, dict]] = []

    def queue_read(self, query_substring: str, rows: list[dict]):
        self.read_responses[query_substring] = rows

    def run(self, query: str, **params) -> FakeResult:
        # Reads (return canned data)
        for sub, rows in self.read_responses.items():
            if sub in query:
                return FakeResult(rows)
        # Writes (record for assertions)
        self.writes.append((query, params))
        return FakeResult([])


class CannedLLM:
    def __init__(self, classification: Classification):
        self._c = classification
        self.calls: int = 0

    def classify(self, bundle: EnrichmentBundle) -> Classification:
        self.calls += 1
        return self._c


def _bundle(kb_known: bool = False) -> EnrichmentBundle:
    return EnrichmentBundle(
        target_person=TargetPersonRef(
            canonical_id="anton-id", display_name="Anton Osika",
            role_tags=["founder_candidate"],
            identities=[{"platform": "github", "handle": "AntonOsika",
                         "profile_url": "https://github.com/AntonOsika"}],
        ),
        github_profile=GitHubProfile(handle="AntonOsika",
                                      profile_url="https://github.com/AntonOsika",
                                      followers=3278),
        owned_repos=[OwnedRepo(full_name="AntonOsika/gpt-engineer",
                                html_url="https://github.com/AntonOsika/gpt-engineer",
                                stars=55231)],
        twitter_profile=TwitterProfile(handle="antonosika",
                                        profile_url="https://x.com/antonosika"),
        recent_tweets=[],
        convergence_evidence=ConvergenceEvidence(
            event_id="cv-demo-anton-id-2024-11-01",
            distinct_member_count=4, score=4.05,
            window_start="2024-01-01T00:00:00+00:00",
            window_end="2024-11-01T00:00:00+00:00",
            signal_type_counts={"STARRED_REPO": 4}, evidence_rows=[],
        ),
        cross_platform_followers=[],
        kb_match=KBMatch(is_known=kb_known,
                         investor_type="Angel" if kb_known else None),
        user_id="demo",
        gathered_at="2026-05-01T00:00:00+00:00",
    )


def _classification(role="founder", confidence=0.92) -> Classification:
    return Classification(
        role=role, confidence=confidence,
        narrative="Anton built gpt-engineer.",
        key_signals=[{
            "claim": "55K stars",
            "supporting_url": "https://github.com/AntonOsika/gpt-engineer",
        }],
        recommended_action="warm_intro_via_max_stoiber",
        cross_check_kb={"is_known_investor": False,
                        "investor_type": None,
                        "agreement_with_kb": "kb_silent"},
    )


# --- Tests --------------------------------------------------------------

def test_first_run_creates_new_dossier_and_calls_llm():
    s = FakeSession()
    s.queue_read("WHERE d.status IN ['draft', 'ready_to_send']", [])  # no prior draft
    s.queue_read("RETURN d.id     AS id,\n       d.status", [])  # no prior anything
    llm = CannedLLM(_classification())
    res = build_or_update(s, user_id="demo", bundle=_bundle(),
                         triggering_event_ids=["cv-demo-anton-id-2024-11-01"],
                         llm=llm)
    assert llm.calls == 1
    assert res.is_new_dossier
    assert res.regenerated and not res.cached
    assert res.classification == "founder"
    # status auto-promoted because role=founder + confidence=0.92 >= 0.85
    assert res.status == "ready_to_send"
    print("  OK  first run: creates new dossier, calls LLM, auto-promotes founder@0.92 to ready_to_send")


def test_idempotent_run_skips_llm_when_hash_unchanged():
    s = FakeSession()
    bundle = _bundle()
    h = compute_bundle_hash(bundle)
    s.queue_read("WHERE d.status IN ['draft', 'ready_to_send']", [{
        "id": "dossier-demo-anton-id-20260501-120000",
        "hash": h,
        "classification": "founder",
        "confidence": 0.92,
        "status": "ready_to_send",
        "generated_at": "2026-05-01T12:00:00+00:00",
    }])
    llm = CannedLLM(_classification())
    res = build_or_update(s, user_id="demo", bundle=bundle,
                         triggering_event_ids=["cv-demo-anton-id-2024-11-01"],
                         llm=llm)
    assert llm.calls == 0, "LLM must not be called when hash unchanged"
    assert res.cached and not res.regenerated
    assert res.dossier_id == "dossier-demo-anton-id-20260501-120000"
    print("  OK  idempotent: same bundle hash -> no LLM call, returns existing draft")


def test_changed_bundle_overwrites_existing_draft():
    s = FakeSession()
    bundle = _bundle()
    s.queue_read("WHERE d.status IN ['draft', 'ready_to_send']", [{
        "id": "dossier-demo-anton-id-20260501-120000",
        "hash": "OLD_DIFFERENT_HASH",
        "classification": "unclear",
        "confidence": 0.4,
        "generated_at": "2026-05-01T12:00:00+00:00",
    }])
    s.queue_read("RETURN d.id     AS id,\n       d.status", [{
        "id": "dossier-demo-anton-id-20260501-120000",
        "status": "draft",
        "generated_at": "2026-05-01T12:00:00+00:00",
    }])
    llm = CannedLLM(_classification())
    res = build_or_update(s, user_id="demo", bundle=bundle,
                         triggering_event_ids=["cv-demo-anton-id-2024-11-01"],
                         llm=llm)
    assert llm.calls == 1, "LLM should be called when bundle hash changed"
    assert not res.is_new_dossier, "should overwrite the existing draft (same id)"
    assert res.dossier_id == "dossier-demo-anton-id-20260501-120000"
    print("  OK  changed bundle: re-classifies, overwrites existing draft in place")


def test_sent_dossier_is_immutable_creates_new_one():
    s = FakeSession()
    s.queue_read("WHERE d.status IN ['draft', 'ready_to_send']", [])  # no draft
    s.queue_read("RETURN d.id     AS id,\n       d.status", [{
        "id": "dossier-demo-anton-id-20260101-120000",
        "status": "sent",
        "generated_at": "2026-01-01T12:00:00+00:00",
    }])
    llm = CannedLLM(_classification())
    now = datetime(2026, 5, 1, 14, 30, 0, tzinfo=timezone.utc)
    res = build_or_update(s, user_id="demo", bundle=_bundle(),
                         triggering_event_ids=["cv-demo-anton-id-2024-11-01"],
                         llm=llm, now=now)
    assert res.is_new_dossier, "must NOT overwrite a sent dossier"
    assert res.dossier_id != "dossier-demo-anton-id-20260101-120000"
    assert "20260501" in res.dossier_id  # new dated id
    print("  OK  immutability: sent dossier preserved; new dated dossier created")


def test_auto_promotion_respects_classifications_to_emit():
    """An 'investor' classification stays draft when only 'founder' is in the emit list."""
    s = FakeSession()
    s.queue_read("WHERE d.status IN ['draft', 'ready_to_send']", [])
    s.queue_read("RETURN d.id     AS id,\n       d.status", [])
    llm = CannedLLM(_classification(role="investor", confidence=0.95))
    res = build_or_update(s, user_id="demo", bundle=_bundle(kb_known=True),
                         triggering_event_ids=["cv-demo-anton-id-2024-11-01"],
                         llm=llm,
                         classifications_to_emit=["founder"])
    assert res.classification == "investor"
    assert res.status == "draft", "investor not in emit list -> stays draft"
    print("  OK  auto-promote: investor classification respects emit list (stays draft)")


def test_low_confidence_stays_draft_even_for_founder():
    s = FakeSession()
    s.queue_read("WHERE d.status IN ['draft', 'ready_to_send']", [])
    s.queue_read("RETURN d.id     AS id,\n       d.status", [])
    llm = CannedLLM(_classification(role="founder", confidence=0.6))
    res = build_or_update(s, user_id="demo", bundle=_bundle(),
                         triggering_event_ids=["cv-demo-anton-id-2024-11-01"],
                         llm=llm)
    assert res.classification == "founder"
    assert res.confidence == 0.6
    assert res.status == "draft", f"confidence 0.6 < {AUTO_PROMOTE_CONFIDENCE_THRESHOLD} should stay draft"
    print(f"  OK  auto-promote: founder@0.6 < {AUTO_PROMOTE_CONFIDENCE_THRESHOLD} stays draft")


def test_grounding_violation_marks_dossier_failed():
    s = FakeSession()
    s.queue_read("WHERE d.status IN ['draft', 'ready_to_send']", [])
    s.queue_read("RETURN d.id     AS id,\n       d.status", [])
    bad = Classification(
        role="founder", confidence=0.95,
        narrative="x",
        key_signals=[{"claim": "fake", "supporting_url": "https://example.com/not-in-bundle"}],
        recommended_action="monitor",
        cross_check_kb={"is_known_investor": False, "investor_type": None, "agreement_with_kb": "kb_silent"},
    )
    llm = CannedLLM(bad)
    res = build_or_update(s, user_id="demo", bundle=_bundle(),
                         triggering_event_ids=["cv-demo-anton-id-2024-11-01"],
                         llm=llm)
    assert res.status == "failed"
    assert any("ungrounded URL" in i for i in res.grounding_issues)
    print("  OK  grounding violation -> status='failed' + issue list populated")


def test_force_reclassify_skips_idempotency_check():
    s = FakeSession()
    bundle = _bundle()
    h = compute_bundle_hash(bundle)
    s.queue_read("WHERE d.status IN ['draft', 'ready_to_send']", [{
        "id": "dossier-demo-anton-id-20260501-120000",
        "hash": h, "classification": "founder",
        "confidence": 0.92,
        "generated_at": "2026-05-01T12:00:00+00:00",
    }])
    s.queue_read("RETURN d.id     AS id,\n       d.status", [{
        "id": "dossier-demo-anton-id-20260501-120000",
        "status": "draft",
        "generated_at": "2026-05-01T12:00:00+00:00",
    }])
    llm = CannedLLM(_classification())
    res = build_or_update(s, user_id="demo", bundle=bundle,
                         triggering_event_ids=["cv-demo-anton-id-2024-11-01"],
                         llm=llm,
                         force_reclassify=True)
    assert llm.calls == 1, "force_reclassify must call LLM despite hash match"
    assert res.regenerated
    print("  OK  force_reclassify=True bypasses hash idempotency")


def test_kb_match_attaches_audit_edge():
    s = FakeSession()
    s.queue_read("WHERE d.status IN ['draft', 'ready_to_send']", [])
    s.queue_read("RETURN d.id     AS id,\n       d.status", [])
    llm = CannedLLM(_classification(role="investor", confidence=0.9))
    build_or_update(s, user_id="demo", bundle=_bundle(kb_known=True),
                    triggering_event_ids=["cv-demo-anton-id-2024-11-01"],
                    llm=llm)
    # Look for a write that matches the audit edge query
    audit_writes = [w for w in s.writes if "CROSS_SEEN_VIA_CONVERGENCE" in w[0]]
    assert len(audit_writes) == 1, f"expected 1 audit-edge write, got {len(audit_writes)}"
    assert audit_writes[0][1]["person_id"] == "anton-id"
    print("  OK  KB cross-match -> CROSS_SEEN_VIA_CONVERGENCE audit edge written")


# --- Test runner --------------------------------------------------------

TESTS = [
    test_first_run_creates_new_dossier_and_calls_llm,
    test_idempotent_run_skips_llm_when_hash_unchanged,
    test_changed_bundle_overwrites_existing_draft,
    test_sent_dossier_is_immutable_creates_new_one,
    test_auto_promotion_respects_classifications_to_emit,
    test_low_confidence_stays_draft_even_for_founder,
    test_grounding_violation_marks_dossier_failed,
    test_force_reclassify_skips_idempotency_check,
    test_kb_match_attaches_audit_edge,
]


def main() -> int:
    print(f"Running {len(TESTS)} M9.5.2 dossier persistence tests...\n")
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
