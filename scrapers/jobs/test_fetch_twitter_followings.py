"""Unit tests for the Twitter follow-list snapshot/diff job.

No network. No Scrapebadger. Uses a FakeTwitterFollowingClient so every code
path can be exercised in isolation, per the brief's "Tests To Implement" list.

Run:
    python scrapers/jobs/test_fetch_twitter_followings.py
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from scrapers.clients.twitter_following_client import (
    FollowingsPage,
    TwitterUserRecord,
)
from scrapers.jobs.fetch_twitter_followings import (
    CONFIDENCE_BASELINE,
    CONFIDENCE_DIFF,
    JobConfig,
    fetch_one_account,
    load_handles,
    parse_handle,
)


# --- Fake client -----------------------------------------------------------

class FakeTwitterFollowingClient:
    """Configurable in-memory stand-in for ScrapebadgerClient."""

    def __init__(self, followings: dict[str, list[TwitterUserRecord]] | None = None):
        self._followings = followings or {}
        self.lookup_calls: list[str] = []
        self.list_calls: list[tuple[str, str | None]] = []

    def set_followings(self, handle: str, users: list[TwitterUserRecord]) -> None:
        self._followings[handle.lower()] = users

    def lookup_user(self, username: str) -> TwitterUserRecord:
        self.lookup_calls.append(username)
        return TwitterUserRecord(id="1", username=username, name=username.title())

    def list_followings(self, username: str, cursor: str | None = None) -> FollowingsPage:
        self.list_calls.append((username, cursor))
        users = self._followings.get(username.lower(), [])
        return FollowingsPage(users=list(users), next_cursor=None)


def _user(name: str, *, followers: int = 0, verified: bool = False) -> TwitterUserRecord:
    return TwitterUserRecord(
        id=str(abs(hash(name)) % 10_000_000),
        username=name,
        name=name.title(),
        verified=verified,
        followers_count=followers,
    )


def _read_signals(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


# --- Fixture --------------------------------------------------------------

class TmpJob:
    def __init__(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="m8-test-"))
        self.snapshot_dir = self.tmp / "snapshots"
        self.signals_file = self.tmp / "signals.jsonl"

    def config(self, **overrides) -> JobConfig:
        defaults = dict(
            snapshot_dir=self.snapshot_dir,
            signals_file=self.signals_file,
            target_set=None,
            include_existing=False,
            max_pages=1,
            write_snapshots=True,
        )
        defaults.update(overrides)
        return JobConfig(**defaults)

    def cleanup(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


# --- Tests ----------------------------------------------------------------

def test_first_run_baselines_emits_zero():
    """Brief #1: first run baselines and emits zero signals by default."""
    fix = TmpJob()
    try:
        client = FakeTwitterFollowingClient()
        client.set_followings("naga", [_user("angellist"), _user("ycombinator")])
        result = fetch_one_account(client, "naga", fix.config())
        assert result.snapshot_written
        assert result.signals_emitted == 0, result
        assert result.error is None
        signals = _read_signals(fix.signals_file)
        assert signals == [], signals
        snapshot = json.loads((fix.snapshot_dir / "naga.json").read_text())
        assert {u["username"] for u in snapshot["following"]} == {"angellist", "ycombinator"}
        print("  OK  first run: snapshot written, zero signals (default)")
    finally:
        fix.cleanup()


def test_first_run_include_existing_emits_baseline():
    """Brief #2: first run with include_existing=True emits current follows
    at baseline confidence."""
    fix = TmpJob()
    try:
        client = FakeTwitterFollowingClient()
        client.set_followings("naga", [_user("angellist"), _user("ycombinator")])
        result = fetch_one_account(client, "naga", fix.config(include_existing=True))
        assert result.signals_emitted == 2
        signals = _read_signals(fix.signals_file)
        assert all(s["confidence"] == CONFIDENCE_BASELINE for s in signals)
        assert all(s["metadata"]["timing_basis"] == "baseline_existing_follow" for s in signals)
        targets = sorted(s["target"] for s in signals)
        assert targets == ["twitter:angellist", "twitter:ycombinator"], targets
        print(f"  OK  first run + include_existing: 2 baseline signals @{CONFIDENCE_BASELINE}")
    finally:
        fix.cleanup()


def test_diff_emits_only_new():
    """Brief #3: second run emits only current usernames not in previous snapshot."""
    fix = TmpJob()
    try:
        client = FakeTwitterFollowingClient()
        client.set_followings("naga", [_user("angellist"), _user("ycombinator")])
        # First run: baseline (no signals).
        fetch_one_account(client, "naga", fix.config())
        # Second run: add one new follow, drop none.
        client.set_followings("naga", [
            _user("angellist"), _user("ycombinator"), _user("antonosika"),
        ])
        result = fetch_one_account(client, "naga", fix.config())
        assert result.signals_emitted == 1, result
        signals = _read_signals(fix.signals_file)
        assert len(signals) == 1
        s = signals[0]
        assert s["target"] == "twitter:antonosika"
        assert s["confidence"] == CONFIDENCE_DIFF
        assert s["metadata"]["timing_basis"] == "first_observed_snapshot_diff"
        print(f"  OK  diff run: 1 new follow emitted @{CONFIDENCE_DIFF}")
    finally:
        fix.cleanup()


def test_target_filter_excludes_non_targets():
    """Brief #4: target list filters out non-target follows."""
    fix = TmpJob()
    try:
        client = FakeTwitterFollowingClient()
        client.set_followings("naga", [_user("angellist")])
        # Baseline.
        fetch_one_account(client, "naga", fix.config())
        # Add two new follows; only one is a target.
        client.set_followings("naga", [
            _user("angellist"), _user("antonosika"), _user("randomperson"),
        ])
        result = fetch_one_account(
            client, "naga",
            fix.config(target_set={"antonosika"}),
        )
        assert result.signals_emitted == 1, result
        signals = _read_signals(fix.signals_file)
        assert signals[0]["target"] == "twitter:antonosika"
        print("  OK  target filter: only targeted handle emits")
    finally:
        fix.cleanup()


def test_all_following_mode_emits_any_new():
    """Brief #5: all_following=True emits any new follow (target_set=None)."""
    fix = TmpJob()
    try:
        client = FakeTwitterFollowingClient()
        client.set_followings("naga", [_user("angellist")])
        fetch_one_account(client, "naga", fix.config())
        client.set_followings("naga", [
            _user("angellist"), _user("a"), _user("b"), _user("c"),
        ])
        result = fetch_one_account(client, "naga", fix.config(target_set=None))
        assert result.signals_emitted == 3, result
        targets = sorted(s["target"] for s in _read_signals(fix.signals_file))
        assert targets == ["twitter:a", "twitter:b", "twitter:c"]
        print("  OK  all-following mode: every new follow emitted")
    finally:
        fix.cleanup()


def test_handle_parser_accepts_variants():
    """Brief #6: handle parser accepts @handle, x.com/handle, twitter.com/handle."""
    cases = [
        ("naga",                              "naga"),
        ("@naga",                             "naga"),
        ("https://x.com/naga",                "naga"),
        ("https://twitter.com/naga",          "naga"),
        ("https://www.x.com/naga/",           "naga"),
        ("HTTP://X.COM/Naga",                 "naga"),
        ("twitter.com/_nagaa____",            None),  # bare host w/o https — be strict
    ]
    for raw, expected in cases:
        got = parse_handle(raw)
        assert got == expected, f"parse_handle({raw!r}) -> {got!r}, expected {expected!r}"
    # And the bare-handle line case:
    assert parse_handle("_nagaa____") == "_nagaa____"
    print("  OK  handle parser: @, https://x.com, https://twitter.com all accepted")


def test_load_handles_skips_blanks_and_comments():
    fix = TmpJob()
    try:
        f = fix.tmp / "wl.txt"
        f.write_text("\n".join([
            "# header comment",
            "naga",
            "",
            "@karpathy",
            "https://x.com/sama",
            "  ",
            "# trailing comment",
        ]))
        handles = load_handles(f)
        assert handles == ["naga", "karpathy", "sama"], handles
        print("  OK  load_handles: skips blanks/comments, normalizes")
    finally:
        fix.cleanup()


def test_lookup_user_failure_returns_error():
    """Brief #7 (in spirit): per-account failure isolates."""
    fix = TmpJob()
    try:
        class Boom(FakeTwitterFollowingClient):
            def lookup_user(self, username):
                raise RuntimeError("simulated 500")

        result = fetch_one_account(Boom(), "naga", fix.config())
        assert result.error and "simulated 500" in result.error
        assert result.signals_emitted == 0
        assert not result.snapshot_written
        # Subsequent call to a healthy account would succeed (caller-orchestrated).
        print("  OK  per-account error contained, no signals/snapshot leaked")
    finally:
        fix.cleanup()


def test_snapshot_records_max_pages_for_depth_warning():
    """Brief #8: snapshot depth warning is encoded — we record max_pages."""
    fix = TmpJob()
    try:
        client = FakeTwitterFollowingClient()
        client.set_followings("naga", [_user("a")])
        fetch_one_account(client, "naga", fix.config(max_pages=1))
        snap = json.loads((fix.snapshot_dir / "naga.json").read_text())
        assert snap["max_pages"] == 1, snap
        print("  OK  snapshot persists max_pages so depth-mismatch can be detected")
    finally:
        fix.cleanup()


def test_no_write_snapshots_does_not_persist():
    fix = TmpJob()
    try:
        client = FakeTwitterFollowingClient()
        client.set_followings("naga", [_user("a")])
        result = fetch_one_account(client, "naga", fix.config(write_snapshots=False))
        assert not result.snapshot_written
        assert not (fix.snapshot_dir / "naga.json").exists()
        print("  OK  write_snapshots=False does not write to disk")
    finally:
        fix.cleanup()


def test_diff_persists_new_snapshot():
    """After a diff run, the snapshot file reflects the CURRENT state (not the prior)."""
    fix = TmpJob()
    try:
        client = FakeTwitterFollowingClient()
        client.set_followings("naga", [_user("a")])
        fetch_one_account(client, "naga", fix.config())
        client.set_followings("naga", [_user("a"), _user("b")])
        fetch_one_account(client, "naga", fix.config())
        snap = json.loads((fix.snapshot_dir / "naga.json").read_text())
        names = sorted(u["username"] for u in snap["following"])
        assert names == ["a", "b"], names
        print("  OK  diff run persists the NEW snapshot (state moves forward)")
    finally:
        fix.cleanup()


def test_signal_id_format_and_evidence_url():
    """The signal id and evidence_url match the brief's contract."""
    fix = TmpJob()
    try:
        client = FakeTwitterFollowingClient()
        client.set_followings("naga", [_user("angellist")])
        ts = datetime(2026, 5, 1, 12, 0, 0, tzinfo=timezone.utc)
        fetch_one_account(client, "naga", fix.config(include_existing=True), now=ts)
        s = _read_signals(fix.signals_file)[0]
        assert s["id"].startswith("scrapebadger-twitter-follow:naga:angellist:")
        assert s["id"].endswith("20260501T120000Z"), s["id"]
        assert s["evidence_url"] == "https://x.com/naga/following"
        assert s["actor"] == "twitter:naga"
        assert s["target"] == "twitter:angellist"
        assert s["metadata"]["api_evidence_url"] == \
            "https://scrapebadger.com/v1/twitter/users/naga/followings"
        print("  OK  signal id + evidence_url match the brief's exact format")
    finally:
        fix.cleanup()


# --- Test runner ----------------------------------------------------------

TESTS = [
    test_first_run_baselines_emits_zero,
    test_first_run_include_existing_emits_baseline,
    test_diff_emits_only_new,
    test_target_filter_excludes_non_targets,
    test_all_following_mode_emits_any_new,
    test_handle_parser_accepts_variants,
    test_load_handles_skips_blanks_and_comments,
    test_lookup_user_failure_returns_error,
    test_snapshot_records_max_pages_for_depth_warning,
    test_no_write_snapshots_does_not_persist,
    test_diff_persists_new_snapshot,
    test_signal_id_format_and_evidence_url,
]


def main() -> int:
    print(f"Running {len(TESTS)} M8 Twitter-followings tests...\n")
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
