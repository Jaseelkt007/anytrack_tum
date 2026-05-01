"""Unit tests for the M9.5.5 dossier feedback module.

No Neo4j — uses a FakeSession that records writes and serves canned reads.

Run:
    python intelligence/dossier/test_feedback.py
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from intelligence.dossier.feedback import (
    DossierNotFoundError,
    FeedbackValidationError,
    STATUS_FLIP_VERDICTS,
    VALID_VERDICTS,
    submit_feedback,
)


# --- Fakes ---------------------------------------------------------------

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

    def queue_read(self, query_substring: str, rows: list[dict]) -> None:
        self.read_responses[query_substring] = rows

    def run(self, query: str, **params) -> FakeResult:
        for sub, rows in self.read_responses.items():
            if sub in query:
                return FakeResult(rows)
        # Mutating writes
        self.writes.append((query, params))
        return FakeResult([])


def _session_with_dossier(*, status: str = "ready_to_send",
                           target_id: str = "anton-id") -> FakeSession:
    s = FakeSession()
    s.queue_read("RETURN d.status AS status, d.target_person_id", [
        {"status": status, "target_person_id": target_id},
    ])
    return s


# --- Tests ---------------------------------------------------------------

def test_correct_verdict_records_no_status_change():
    s = _session_with_dossier(status="ready_to_send")
    res = submit_feedback(
        s, user_id="demo", dossier_id="d1", verdict="correct",
    )
    assert res.verdict == "correct"
    assert res.side_effect is None
    assert res.new_dossier_status is None
    # No FLIP_DOSSIER write should have happened
    flip_writes = [w for w in s.writes if "SET d.status = CASE" in w[0]]
    assert flip_writes == [], flip_writes
    # The INSERT did happen
    insert_writes = [w for w in s.writes if "MERGE (fb:DossierFeedback" in w[0]]
    assert len(insert_writes) == 1
    print("  OK  verdict='correct' records feedback, NO status flip")


def test_wrong_classification_flips_status_and_requires_corrected():
    s = _session_with_dossier(status="ready_to_send")
    res = submit_feedback(
        s, user_id="demo", dossier_id="d2",
        verdict="wrong_classification",
        corrected_classification="investor",
    )
    assert res.side_effect == "dossier_status_changed_to_rejected"
    assert res.new_dossier_status == "rejected"
    flip_writes = [w for w in s.writes if "SET d.status = CASE" in w[0]]
    assert len(flip_writes) == 1
    print("  OK  verdict='wrong_classification' with corrected_classification flips status")


def test_spam_flips_status_no_correction_required():
    s = _session_with_dossier(status="draft")
    res = submit_feedback(s, user_id="demo", dossier_id="d3", verdict="spam")
    assert res.side_effect == "dossier_status_changed_to_rejected"
    flip_writes = [w for w in s.writes if "SET d.status = CASE" in w[0]]
    assert len(flip_writes) == 1
    print("  OK  verdict='spam' flips status without requiring corrected_classification")


def test_invalid_verdict_raises():
    s = _session_with_dossier()
    try:
        submit_feedback(s, user_id="demo", dossier_id="d", verdict="totally_made_up")
        raise AssertionError("expected FeedbackValidationError")
    except FeedbackValidationError as e:
        assert "totally_made_up" in str(e)
    print("  OK  invalid verdict raises FeedbackValidationError")


def test_wrong_classification_without_corrected_raises():
    s = _session_with_dossier()
    try:
        submit_feedback(s, user_id="demo", dossier_id="d",
                        verdict="wrong_classification")
        raise AssertionError("expected FeedbackValidationError")
    except FeedbackValidationError as e:
        assert "corrected_classification" in str(e)
    print("  OK  wrong_classification without corrected_classification raises")


def test_invalid_corrected_classification_raises():
    s = _session_with_dossier()
    try:
        submit_feedback(s, user_id="demo", dossier_id="d",
                        verdict="wrong_classification",
                        corrected_classification="ceo")  # not a valid role
        raise AssertionError("expected FeedbackValidationError")
    except FeedbackValidationError as e:
        assert "ceo" in str(e)
    print("  OK  invalid corrected_classification raises")


def test_sent_dossier_records_feedback_but_preserves_status():
    """Sent dossiers are immutable per M9.5 contract — the email already went out."""
    s = _session_with_dossier(status="sent")
    res = submit_feedback(s, user_id="demo", dossier_id="d_sent", verdict="spam")
    assert res.side_effect is None, "side_effect must be None when status was 'sent'"
    assert res.new_dossier_status is None
    flip_writes = [w for w in s.writes if "SET d.status = CASE" in w[0]]
    assert flip_writes == [], "must NOT flip status of a sent dossier"
    insert_writes = [w for w in s.writes if "MERGE (fb:DossierFeedback" in w[0]]
    assert len(insert_writes) == 1, "feedback IS recorded even for sent dossiers"
    print("  OK  sent dossier preserved — feedback recorded, status NOT flipped")


def test_multiple_feedback_events_persist_chronologically():
    """Two submissions in sequence produce two distinct feedback rows."""
    s = _session_with_dossier(status="ready_to_send")
    t1 = datetime(2026, 5, 1, 10, 0, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 5, 1, 10, 0, 1, tzinfo=timezone.utc)
    r1 = submit_feedback(s, user_id="demo", dossier_id="d", verdict="correct", now=t1)
    # After first submit, the dossier status is unchanged but the next call
    # still sees "ready_to_send" because the FakeSession serves the same canned
    # read. That's correct for THIS test (verifying two writes happen).
    r2 = submit_feedback(s, user_id="demo", dossier_id="d", verdict="low_priority", now=t2)
    assert r1.feedback_id != r2.feedback_id
    assert r1.feedback_id < r2.feedback_id  # chronological because timestamp-keyed
    insert_writes = [w for w in s.writes if "MERGE (fb:DossierFeedback" in w[0]]
    assert len(insert_writes) == 2
    print(f"  OK  multiple feedback events persist with distinct ids ({r1.feedback_id[-15:]} < {r2.feedback_id[-15:]})")


def test_dossier_not_found_raises():
    """Posting feedback on a non-existent dossier raises DossierNotFoundError."""
    s = FakeSession()
    s.queue_read("RETURN d.status AS status, d.target_person_id", [])  # no rows
    try:
        submit_feedback(s, user_id="demo", dossier_id="nope", verdict="correct")
        raise AssertionError("expected DossierNotFoundError")
    except DossierNotFoundError as e:
        assert "nope" in str(e)
    print("  OK  unknown dossier_id raises DossierNotFoundError")


def test_status_flip_set_constants_match_plan():
    """Defensive: ensure the verdict vocabulary doesn't drift from the plan."""
    assert set(VALID_VERDICTS) == {
        "correct", "wrong_classification", "wrong_target", "spam", "low_priority",
    }
    assert STATUS_FLIP_VERDICTS == {"wrong_classification", "wrong_target", "spam"}
    print("  OK  VALID_VERDICTS and STATUS_FLIP_VERDICTS match the plan")


# --- Test runner --------------------------------------------------------

TESTS = [
    test_correct_verdict_records_no_status_change,
    test_wrong_classification_flips_status_and_requires_corrected,
    test_spam_flips_status_no_correction_required,
    test_invalid_verdict_raises,
    test_wrong_classification_without_corrected_raises,
    test_invalid_corrected_classification_raises,
    test_sent_dossier_records_feedback_but_preserves_status,
    test_multiple_feedback_events_persist_chronologically,
    test_dossier_not_found_raises,
    test_status_flip_set_constants_match_plan,
]


def main() -> int:
    print(f"Running {len(TESTS)} M9.5.5 feedback tests...\n")
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
