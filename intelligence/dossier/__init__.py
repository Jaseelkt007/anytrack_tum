"""Investigation Layer (M9.5).

Turns each ConvergenceEvent (a *lead*) into a vetted Dossier (a *case file*) by:

  1. Enrichment — gather GitHub profile + owned repos, Twitter profile + recent
     tweets, convergence evidence, cross-platform watchers, KB cross-match
  2. Classification — Gemini judges role: founder | investor | operator |
     unclear | not_relevant; produces a per-sentence-grounded narrative
  3. Persistence — Dossier node with full evidence_bundle_hash for idempotency,
     status state machine (draft -> ready_to_send -> sent), immutability of
     sent dossiers

This is the verification step that distinguishes the system from Specter (which
Omar described as 'fake information'). Every claim in a dossier must be
grounded in a clickable evidence URL from the bundle.
"""

from intelligence.dossier.enrichment import (
    EnrichmentBundle,
    GitHubProfile,
    OwnedRepo,
    TwitterProfile,
    TweetSummary,
    ConvergenceEvidence,
    CrossPlatformFollower,
    KBMatch,
    enrich,
)

__all__ = [
    "ConvergenceEvidence",
    "CrossPlatformFollower",
    "EnrichmentBundle",
    "GitHubProfile",
    "KBMatch",
    "OwnedRepo",
    "TweetSummary",
    "TwitterProfile",
    "enrich",
]
