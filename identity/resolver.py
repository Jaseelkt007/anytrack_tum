"""Three-tier identity resolver. Every cross-platform Person upsert SHOULD route
through `resolve_identity` so that the same real person is not split into
duplicate canonical_ids across platforms.

Tiers (short-circuit on first hit):
  1. Override CSV   -> data/identity_overrides.csv (hand-curated pairs)
  2. Bio-link       -> explicit URLs in profile bio matching existing Persons
  3. LLM (gated)    -> only if 1 & 2 miss AND candidate_finder returns >= 1
                       Auto-merge requires verdict.decision == 'same' AND
                       verdict.confidence >= LLM_MERGE_CONFIDENCE_THRESHOLD
  4. Fresh Person   -> deterministic uuid5(NAMESPACE, "{prefix}:{handle}")

The NAMESPACE constant matches scrapers/cypher.py and load_investor_reference.py.
The github prefix scheme `gh:<handle>` is identical to scrapers/cypher.py:
github_person_id, so existing Phase 1 ids do not change.
"""

from __future__ import annotations

import csv
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from identity.bio_link_extractor import extract_platform_links
from identity.candidate_finder import (
    CandidateFinder,
    CandidatePerson,
    Neo4jCandidateFinder,
)
from identity.llm_arbiter import LLMArbiter, LLMVerdict


NAMESPACE = uuid.UUID("8e1b3f2a-1c5d-4e7f-9a8b-2c3d4e5f6a7b")

PLATFORM_PREFIX = {"github": "gh", "twitter": "tw", "linkedin": "li"}

LLM_MERGE_CONFIDENCE_THRESHOLD = 0.85


def platform_person_id(platform: str, handle: str) -> str:
    """Deterministic canonical_id for a Person known only by one platform handle.

    For platform='github', this returns the same value as
    scrapers/cypher.py:github_person_id — verified by test.
    """
    prefix = PLATFORM_PREFIX.get(platform, platform[:2])
    return str(uuid.uuid5(NAMESPACE, f"{prefix}:{handle.lower()}"))


@dataclass
class ResolveResult:
    canonical_id: str
    tier: str                    # 'override' | 'bio_link' | 'llm_match' | 'fresh'
    confidence: float
    reasoning: str
    candidates_considered: list[str] = field(default_factory=list)
    llm_verdict: LLMVerdict | None = None


# --- Tier 1: override index ---------------------------------------------------

class OverrideIndex:
    """In-memory index over data/identity_overrides.csv.

    Maps every (platform, handle.lower()) appearing in the CSV to the same
    canonical_id (computed once per row using whichever handle is present,
    matching the scheme in scripts/load_identity_overrides.py).
    """

    def __init__(self, rows: list[dict[str, str]] | None = None):
        self._index: dict[tuple[str, str], str] = {}
        if rows is not None:
            self.load(rows)

    @classmethod
    def from_csv(cls, path: Path) -> "OverrideIndex":
        idx = cls()
        if not path.exists():
            return idx
        with path.open(encoding="utf-8") as f:
            idx.load(list(csv.DictReader(f)))
        return idx

    def load(self, rows: list[dict[str, str]]) -> None:
        for row in rows:
            gh = (row.get("github_handle") or "").strip()
            li = (row.get("linkedin_slug") or "").strip()
            tw = (row.get("twitter_handle") or "").strip()

            if gh:
                cid = platform_person_id("github", gh)
            elif li:
                cid = platform_person_id("linkedin", li)
            elif tw:
                cid = platform_person_id("twitter", tw)
            else:
                continue

            for platform, handle in (("github", gh), ("linkedin", li), ("twitter", tw)):
                if handle:
                    self._index[(platform, handle.lower())] = cid

    def lookup(self, platform: str, handle: str) -> str | None:
        return self._index.get((platform, handle.lower()))

    def __len__(self) -> int:
        return len(self._index)


# --- Tier 2: existing-PlatformIdentity lookup --------------------------------

class IdentityLookup(Protocol):
    """Returns canonical_id for an existing PlatformIdentity, or None."""
    def find_canonical_id(self, platform: str, handle: str) -> str | None: ...


_LOOKUP_QUERY = """
MATCH (p:Person)-[:HAS_IDENTITY]->(i:PlatformIdentity {platform: $platform, handle: $handle})
RETURN p.canonical_id AS canonical_id LIMIT 1
"""


class Neo4jIdentityLookup:
    def __init__(self, session):
        self._session = session

    def find_canonical_id(self, platform: str, handle: str) -> str | None:
        rec = self._session.run(_LOOKUP_QUERY, platform=platform, handle=handle).single()
        if rec:
            return rec["canonical_id"]
        if handle != handle.lower():
            rec = self._session.run(_LOOKUP_QUERY, platform=platform, handle=handle.lower()).single()
            if rec:
                return rec["canonical_id"]
        return None


# --- Resolver ----------------------------------------------------------------

@dataclass
class Resolver:
    overrides: OverrideIndex
    identity_lookup: IdentityLookup
    candidate_finder: CandidateFinder
    llm_arbiter: LLMArbiter | None = None

    def resolve(
        self,
        platform: str,
        handle: str,
        profile_blob: dict | None = None,
    ) -> ResolveResult:
        profile_blob = profile_blob or {}
        handle_norm = handle.strip().lstrip("@")

        # Tier 0: this exact (platform, handle) is already a known PlatformIdentity.
        # Without this short-circuit, re-running the resolver on the same input
        # could produce a different canonical_id when Tier 3 (LLM) is non-deterministic,
        # creating duplicate Persons and edges across runs.
        existing_self = self.identity_lookup.find_canonical_id(platform, handle_norm)
        if existing_self:
            return ResolveResult(
                canonical_id=existing_self, tier="known",
                confidence=1.0,
                reasoning=f"{platform}:{handle_norm} is already in the graph",
            )

        # Tier 1: override
        cid = self.overrides.lookup(platform, handle_norm)
        if cid:
            return ResolveResult(
                canonical_id=cid, tier="override", confidence=1.0,
                reasoning="exact match in identity_overrides.csv",
            )

        # Tier 2: bio-link extraction
        bio_text = " ".join(filter(None, [
            profile_blob.get("bio"),
            profile_blob.get("description"),
            profile_blob.get("url"),
            profile_blob.get("website"),
        ]))
        links = extract_platform_links(bio_text)
        # Skip self-references: a github bio containing its own URL is not useful.
        links = [(p, h) for p, h in links if not (p == platform and h == handle_norm.lower())]
        for link_platform, link_handle in links:
            existing = self.identity_lookup.find_canonical_id(link_platform, link_handle)
            if existing:
                return ResolveResult(
                    canonical_id=existing, tier="bio_link", confidence=0.95,
                    reasoning=(
                        f"bio links to {link_platform}:{link_handle} "
                        f"which is an existing Person"
                    ),
                )

        # Tier 3: candidate finder (gate) + LLM
        candidates = self.candidate_finder.find(platform, handle_norm, profile_blob)
        cand_ids = [c.canonical_id for c in candidates]

        if not candidates:
            return ResolveResult(
                canonical_id=platform_person_id(platform, handle_norm),
                tier="fresh", confidence=0.6,
                reasoning="no candidates; created fresh Person (LLM not invoked)",
            )

        if self.llm_arbiter is None:
            return ResolveResult(
                canonical_id=platform_person_id(platform, handle_norm),
                tier="fresh", confidence=0.6,
                reasoning=(
                    "candidates exist but no LLM arbiter configured; "
                    "created fresh Person"
                ),
                candidates_considered=cand_ids,
            )

        verdict = self.llm_arbiter.judge(platform, handle_norm, profile_blob, candidates)
        if (
            verdict.decision == "same"
            and verdict.confidence >= LLM_MERGE_CONFIDENCE_THRESHOLD
            and verdict.candidate_canonical_id in cand_ids
        ):
            return ResolveResult(
                canonical_id=verdict.candidate_canonical_id,
                tier="llm_match",
                confidence=verdict.confidence,
                reasoning=verdict.reasoning,
                candidates_considered=cand_ids,
                llm_verdict=verdict,
            )

        return ResolveResult(
            canonical_id=platform_person_id(platform, handle_norm),
            tier="fresh", confidence=0.6,
            reasoning=(
                f"LLM did not merge (decision={verdict.decision}, "
                f"confidence={verdict.confidence:.2f}); created fresh Person"
            ),
            candidates_considered=cand_ids,
            llm_verdict=verdict,
        )


# --- Module-level convenience -----------------------------------------------

_DEFAULT_OVERRIDE_PATH = (
    Path(__file__).resolve().parent.parent / "data" / "identity_overrides.csv"
)


def build_default_resolver(neo4j_session, *, with_llm: bool = True) -> Resolver:
    """Construct a Resolver wired against Neo4j and (optionally) Gemini.

    `with_llm=False` (or missing GEMINI_API_KEY) returns a Resolver that will
    create fresh Persons for every ambiguous case rather than risk a wrong merge.
    """
    overrides = OverrideIndex.from_csv(_DEFAULT_OVERRIDE_PATH)
    arbiter: LLMArbiter | None = None
    if with_llm:
        try:
            from identity.llm_arbiter import GeminiArbiter
            arbiter = GeminiArbiter()
        except RuntimeError:
            arbiter = None
    return Resolver(
        overrides=overrides,
        identity_lookup=Neo4jIdentityLookup(neo4j_session),
        candidate_finder=Neo4jCandidateFinder(neo4j_session),
        llm_arbiter=arbiter,
    )


def resolve_identity(
    platform: str,
    handle: str,
    profile_blob: dict | None = None,
    *,
    resolver: Resolver,
) -> ResolveResult:
    """Convenience wrapper. Pass a configured Resolver from build_default_resolver."""
    return resolver.resolve(platform, handle, profile_blob)
