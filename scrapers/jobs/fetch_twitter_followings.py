"""Twitter follow-list snapshot/diff job.

Implements the contract in CLAUDE_SCRAPEBADGER_IMPLEMENTATION_BRIEF.md:

  First run (no prior snapshot)
    -> save snapshot, emit zero signals (or all current as baseline if
       include_existing=True, with confidence 0.76).

  Diff run (prior snapshot exists)
    -> emit usernames present in current page-N but not in prior page-N,
       confidence 0.91, save the new snapshot.

  Optional target filter — only emit signals whose target matches the
  configured target list. With all_following=True the filter is bypassed.

Wording rule: never claim exact follow timestamps. Signal field
`occurred_at` is set to `observed_at` (poll time); `timing_basis` in the
metadata makes the semantics explicit.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from scrapers.clients.twitter_following_client import (
    TwitterFollowingClient,
    TwitterUserRecord,
    iter_followings,
)

logger = logging.getLogger(__name__)


# --- Confidences from the brief -------------------------------------------

CONFIDENCE_DIFF = 0.91
CONFIDENCE_BASELINE = 0.76


# --- Handle parsing -------------------------------------------------------

_HANDLE_LINE_PATTERNS = [
    re.compile(r"^https?://(?:www\.)?(?:twitter|x)\.com/([A-Za-z0-9_]{1,15})/?", re.IGNORECASE),
    re.compile(r"^@?([A-Za-z0-9_]{1,15})$"),
]


def parse_handle(line: str) -> str | None:
    """Accept '@h', 'h', 'twitter.com/h', 'x.com/h', 'https://x.com/h'.
    Return the bare lower-cased handle, or None if the line doesn't look like one."""
    line = (line or "").strip()
    if not line or line.startswith("#"):
        return None
    for pat in _HANDLE_LINE_PATTERNS:
        m = pat.match(line)
        if m:
            return m.group(1).lower()
    return None


def load_handles(path: Path) -> list[str]:
    out: list[str] = []
    with path.open(encoding="utf-8") as f:
        for raw in f:
            h = parse_handle(raw)
            if h:
                out.append(h)
    return out


# --- Signal record --------------------------------------------------------

@dataclass(frozen=True)
class FollowSignal:
    id: str
    source: str
    type: str
    actor: str            # "twitter:<handle>"
    target: str           # "twitter:<handle>"
    observed_at: str
    occurred_at: str
    evidence_url: str
    confidence: float
    metadata: dict

    def to_jsonl(self) -> str:
        return json.dumps({
            "id": self.id,
            "source": self.source,
            "type": self.type,
            "actor": self.actor,
            "target": self.target,
            "observed_at": self.observed_at,
            "occurred_at": self.occurred_at,
            "evidence_url": self.evidence_url,
            "confidence": self.confidence,
            "metadata": self.metadata,
        }, ensure_ascii=False)


def _build_signal(
    watched: str,
    target: TwitterUserRecord,
    observed_at: datetime,
    confidence: float,
    timing_basis: str,
    pages_fetched: int,
) -> FollowSignal:
    ts_compact = observed_at.strftime("%Y%m%dT%H%M%SZ")
    return FollowSignal(
        id=f"scrapebadger-twitter-follow:{watched.lower()}:{target.username.lower()}:{ts_compact}",
        source="twitter",
        type="twitter_follow",
        actor=f"twitter:{watched.lower()}",
        target=f"twitter:{target.username.lower()}",
        observed_at=observed_at.isoformat(),
        occurred_at=observed_at.isoformat(),
        evidence_url=f"https://x.com/{watched}/following",
        confidence=confidence,
        metadata={
            "provider": "scrapebadger",
            "target_url": f"https://x.com/{target.username}",
            "target_id": target.id,
            "api_evidence_url": f"https://scrapebadger.com/v1/twitter/users/{watched}/followings",
            "timing_basis": timing_basis,
            "pages_fetched": pages_fetched,
            "target_followers_count": target.followers_count,
            "target_verified": target.verified,
            "target_display_name": target.name,
        },
    )


# --- Snapshot I/O ---------------------------------------------------------

def _snapshot_path(snapshot_dir: Path, handle: str) -> Path:
    return snapshot_dir / f"{handle.lower()}.json"


def load_snapshot(snapshot_dir: Path, handle: str) -> dict | None:
    path = _snapshot_path(snapshot_dir, handle)
    if not path.exists():
        return None
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def write_snapshot(snapshot_dir: Path, handle: str, payload: dict) -> Path:
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    path = _snapshot_path(snapshot_dir, handle)
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return path


def _build_snapshot_payload(
    handle: str,
    subject_id: str,
    captured_at: datetime,
    users: Iterable[TwitterUserRecord],
    max_pages: int,
) -> dict:
    return {
        "source": "twitter",
        "provider": "scrapebadger",
        "subject": f"twitter:{handle.lower()}",
        "subject_id": subject_id,
        "captured_at": captured_at.isoformat(),
        "max_pages": max_pages,
        "following": [
            {
                "id": u.id,
                "username": u.username,
                "name": u.name,
                "followers_count": u.followers_count,
                "following_count": u.following_count,
                "verified": u.verified,
            }
            for u in users
        ],
    }


# --- Job ------------------------------------------------------------------

@dataclass
class JobConfig:
    snapshot_dir: Path
    signals_file: Path
    target_set: set[str] | None = None      # None == all-following mode
    include_existing: bool = False
    max_pages: int = 1
    write_snapshots: bool = True


@dataclass
class JobResult:
    handle: str
    snapshot_written: bool
    signals_emitted: int
    error: str | None = None
    new_targets: list[str] = field(default_factory=list)


def fetch_one_account(
    client: TwitterFollowingClient,
    handle: str,
    config: JobConfig,
    *,
    now: datetime | None = None,
) -> JobResult:
    """Fetch one watched account. Snapshot+diff vs prior, append matching
    signals to the JSONL file, return a per-account result."""
    handle = handle.lower()
    captured_at = now or datetime.now(timezone.utc)

    try:
        user_record = client.lookup_user(handle)
    except Exception as e:
        return JobResult(handle, False, 0, error=f"lookup_user: {type(e).__name__}: {e}")

    try:
        current_users = list(iter_followings(client, handle, max_pages=config.max_pages))
    except Exception as e:
        return JobResult(handle, False, 0, error=f"list_followings: {type(e).__name__}: {e}")

    prior = load_snapshot(config.snapshot_dir, handle)
    prior_usernames: set[str] = set()
    prior_max_pages: int | None = None
    if prior:
        prior_usernames = {
            (u.get("username") or "").lower()
            for u in prior.get("following", [])
            if u.get("username")
        }
        prior_max_pages = prior.get("max_pages")

    if prior_max_pages is not None and prior_max_pages != config.max_pages:
        logger.warning(
            "snapshot depth mismatch for %s: prior=%s current=%s — diff may include "
            "false 'newly observed' signals (see brief Known Limitations).",
            handle, prior_max_pages, config.max_pages,
        )

    new_records: list[TwitterUserRecord] = []
    if prior is None:
        if config.include_existing:
            new_records = list(current_users)
            timing_basis = "baseline_existing_follow"
            confidence = CONFIDENCE_BASELINE
        else:
            timing_basis = ""
            confidence = 0.0
    else:
        new_records = [
            u for u in current_users
            if u.username and u.username.lower() not in prior_usernames
        ]
        timing_basis = "first_observed_snapshot_diff"
        confidence = CONFIDENCE_DIFF

    targets = config.target_set
    emit_records = [
        u for u in new_records
        if targets is None or u.username.lower() in targets
    ]

    config.signals_file.parent.mkdir(parents=True, exist_ok=True)
    signals_emitted = 0
    new_targets: list[str] = []
    if emit_records:
        with config.signals_file.open("a", encoding="utf-8") as f:
            for target in emit_records:
                signal = _build_signal(
                    watched=handle,
                    target=target,
                    observed_at=captured_at,
                    confidence=confidence,
                    timing_basis=timing_basis,
                    pages_fetched=config.max_pages,
                )
                f.write(signal.to_jsonl() + "\n")
                signals_emitted += 1
                new_targets.append(target.username)

    snapshot_written = False
    if config.write_snapshots:
        payload = _build_snapshot_payload(
            handle=handle,
            subject_id=user_record.id or "",
            captured_at=captured_at,
            users=current_users,
            max_pages=config.max_pages,
        )
        write_snapshot(config.snapshot_dir, handle, payload)
        snapshot_written = True

    return JobResult(
        handle=handle,
        snapshot_written=snapshot_written,
        signals_emitted=signals_emitted,
        new_targets=new_targets,
    )
