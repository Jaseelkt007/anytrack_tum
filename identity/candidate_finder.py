"""Find existing Persons that might be the same as a newly-observed (platform, handle).

Cheap signals only — no LLM here. If 0 candidates are returned, the resolver
short-circuits and creates a fresh Person without ever calling the LLM. This
is the primary cost-control gate.

Signals:
  - Display-name fuzzy match (normalized, accent-stripped, token overlap)
  - Handle string similarity (levenshtein + substring containment)
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class CandidatePerson:
    canonical_id: str
    display_name: str
    identities: list[tuple[str, str]]   # [(platform, handle), ...]
    match_reason: str
    score: float                        # 0..1, cheap heuristic


def normalize_name(name: str | None) -> str:
    if not name:
        return ""
    nfkd = unicodedata.normalize("NFKD", name)
    no_accents = "".join(c for c in nfkd if not unicodedata.combining(c))
    return " ".join(no_accents.lower().split())


def _levenshtein(a: str, b: str) -> int:
    if len(a) < len(b):
        a, b = b, a
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        curr = [i]
        for j, cb in enumerate(b, 1):
            cost = 0 if ca == cb else 1
            curr.append(min(prev[j] + 1, curr[j - 1] + 1, prev[j - 1] + cost))
        prev = curr
    return prev[-1]


def handle_similarity(a: str, b: str) -> float:
    a, b = a.lower(), b.lower()
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if a in b or b in a:
        return 0.85
    return 1.0 - (_levenshtein(a, b) / max(len(a), len(b)))


def score_candidate(
    new_name_norm: str,
    new_handle: str,
    cand_name_norm: str,
    cand_identities: list[tuple[str, str]],
) -> tuple[float, str]:
    """Combined name + handle score. Returns (score, human-readable reason)."""
    name_score = 0.0
    if new_name_norm and cand_name_norm:
        if new_name_norm == cand_name_norm:
            name_score = 1.0
        elif new_name_norm in cand_name_norm or cand_name_norm in new_name_norm:
            name_score = 0.7
        else:
            t1, t2 = set(new_name_norm.split()), set(cand_name_norm.split())
            if t1 and t2:
                overlap = len(t1 & t2) / max(len(t1), len(t2))
                if overlap >= 0.5:
                    name_score = 0.6 * overlap

    handle_score = 0.0
    handle_reason = ""
    for cand_platform, cand_handle in cand_identities:
        sim = handle_similarity(new_handle, cand_handle)
        if sim > handle_score:
            handle_score = sim
            handle_reason = (
                f"handle similar to existing {cand_platform}:{cand_handle} "
                f"(sim={sim:.2f})"
            )

    score = max(name_score, handle_score)
    if name_score >= 0.7 and handle_score >= 0.7:
        score = min(1.0, name_score + 0.2)

    if name_score >= handle_score and name_score > 0:
        reason = f"display_name overlap (norm score={name_score:.2f})"
    elif handle_reason:
        reason = handle_reason
    else:
        reason = f"weak match (name={name_score:.2f}, handle={handle_score:.2f})"
    return score, reason


# --- Neo4j-backed implementation -------------------------------------------

class CandidateFinder(Protocol):
    def find(self, platform: str, handle: str, profile_blob: dict) -> list[CandidatePerson]: ...


_CANDIDATE_QUERY = """
MATCH (p:Person)
WHERE
    toLower(p.display_name) CONTAINS $name_token
    OR EXISTS {
        MATCH (p)-[:HAS_IDENTITY]->(i:PlatformIdentity)
        WHERE toLower(i.handle) CONTAINS $handle_token
           OR $handle_token CONTAINS toLower(i.handle)
    }
WITH p
OPTIONAL MATCH (p)-[:HAS_IDENTITY]->(i:PlatformIdentity)
WITH p, collect({platform: i.platform, handle: i.handle}) AS ids
RETURN p.canonical_id AS canonical_id,
       p.display_name AS display_name,
       ids
LIMIT 25
"""


class Neo4jCandidateFinder:
    """Default candidate finder. Pulls plausible Persons from Neo4j by cheap
    text-match (substring on normalized display_name + handle similarity)."""

    MIN_SCORE = 0.5
    MAX_RETURN = 5

    def __init__(self, session):
        self._session = session

    def find(self, platform: str, handle: str, profile_blob: dict) -> list[CandidatePerson]:
        display_name = (
            profile_blob.get("display_name")
            or profile_blob.get("name")
            or ""
        ).strip()
        norm_input = normalize_name(display_name)
        name_token = norm_input.split(" ")[0] if norm_input else ""
        handle_token = handle.lower()

        if not name_token and not handle_token:
            return []

        records = self._session.run(
            _CANDIDATE_QUERY,
            name_token=name_token or "__never_matches__",
            handle_token=handle_token,
        ).data()

        out: list[CandidatePerson] = []
        for row in records:
            ids = [
                (d["platform"], d["handle"])
                for d in row.get("ids", [])
                if d and d.get("platform") and d.get("handle")
            ]
            cand_norm = normalize_name(row["display_name"])
            score, reason = score_candidate(norm_input, handle, cand_norm, ids)
            if score >= self.MIN_SCORE:
                out.append(CandidatePerson(
                    canonical_id=row["canonical_id"],
                    display_name=row["display_name"],
                    identities=ids,
                    match_reason=reason,
                    score=score,
                ))

        out.sort(key=lambda c: c.score, reverse=True)
        return out[:self.MAX_RETURN]
