"""Cross-platform identity resolution (Phase 2 / M7).

Routes every Person upsert through three deterministic tiers before
falling back to fresh canonical_id generation:

  1. Override CSV (data/identity_overrides.csv) — hand-curated cross-platform pairs
  2. Bio-link extraction — explicit URLs in profile bios matching existing Persons
  3. LLM arbitration — only if 1 & 2 miss AND candidate_finder returns ≥ 1

The NAMESPACE UUID and canonical_id schemes are inherited from Phase 1
(see scrapers/cypher.py:github_person_id, scripts/load_investor_reference.py).
Changing NAMESPACE would break Phase 1 idempotency.
"""

from identity.resolver import (
    NAMESPACE,
    LLM_MERGE_CONFIDENCE_THRESHOLD,
    OverrideIndex,
    Neo4jIdentityLookup,
    Resolver,
    ResolveResult,
    build_default_resolver,
    platform_person_id,
    resolve_identity,
)

__all__ = [
    "NAMESPACE",
    "LLM_MERGE_CONFIDENCE_THRESHOLD",
    "OverrideIndex",
    "Neo4jIdentityLookup",
    "Resolver",
    "ResolveResult",
    "build_default_resolver",
    "platform_person_id",
    "resolve_identity",
]
