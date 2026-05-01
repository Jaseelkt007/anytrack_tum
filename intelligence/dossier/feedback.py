"""Dossier feedback persistence (M9.5.5).

Records the user's verdict on each Dossier as an append-only `DossierFeedback`
node, optionally flipping the underlying Dossier to `status='rejected'` for
negative verdicts. The feedback table is the labelling source M11 (Bayesian
per-watcher precision) will eventually consume; M12's email digest will use
it to skip rejected dossiers.

Side-effect rules (verdict -> dossier status transition):

  correct                   -> no status change
  low_priority              -> no status change
  wrong_classification      -> flip to 'rejected' (unless already 'sent')
  wrong_target              -> flip to 'rejected' (unless already 'sent')
  spam                      -> flip to 'rejected' (unless already 'sent')

Sent dossiers are immutable per the M9.5 contract — feedback is recorded but
status is preserved (the email already went out, can't unring that bell).

Append-only: never edit a feedback record; submit a new one to "change your
mind." The full chronological history is the audit trail.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

VALID_VERDICTS = (
    "correct",
    "wrong_classification",
    "wrong_target",
    "spam",
    "low_priority",
)

STATUS_FLIP_VERDICTS = frozenset({
    "wrong_classification",
    "wrong_target",
    "spam",
})

# Mirrors intelligence.dossier.classifier.VALID_ROLES — duplicated to avoid the
# import cycle and to keep this module self-contained.
VALID_CORRECTED_ROLES = (
    "founder",
    "investor",
    "operator",
    "unclear",
    "not_relevant",
)


# --- Result ----------------------------------------------------------------

@dataclass(frozen=True)
class FeedbackResult:
    feedback_id: str
    dossier_id: str
    verdict: str
    submitted_at: str
    side_effect: str | None = None        # 'dossier_status_changed_to_rejected' or None
    new_dossier_status: str | None = None  # set when side_effect fired


# --- Cypher templates ------------------------------------------------------

INSERT_DOSSIER_FEEDBACK = """
MATCH (d:Dossier {id: $dossier_id})
MERGE (fb:DossierFeedback {id: $id})
ON CREATE SET
    fb.dossier_id              = $dossier_id,
    fb.target_person_id        = d.target_person_id,
    fb.user_id                 = $user_id,
    fb.verdict                 = $verdict,
    fb.corrected_classification = $corrected_classification,
    fb.notes                   = $notes,
    fb.submitted_at            = datetime($now_iso),
    fb.side_effect             = $side_effect
MERGE (fb)-[:FEEDBACK_FOR]->(d)
WITH fb, d
MATCH (target:Person {canonical_id: d.target_person_id})
MERGE (fb)-[:ABOUT_TARGET]->(target)
RETURN fb.id AS id
"""

# Flip Dossier status to 'rejected' iff it's currently mutable. Returns the
# resulting status so the caller knows whether the side effect actually fired.
FLIP_DOSSIER_STATUS_REJECTED = """
MATCH (d:Dossier {id: $dossier_id})
WITH d, d.status AS prior_status
SET d.status = CASE
    WHEN prior_status IN ['draft', 'ready_to_send'] THEN 'rejected'
    ELSE prior_status
END,
    d.status_updated_at = datetime($now_iso)
RETURN d.status AS new_status, prior_status
"""

LOOKUP_DOSSIER_STATUS = """
MATCH (d:Dossier {id: $dossier_id})
RETURN d.status AS status, d.target_person_id AS target_person_id
"""

LIST_FEEDBACK_FOR_DOSSIER = """
MATCH (fb:DossierFeedback {dossier_id: $dossier_id})
RETURN fb.id                       AS id,
       fb.dossier_id               AS dossier_id,
       fb.target_person_id         AS target_person_id,
       fb.user_id                  AS user_id,
       fb.verdict                  AS verdict,
       fb.corrected_classification AS corrected_classification,
       fb.notes                    AS notes,
       toString(fb.submitted_at)   AS submitted_at,
       fb.side_effect              AS side_effect
ORDER BY fb.submitted_at ASC
"""

LIST_FEEDBACK_SINCE = """
MATCH (fb:DossierFeedback {user_id: $user_id})
WHERE $since IS NULL OR fb.submitted_at >= datetime($since)
RETURN fb.id                       AS id,
       fb.dossier_id               AS dossier_id,
       fb.target_person_id         AS target_person_id,
       fb.user_id                  AS user_id,
       fb.verdict                  AS verdict,
       fb.corrected_classification AS corrected_classification,
       fb.notes                    AS notes,
       toString(fb.submitted_at)   AS submitted_at,
       fb.side_effect              AS side_effect
ORDER BY fb.submitted_at DESC
LIMIT $limit
"""

COUNT_FEEDBACK_FOR_DOSSIER = """
MATCH (fb:DossierFeedback {dossier_id: $dossier_id})
RETURN count(fb) AS feedback_count
"""


# --- Errors ----------------------------------------------------------------

class FeedbackValidationError(ValueError):
    """Raised when feedback fields are invalid (verdict, corrected_classification)."""


class DossierNotFoundError(LookupError):
    """Raised when the target dossier doesn't exist."""


# --- Validation ------------------------------------------------------------

def _validate(verdict: str, corrected_classification: str | None) -> None:
    if verdict not in VALID_VERDICTS:
        raise FeedbackValidationError(
            f"verdict {verdict!r} not in {list(VALID_VERDICTS)}"
        )
    if verdict == "wrong_classification":
        if not corrected_classification:
            raise FeedbackValidationError(
                "verdict='wrong_classification' requires corrected_classification"
            )
        if corrected_classification not in VALID_CORRECTED_ROLES:
            raise FeedbackValidationError(
                f"corrected_classification {corrected_classification!r} not in "
                f"{list(VALID_CORRECTED_ROLES)}"
            )


# --- Helpers ---------------------------------------------------------------

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _make_feedback_id(user_id: str, dossier_id: str, ts: datetime) -> str:
    """Stable id keyed on submission timestamp; collisions essentially impossible
    at <1s granularity for the same user+dossier."""
    return f"fb-{user_id}-{dossier_id}-{ts.strftime('%Y%m%dT%H%M%S%f')}"


# --- Public API ------------------------------------------------------------

def submit_feedback(
    session,
    *,
    user_id: str,
    dossier_id: str,
    verdict: str,
    corrected_classification: str | None = None,
    notes: str | None = None,
    now: datetime | None = None,
) -> FeedbackResult:
    """Persist one feedback event and (when the verdict warrants) flip the
    underlying Dossier to status='rejected'. Always append-only on
    DossierFeedback; never edits an existing feedback record."""
    _validate(verdict, corrected_classification)

    # Look up the dossier to confirm it exists and to discover its current status.
    rec = session.run(LOOKUP_DOSSIER_STATUS, dossier_id=dossier_id).single()
    if rec is None:
        raise DossierNotFoundError(f"no Dossier with id={dossier_id}")
    current_status = rec["status"]

    now = now or _now()
    feedback_id = _make_feedback_id(user_id, dossier_id, now)

    # Decide the side effect BEFORE we write so we can record it on the feedback row.
    side_effect: str | None = None
    new_status: str | None = None
    if verdict in STATUS_FLIP_VERDICTS and current_status in ("draft", "ready_to_send"):
        side_effect = "dossier_status_changed_to_rejected"
        new_status = "rejected"

    # If notes is empty string, normalize to None for cleaner JSON.
    notes_to_store = (notes or None) if notes != "" else None

    session.run(
        INSERT_DOSSIER_FEEDBACK,
        id=feedback_id,
        dossier_id=dossier_id,
        user_id=user_id,
        verdict=verdict,
        corrected_classification=corrected_classification,
        notes=notes_to_store,
        now_iso=now.isoformat(),
        side_effect=side_effect,
    )

    if side_effect == "dossier_status_changed_to_rejected":
        session.run(
            FLIP_DOSSIER_STATUS_REJECTED,
            dossier_id=dossier_id,
            now_iso=now.isoformat(),
        )

    return FeedbackResult(
        feedback_id=feedback_id,
        dossier_id=dossier_id,
        verdict=verdict,
        submitted_at=now.isoformat(),
        side_effect=side_effect,
        new_dossier_status=new_status,
    )


def list_feedback_for_dossier(session, dossier_id: str) -> list[dict[str, Any]]:
    rows = session.run(LIST_FEEDBACK_FOR_DOSSIER, dossier_id=dossier_id).data()
    return [dict(r) for r in rows]


def list_feedback_for_user(
    session,
    user_id: str,
    *,
    since: datetime | str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    since_iso = since.isoformat() if isinstance(since, datetime) else since
    rows = session.run(
        LIST_FEEDBACK_SINCE,
        user_id=user_id, since=since_iso, limit=limit,
    ).data()
    return [dict(r) for r in rows]


def count_feedback_for_dossier(session, dossier_id: str) -> int:
    rec = session.run(COUNT_FEEDBACK_FOR_DOSSIER, dossier_id=dossier_id).single()
    return int(rec["feedback_count"]) if rec else 0
