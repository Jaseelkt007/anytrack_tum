"""Unit tests for the M12-email notifier (no Neo4j, no Resend).

Run:
    python intelligence/test_notifier.py
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from intelligence.notifier import (
    DigestItem,
    SendResult,
    build_subject,
    fetch_digest_candidates,
    render_html,
    send_daily_digest,
)
from intelligence.rule import AlertRule


# --- Fakes ---------------------------------------------------------------

class FakeResult:
    def __init__(self, rows):
        self._rows = list(rows)
    def data(self):
        return list(self._rows)
    def single(self):
        return self._rows[0] if self._rows else None


class FakeSession:
    """Substring-keyed reads + write recorder. Mutates also recorded for
    verifying status-flip behavior."""
    def __init__(self):
        self.read_responses: dict[str, list[dict]] = {}
        self.writes: list[tuple[str, dict]] = []

    def queue_read(self, query_substring: str, rows: list[dict]) -> None:
        self.read_responses[query_substring] = rows

    def run(self, query: str, **params) -> FakeResult:
        for sub, rows in self.read_responses.items():
            if sub in query:
                return FakeResult(rows)
        # Mutating writes (e.g., FLIP_DOSSIER_STATUS_SENT)
        self.writes.append((query, params))
        return FakeResult([])


class FakeEmailClient:
    def __init__(self, *, fail: bool = False):
        self.fail = fail
        self.calls: list[dict] = []

    def send(self, *, to, from_email, from_name, subject, html):
        self.calls.append({
            "to": to, "from_email": from_email, "from_name": from_name,
            "subject": subject, "html_size": len(html),
        })
        if self.fail:
            raise RuntimeError("simulated provider error")
        return {"id": f"msg_{len(self.calls)}"}


def _candidate_row(*, name="Anton Osika", classification="founder",
                    confidence=0.95, score=7.5, dossier_id="d1") -> dict:
    return {
        "id": dossier_id,
        "target_person_id": f"{dossier_id}-target",
        "target_name": name,
        "classification": classification,
        "confidence": confidence,
        "score": score,
        "narrative": f"{name} is the founder of something interesting.",
        "recommended_action": "Warm intro via Max Stoiber",
        "key_signals_json": json.dumps([
            {"claim": "owns gpt-engineer", "supporting_url": "https://github.com/AntonOsika/gpt-engineer"},
        ]),
        "evidence_bundle_json": "{}",
    }


def _rule(**overrides) -> AlertRule:
    r = AlertRule()
    for k, v in overrides.items():
        setattr(r, k, v)
    return r


# --- Tests ---------------------------------------------------------------

def test_no_email_set_returns_skipped():
    """When notify_email is None, the digest is a no-op."""
    s = FakeSession()
    r = _rule(notify_email=None)
    res = send_daily_digest(s, rule=r, email_client=FakeEmailClient())
    assert res.sent is False
    assert res.skipped_reason == "no_email"
    assert s.writes == []
    print("  OK  no notify_email -> skipped_reason='no_email', no writes")


def test_disabled_returns_skipped():
    s = FakeSession()
    r = _rule(notify_email="vc@example.com", notify_enabled=False)
    res = send_daily_digest(s, rule=r, email_client=FakeEmailClient())
    assert res.sent is False
    assert res.skipped_reason == "disabled"
    print("  OK  notify_enabled=False -> skipped_reason='disabled'")


def test_no_candidates_returns_skipped():
    s = FakeSession()
    s.queue_read("d.classification IN $classifications",[])  # zero candidates
    r = _rule(notify_email="vc@example.com")
    res = send_daily_digest(s, rule=r, email_client=FakeEmailClient())
    assert res.sent is False
    assert res.skipped_reason == "no_candidates"
    print("  OK  zero candidates -> skipped_reason='no_candidates', no email sent")


def test_happy_path_sends_email_and_flips_statuses():
    s = FakeSession()
    s.queue_read("d.classification IN $classifications",[_candidate_row()])
    client = FakeEmailClient()
    r = _rule(notify_email="vc@example.com")
    now = datetime(2026, 5, 1, 7, 0, 0, tzinfo=timezone.utc)
    res = send_daily_digest(s, rule=r, email_client=client, now=now,
                              from_email="bot@example.com", from_name="Bot")
    assert res.sent is True
    assert res.dossier_count == 1
    assert res.provider_message_id == "msg_1"
    # Email was actually called with the right shape
    assert len(client.calls) == 1
    call = client.calls[0]
    assert call["to"] == "vc@example.com"
    assert call["from_email"] == "bot@example.com"
    assert "Anton" in call["subject"]
    # Status flip happened — one write per dossier
    flips = [w for w in s.writes if "SET d.status" in w[0]]
    assert len(flips) == 1
    assert flips[0][1]["dossier_id"] == "d1"
    print("  OK  happy path: email sent, status flip recorded for the 1 dossier")


def test_send_failure_preserves_status():
    """If the provider raises, dossier statuses must NOT flip."""
    s = FakeSession()
    s.queue_read("d.classification IN $classifications",[_candidate_row(dossier_id="d2")])
    client = FakeEmailClient(fail=True)
    r = _rule(notify_email="vc@example.com")
    res = send_daily_digest(s, rule=r, email_client=client)
    assert res.sent is False
    assert res.skipped_reason == "send_failed"
    # Critical: NO status flip on failure
    flips = [w for w in s.writes if "SET d.status" in w[0]]
    assert flips == [], "status MUST NOT flip when send fails"
    print("  OK  send failure preserves status (no flip writes)")


def test_dry_run_no_email_no_flip():
    s = FakeSession()
    s.queue_read("d.classification IN $classifications",[_candidate_row(name="Test"), _candidate_row(dossier_id="d2", name="Test2")])
    client = FakeEmailClient()
    r = _rule(notify_email="vc@example.com")
    res = send_daily_digest(s, rule=r, email_client=client, dry_run=True)
    assert res.sent is False
    assert res.skipped_reason is None
    assert res.dossier_count == 2
    assert res.provider_message_id == "(dry-run)"
    assert client.calls == [], "dry_run must not call the email client"
    flips = [w for w in s.writes if "SET d.status" in w[0]]
    assert flips == [], "dry_run must not flip status"
    print("  OK  dry_run renders & previews but does NOT send or flip")


def test_query_thresholds_pass_through_to_cypher():
    s = FakeSession()
    s.queue_read("d.classification IN $classifications",[])
    r = _rule(
        notify_email="vc@example.com",
        notify_min_score=8.5,
        notify_min_confidence=0.92,
        notify_daily_cap=3,
        notify_classifications=["founder", "operator"],
    )
    fetch_digest_candidates(s, user_id="demo", rule=r)
    # The FakeSession's run() ran the candidate query (read), captured by read_responses, not writes.
    # Verify the rule values would be the params: re-run through a wrapped session.
    captured = {}
    class CapturingSession(FakeSession):
        def run(self, query, **params):
            if "d.classification IN $classifications" in query:
                captured.update(params)
            return super().run(query, **params)
    cs = CapturingSession()
    cs.queue_read("d.classification IN $classifications", [])
    fetch_digest_candidates(cs, user_id="demo", rule=r)
    assert captured["min_score"] == 8.5
    assert captured["min_confidence"] == 0.92
    assert captured["cap"] == 3
    assert captured["classifications"] == ["founder", "operator"]
    print("  OK  query thresholds (score/conf/cap/classifications) pass through to Cypher")


def test_subject_line_includes_count_and_top_target():
    items = [
        DigestItem(dossier_id="d1", target_name="Anton Osika", classification="founder",
                   confidence=0.95, score=7.5, narrative="x", recommended_action="",
                   key_signals=[]),
        DigestItem(dossier_id="d2", target_name="Other", classification="founder",
                   confidence=0.9, score=7.0, narrative="x", recommended_action="",
                   key_signals=[]),
    ]
    now = datetime(2026, 5, 1, 7, 0, 0, tzinfo=timezone.utc)
    s = build_subject(items, digest_date=now)
    assert "2" in s
    assert "Anton Osika" in s
    assert "May 01" in s
    print(f"  OK  subject includes count + top target + date: {s!r}")


def test_subject_line_zero_items():
    """Defensive: a zero-item digest still produces a coherent subject."""
    s = build_subject([], digest_date=datetime(2026, 5, 1, tzinfo=timezone.utc))
    assert "no new" in s.lower()
    print(f"  OK  zero-item subject is coherent: {s!r}")


def test_render_html_contains_target_name_and_evidence_url():
    items = [
        DigestItem(
            dossier_id="d1",
            target_name="Anton <Osika>",  # ensure HTML escaping works
            classification="founder",
            confidence=0.95,
            score=7.5,
            narrative="Builder of things.",
            recommended_action="Warm intro via Max",
            key_signals=[{"claim": "55K stars",
                          "supporting_url": "https://github.com/AntonOsika/gpt-engineer"}],
        ),
    ]
    html = render_html(items, web_app_url="https://app.example.com")
    assert "Anton &lt;Osika&gt;" in html, "name should be HTML-escaped"
    assert "https://github.com/AntonOsika/gpt-engineer" in html
    assert "https://app.example.com/dossier/d1" in html
    assert "Warm intro via Max" in html
    print("  OK  render_html: escapes special chars, includes evidence URL + dossier link")


def test_render_html_overflow_message():
    items = [DigestItem(dossier_id="d1", target_name="Anton", classification="founder",
                        confidence=0.95, score=7.5, narrative="x",
                        recommended_action="", key_signals=[])]
    html = render_html(items, web_app_url="https://app.example.com", overflow_count=12)
    assert "+ 12 more" in html
    print("  OK  render_html: overflow message renders when overflow_count > 0")


# --- Test runner --------------------------------------------------------

TESTS = [
    test_no_email_set_returns_skipped,
    test_disabled_returns_skipped,
    test_no_candidates_returns_skipped,
    test_happy_path_sends_email_and_flips_statuses,
    test_send_failure_preserves_status,
    test_dry_run_no_email_no_flip,
    test_query_thresholds_pass_through_to_cypher,
    test_subject_line_includes_count_and_top_target,
    test_subject_line_zero_items,
    test_render_html_contains_target_name_and_evidence_url,
    test_render_html_overflow_message,
]


def main() -> int:
    print(f"Running {len(TESTS)} M12-email notifier tests...\n")
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
