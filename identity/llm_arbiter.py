"""Gemini-backed LLM arbitration for ambiguous identity matches.

Only invoked by Resolver when:
  - Tier 1 (override CSV) missed
  - Tier 2 (bio-link extraction) missed
  - Candidate finder returned ≥ 1 plausible existing Person

Returns a structured verdict; Resolver only auto-merges when verdict.decision
is 'same' AND verdict.confidence >= LLM_MERGE_CONFIDENCE_THRESHOLD (0.85).

Every call is logged to data/identity_decisions.jsonl for audit.

Reads GEMINI_API_KEY from env. Never logs the key.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from identity.candidate_finder import CandidatePerson


@dataclass(frozen=True)
class LLMVerdict:
    decision: str                        # 'same' | 'different' | 'unknown'
    confidence: float                    # 0..1
    reasoning: str
    candidate_canonical_id: str | None = None


class LLMArbiter(Protocol):
    def judge(
        self,
        new_platform: str,
        new_handle: str,
        new_profile: dict,
        candidates: list[CandidatePerson],
    ) -> LLMVerdict: ...


SYSTEM_PROMPT = """You are an identity-resolution assistant for a venture-capital signal-tracking system.

Given a newly-observed social profile and a list of existing Persons in our database that
share some surface features (display name, similar handle, etc.), decide whether the new
profile is the SAME real-world Person as one of the candidates, a DIFFERENT person, or
whether the evidence is INSUFFICIENT.

Hard bias: prefer 'unknown' over a wrong 'same'. A wrong merge fuses two real people into
one node and corrupts the graph; a missed merge just leaves a duplicate that future evidence
can fix. When in doubt, return 'unknown'.

Return ONLY valid JSON in this exact shape, no prose, no fencing:
{
  "decision": "same" | "different" | "unknown",
  "confidence": <float in [0, 1]>,
  "candidate_canonical_id": "<id from one of the candidates>" | null,
  "reasoning": "<one or two short sentences>"
}

Set candidate_canonical_id ONLY when decision == "same"; otherwise null."""


def build_user_prompt(
    new_platform: str,
    new_handle: str,
    new_profile: dict,
    candidates: list[CandidatePerson],
) -> str:
    bio = (new_profile.get("bio") or new_profile.get("description") or "")[:400]
    name = new_profile.get("display_name") or new_profile.get("name") or ""
    location = new_profile.get("location") or ""
    url = new_profile.get("url") or new_profile.get("website") or ""

    new_block = (
        "NEW PROFILE\n"
        f"  platform: {new_platform}\n"
        f"  handle:   {new_handle}\n"
        f"  display:  {name}\n"
        f"  bio:      {bio}\n"
        f"  location: {location}\n"
        f"  url:      {url}\n"
    )

    cand_blocks: list[str] = []
    for i, c in enumerate(candidates, 1):
        ids_str = ", ".join(f"{p}:{h}" for p, h in c.identities) or "(none)"
        cand_blocks.append(
            f"CANDIDATE {i}\n"
            f"  canonical_id: {c.canonical_id}\n"
            f"  display:      {c.display_name}\n"
            f"  identities:   {ids_str}\n"
            f"  match_reason: {c.match_reason} (score={c.score:.2f})"
        )
    return new_block + "\n" + "\n\n".join(cand_blocks)


def parse_verdict(raw: str) -> LLMVerdict:
    """Parse Gemini's JSON output, tolerating fence-wrapping and noise."""
    text = (raw or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:].strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return LLMVerdict("unknown", 0.0, f"unparseable response: {raw[:200]}", None)

    decision = str(data.get("decision", "unknown")).lower()
    if decision not in ("same", "different", "unknown"):
        decision = "unknown"
    try:
        confidence = float(data.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))
    return LLMVerdict(
        decision=decision,
        confidence=confidence,
        reasoning=str(data.get("reasoning", ""))[:500],
        candidate_canonical_id=data.get("candidate_canonical_id"),
    )


class GeminiArbiter:
    """Default LLMArbiter using Gemini via the google-generativeai SDK."""

    def __init__(
        self,
        model: str = "gemini-2.5-flash",
        api_key: str | None = None,
        log_path: Path | None = None,
    ):
        api_key = api_key or os.environ.get("GEMINI_API_KEY")
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY not set in environment")
        try:
            import google.generativeai as genai  # type: ignore
        except ImportError as e:
            raise RuntimeError(
                "google-generativeai not installed. Add to requirements.txt and pip install."
            ) from e
        genai.configure(api_key=api_key)
        self._model = genai.GenerativeModel(model, system_instruction=SYSTEM_PROMPT)
        self._log_path = log_path or (
            Path(__file__).resolve().parent.parent / "data" / "identity_decisions.jsonl"
        )
        self._log_path.parent.mkdir(parents=True, exist_ok=True)

    def judge(
        self,
        new_platform: str,
        new_handle: str,
        new_profile: dict,
        candidates: list[CandidatePerson],
    ) -> LLMVerdict:
        if not candidates:
            return LLMVerdict("unknown", 0.0, "no candidates supplied", None)

        prompt = build_user_prompt(new_platform, new_handle, new_profile, candidates)
        try:
            resp = self._model.generate_content(
                prompt,
                generation_config={
                    "response_mime_type": "application/json",
                    "temperature": 0.1,
                },
            )
            verdict = parse_verdict(getattr(resp, "text", "") or "")
        except Exception as e:
            verdict = LLMVerdict(
                "unknown", 0.0,
                f"gemini error: {type(e).__name__}: {e}", None,
            )

        self._log(new_platform, new_handle, new_profile, candidates, verdict)
        return verdict

    def _log(
        self,
        platform: str,
        handle: str,
        profile: dict,
        candidates: list[CandidatePerson],
        verdict: LLMVerdict,
    ) -> None:
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "platform": platform,
            "handle": handle,
            "profile": {
                k: profile.get(k)
                for k in ("display_name", "name", "bio", "description",
                          "location", "url", "website")
            },
            "candidates": [asdict(c) for c in candidates],
            "verdict": asdict(verdict),
        }
        with self._log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
