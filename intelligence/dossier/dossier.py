"""Dossier persistence (M9.5.2 / M9.5.3).

Builds or updates a Dossier node in Neo4j from an enrichment bundle + a
classification. Idempotent via SHA-256 hash of the bundle JSON: if the hash
hasn't changed and the existing dossier is `draft`, we skip the LLM call.

State machine:

    fresh / no draft        ──► create draft        (run classifier)
    draft, hash unchanged   ──► return existing     (skip classifier)
    draft, hash changed     ──► overwrite draft     (run classifier)
    sent (immutable)        ──► create new dated dossier alongside
    rejected                ──► do nothing on auto-runs (manual only)

Auto-promotion:
  if classification.role in rule.dossier_classifications_to_emit
  AND classification.confidence >= 0.85
  -> status = 'ready_to_send'
  else -> status = 'draft'
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from intelligence.dossier.classifier import (
    Classification,
    LLMClassifier,
    classify as _classify,
    validate_grounding,
)
from intelligence.dossier.enrichment import EnrichmentBundle

logger = logging.getLogger(__name__)


VALID_STATUSES = ("draft", "ready_to_send", "sent", "rejected", "failed")

AUTO_PROMOTE_CONFIDENCE_THRESHOLD = 0.85


# --- Cypher templates -----------------------------------------------------

UPSERT_DOSSIER = """
MERGE (d:Dossier {id: $id})
ON CREATE SET d.created_at = datetime($now_iso)
SET d.target_person_id      = $target_person_id,
    d.user_id               = $user_id,
    d.classification        = $classification,
    d.confidence            = $confidence,
    d.narrative             = $narrative,
    d.key_signals_json      = $key_signals_json,
    d.recommended_action    = $recommended_action,
    d.cross_check_kb_json   = $cross_check_kb_json,
    d.evidence_bundle_json  = $evidence_bundle_json,
    d.evidence_bundle_hash  = $evidence_bundle_hash,
    d.kb_cross_match_id     = $kb_cross_match_id,
    d.kb_cross_match_kind   = $kb_cross_match_kind,
    d.generated_at          = datetime($generated_at),
    d.llm_model             = $llm_model,
    d.llm_input_tokens      = $llm_input_tokens,
    d.status                = $status,
    d.status_updated_at     = datetime($now_iso)
WITH d
MATCH (target:Person {canonical_id: $target_person_id})
MERGE (d)-[:DOSSIER_FOR]->(target)
"""

LINK_BUILT_FROM = """
MATCH (d:Dossier {id: $dossier_id})
MATCH (e:ConvergenceEvent {id: $event_id})
MERGE (d)-[:BUILT_FROM]->(e)
"""

# M9.5.3: cross-match audit edge
MERGE_CROSS_SEEN_AUDIT = """
MATCH (e:ConvergenceEvent {id: $event_id})
MATCH (p:Person {canonical_id: $person_id})
MERGE (e)-[r:CROSS_SEEN_VIA_CONVERGENCE]->(p)
ON CREATE SET r.first_observed_at = datetime($now_iso)
SET r.last_observed_at = datetime($now_iso)
"""

# Find the most recent mutable Dossier for (user, target). Used by idempotency.
# 'sent' is immutable -> excluded. 'rejected'/'failed' -> excluded so we DO retry.
QUERY_LATEST_DRAFT = """
MATCH (d:Dossier {user_id: $user_id, target_person_id: $target_id})
WHERE d.status IN ['draft', 'ready_to_send']
RETURN d.id                    AS id,
       d.evidence_bundle_hash  AS hash,
       d.classification        AS classification,
       d.confidence             AS confidence,
       d.status                 AS status,
       toString(d.generated_at) AS generated_at
ORDER BY d.generated_at DESC
LIMIT 1
"""

# Find any dossier for (user, target). Used to decide if we'd be overwriting a 'sent' one.
QUERY_LATEST_ANY = """
MATCH (d:Dossier {user_id: $user_id, target_person_id: $target_id})
RETURN d.id     AS id,
       d.status AS status,
       toString(d.generated_at) AS generated_at
ORDER BY d.generated_at DESC
LIMIT 1
"""


# --- Result dataclass -----------------------------------------------------

@dataclass(frozen=True)
class DossierResult:
    dossier_id: str
    status: str
    classification: str
    confidence: float
    regenerated: bool                 # True if the LLM was called this run
    cached: bool                      # True if we returned an existing draft
    grounding_issues: list[str] = field(default_factory=list)
    is_new_dossier: bool = False      # True if a fresh node was created (vs overwrite)


# --- Pure helpers ---------------------------------------------------------

def compute_bundle_hash(bundle: EnrichmentBundle) -> str:
    return hashlib.sha256(bundle.to_json().encode("utf-8")).hexdigest()


def make_dossier_id(user_id: str, target_id: str, generated_at: datetime) -> str:
    """`dossier-{user}-{target_id}-{date}-{short-hash}`. The short hash is a
    counter-like suffix that lets multiple sent dossiers for the same target
    coexist on the same day if needed."""
    date_part = generated_at.strftime("%Y%m%d")
    nonce = generated_at.strftime("%H%M%S")
    return f"dossier-{user_id}-{target_id}-{date_part}-{nonce}"


def _decide_status(
    classification: Classification,
    *,
    classifications_to_emit: list[str],
    threshold: float,
) -> str:
    if (classification.role in classifications_to_emit
            and classification.confidence >= threshold):
        return "ready_to_send"
    return "draft"


# --- Main entry point ----------------------------------------------------

def build_or_update(
    session,
    *,
    user_id: str,
    bundle: EnrichmentBundle,
    triggering_event_ids: list[str],
    llm: LLMClassifier | None = None,
    classifications_to_emit: list[str] | None = None,
    threshold: float = AUTO_PROMOTE_CONFIDENCE_THRESHOLD,
    force_reclassify: bool = False,
    now: datetime | None = None,
) -> DossierResult:
    """Persist a Dossier for the bundle's target. Idempotent.

    Args:
      triggering_event_ids: ConvergenceEvent ids that this dossier is built from.
        At least one is expected. Used to attach BUILT_FROM edges and the
        CROSS_SEEN_VIA_CONVERGENCE audit edge when the target is a known investor.
      classifications_to_emit: roles for which auto-promotion to 'ready_to_send'
        is allowed. Default: ['founder'].
    """
    if classifications_to_emit is None:
        classifications_to_emit = ["founder"]
    now = now or datetime.now(timezone.utc)
    target_id = bundle.target_person.canonical_id
    bundle_hash = compute_bundle_hash(bundle)

    # 1. Idempotency check: if a draft exists with same hash AND a non-zero
    #    confidence (i.e. the LLM call actually succeeded last time), return it
    #    without calling the LLM. confidence==0 typically means a prior LLM
    #    failure; we should retry.
    if not force_reclassify:
        existing = session.run(
            QUERY_LATEST_DRAFT, user_id=user_id, target_id=target_id,
        ).single()
        if (existing
                and existing["hash"] == bundle_hash
                and float(existing["confidence"] or 0.0) > 0.0):
            return DossierResult(
                dossier_id=existing["id"],
                status=existing.get("status", "draft"),
                classification=existing["classification"],
                confidence=float(existing["confidence"] or 0.0),
                regenerated=False,
                cached=True,
            )

    # 2. Run classifier.
    classification = _classify(bundle, llm=llm)

    # 3. Validate grounding. Issues are logged but don't block persistence —
    #    they're surfaced in the result so callers (or M9.5.4 backend) can
    #    flag dossiers for review.
    issues = validate_grounding(bundle, classification)
    if issues:
        logger.warning("grounding issues for target %s: %s", target_id, issues)

    # 4. Decide status. Sent dossiers are immutable: if the latest is sent,
    #    we ALWAYS create a new dossier rather than overwrite.
    latest = session.run(QUERY_LATEST_ANY, user_id=user_id, target_id=target_id).single()
    create_new = (latest is None) or (latest["status"] == "sent")

    status = _decide_status(
        classification,
        classifications_to_emit=classifications_to_emit,
        threshold=threshold,
    )
    # If grounding had issues, downgrade to 'failed'.
    if issues:
        status = "failed"

    if create_new:
        dossier_id = make_dossier_id(user_id, target_id, now)
        is_new = True
    else:
        # Overwrite the existing draft (same id).
        dossier_id = latest["id"]
        is_new = False

    # 5. Resolve KB cross-match: if target is_known, capture their canonical_id and kind.
    kb = bundle.kb_match
    kb_cross_match_id = target_id if kb.is_known else None
    kb_cross_match_kind = "known_investor" if kb.is_known else "unknown"

    # 6. Persist Dossier node + edges.
    session.run(
        UPSERT_DOSSIER,
        id=dossier_id,
        target_person_id=target_id,
        user_id=user_id,
        classification=classification.role,
        confidence=classification.confidence,
        narrative=classification.narrative,
        key_signals_json=json.dumps(classification.key_signals),
        recommended_action=classification.recommended_action,
        cross_check_kb_json=json.dumps(classification.cross_check_kb),
        evidence_bundle_json=bundle.to_json(),
        evidence_bundle_hash=bundle_hash,
        kb_cross_match_id=kb_cross_match_id,
        kb_cross_match_kind=kb_cross_match_kind,
        generated_at=now.isoformat(),
        llm_model=classification.model,
        llm_input_tokens=classification.input_tokens,
        status=status,
        now_iso=now.isoformat(),
    )

    for ev_id in triggering_event_ids:
        session.run(LINK_BUILT_FROM, dossier_id=dossier_id, event_id=ev_id)

    # 7. KB cross-match audit edge (M9.5.3).
    if kb.is_known and triggering_event_ids:
        for ev_id in triggering_event_ids:
            session.run(
                MERGE_CROSS_SEEN_AUDIT,
                event_id=ev_id, person_id=target_id,
                now_iso=now.isoformat(),
            )

    return DossierResult(
        dossier_id=dossier_id,
        status=status,
        classification=classification.role,
        confidence=classification.confidence,
        regenerated=True,
        cached=False,
        grounding_issues=issues,
        is_new_dossier=is_new,
    )
