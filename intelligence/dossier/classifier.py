"""Gemini-backed dossier classifier (M9.5.2).

Takes an EnrichmentBundle, returns a structured Classification:

    {
      role:                "founder" | "investor" | "operator" | "unclear" | "not_relevant",
      confidence:          0..1,
      narrative:           2-4 sentence case, every sentence grounded in bundle evidence,
      key_signals:         [{ claim, supporting_url }],
      recommended_action:  "warm intro via X" | "monitor" | "ignore" | ...,
      cross_check_kb:      { is_known_investor, investor_type, agreement_with_kb },
    }

Hard rules (encoded in the system prompt):
  - Bias toward 'unclear' over a wrong 'founder' or 'investor'.
  - Every claim in `narrative` must be supported by something in the bundle.
  - If KB cross-match says is_known=True, the LLM must NOT contradict it.

The classifier is designed to be deterministic given the same bundle
(temperature 0.1). The dossier persistence layer (dossier.py) caches by
bundle hash so re-runs with unchanged data don't re-call Gemini.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from intelligence.dossier.enrichment import EnrichmentBundle

logger = logging.getLogger(__name__)


VALID_ROLES = ("founder", "investor", "operator", "unclear", "not_relevant")


@dataclass(frozen=True)
class Classification:
    role: str
    confidence: float
    narrative: str
    key_signals: list[dict[str, str]]   # [{claim, supporting_url}]
    recommended_action: str
    cross_check_kb: dict[str, Any]
    model: str = ""
    input_tokens: int = 0
    raw_response: str = ""               # for forensics; not persisted on Dossier


class LLMClassifier(Protocol):
    def classify(self, bundle: EnrichmentBundle) -> Classification: ...


SYSTEM_PROMPT = """You are an investment-signals analyst for an early-stage VC.

Given an evidence bundle about ONE person — gathered from public GitHub + Twitter
profiles, owned repositories, recent tweets, the VC's network of watchers who
have shown signal toward this person, and a knowledge-base lookup against a
known-investor reference set — classify them and write a short, evidence-grounded
case file.

ALLOWED ROLES (return EXACTLY one):
  - "founder"        : building or recently launched a company; technical depth
                       (e.g. high-star OSS project owned by them) is strong evidence
  - "investor"       : angel, VC, or fund partner. The KB cross-check is ground truth:
                       if kb_match.is_known is True, role MUST be "investor".
  - "operator"       : senior IC at a notable company; not currently building solo
  - "unclear"        : evidence insufficient to commit
  - "not_relevant"   : looks like noise (bot, spam, brand account, locked profile)

HARD RULES (violations cause this signal to be REJECTED downstream):
  1. Every sentence in `narrative` must be grounded in something in the bundle.
     If you can't ground a sentence, don't write it. Bias to fewer, true sentences.
  2. `key_signals` is a list of {claim, supporting_url} pairs. The supporting_url
     MUST be a URL that already appears in the bundle (e.g. a watcher's evidence_url,
     a repo's html_url, the github or twitter profile_url). DO NOT invent URLs.
  3. If kb_match.is_known is True in the bundle: role MUST be "investor", and the
     narrative should reference what KIND of investor (Angel / VC etc.) per
     kb_match.investor_type.
  4. Bias toward 'unclear' over a wrong 'founder' / 'investor'. Confidence < 0.7
     means you're not sure — return 'unclear' and explain what evidence is missing.
  5. `recommended_action` is a SHORT human-readable next step (max 12 words).
     Use proper capitalization and natural language — NEVER snake_case.
     Examples of GOOD output:
       - "Warm intro via Max Stoiber"
       - "Monitor for further engagement"
       - "Ignore — appears to be a brand account, not a person"
       - "Investigate further; signal is strong but role is unclear"
     Examples of BAD output (do not produce these):
       - "warm_intro_via_max_stoiber"
       - "monitor"
       - "warm_intro_via_Addy Osmani"

OUTPUT — return ONLY valid JSON, no prose, no markdown fences:
{
  "role": "founder" | "investor" | "operator" | "unclear" | "not_relevant",
  "confidence": <float 0..1>,
  "narrative": "<2-4 short sentences, each grounded in the bundle>",
  "key_signals": [
    {"claim": "<short>", "supporting_url": "<url from the bundle>"}
  ],
  "recommended_action": "<one of the allowed actions>",
  "cross_check_kb": {
    "is_known_investor": <bool>,
    "investor_type": "<value or null>",
    "agreement_with_kb": "<'agree' | 'disagree' | 'kb_silent'>"
  }
}"""


def build_user_prompt(bundle: EnrichmentBundle) -> str:
    """Compact bundle payload for the LLM. Stays under ~6KB for cost."""
    payload: dict[str, Any] = {
        "target": asdict(bundle.target_person),
        "kb_match": asdict(bundle.kb_match),
    }
    if bundle.github_profile:
        payload["github_profile"] = asdict(bundle.github_profile)
    if bundle.owned_repos:
        payload["owned_repos"] = [asdict(r) for r in bundle.owned_repos]
    if bundle.twitter_profile:
        payload["twitter_profile"] = asdict(bundle.twitter_profile)
    if bundle.recent_tweets:
        payload["recent_tweets"] = [
            {"text": t.text[:280], "url": t.url, "favorite_count": t.favorite_count,
             "retweet_count": t.retweet_count, "created_at": t.created_at}
            for t in bundle.recent_tweets[:8]
        ]
    if bundle.convergence_evidence:
        ce = bundle.convergence_evidence
        payload["convergence"] = {
            "distinct_member_count": ce.distinct_member_count,
            "score": ce.score,
            "window_start": ce.window_start,
            "window_end": ce.window_end,
            "signal_type_counts": ce.signal_type_counts,
            "evidence_rows": ce.evidence_rows[:10],  # cap
        }
    if bundle.cross_platform_followers:
        payload["cross_platform_followers"] = [
            asdict(f) for f in bundle.cross_platform_followers
        ]
    return "EVIDENCE BUNDLE:\n" + json.dumps(payload, ensure_ascii=False, indent=2, default=str)


def parse_classification(raw: str) -> Classification:
    """Tolerant parser: tries strict JSON first, falls back to fence-stripping."""
    text = (raw or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:].strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return Classification(
            role="unclear", confidence=0.0,
            narrative=f"unparseable LLM response: {raw[:200]}",
            key_signals=[], recommended_action="ignore",
            cross_check_kb={"is_known_investor": False,
                            "investor_type": None,
                            "agreement_with_kb": "kb_silent"},
            raw_response=raw,
        )

    role = str(data.get("role", "unclear")).lower()
    if role not in VALID_ROLES:
        role = "unclear"
    try:
        confidence = max(0.0, min(1.0, float(data.get("confidence", 0.0))))
    except (TypeError, ValueError):
        confidence = 0.0

    key_signals_raw = data.get("key_signals") or []
    key_signals: list[dict[str, str]] = []
    if isinstance(key_signals_raw, list):
        for ks in key_signals_raw:
            if isinstance(ks, dict):
                key_signals.append({
                    "claim": str(ks.get("claim", ""))[:300],
                    "supporting_url": str(ks.get("supporting_url", ""))[:500],
                })

    cck = data.get("cross_check_kb") or {}
    if not isinstance(cck, dict):
        cck = {}

    return Classification(
        role=role,
        confidence=confidence,
        narrative=str(data.get("narrative", ""))[:2000],
        key_signals=key_signals,
        recommended_action=str(data.get("recommended_action", "monitor"))[:80],
        cross_check_kb={
            "is_known_investor": bool(cck.get("is_known_investor", False)),
            "investor_type": cck.get("investor_type"),
            "agreement_with_kb": str(cck.get("agreement_with_kb", "kb_silent")),
        },
        raw_response=raw,
    )


def validate_grounding(bundle: EnrichmentBundle, classification: Classification) -> list[str]:
    """Return a list of grounding violations. Empty list = OK.

    Checks:
      - every supporting_url in key_signals appears somewhere in the bundle JSON
      - if kb_match.is_known is True, role is 'investor'
    """
    issues: list[str] = []
    bundle_json = bundle.to_json()
    for ks in classification.key_signals:
        url = ks.get("supporting_url", "")
        if url and url not in bundle_json:
            issues.append(f"ungrounded URL in key_signals: {url}")
    if bundle.kb_match.is_known and classification.role != "investor":
        issues.append(
            f"KB says known investor (type={bundle.kb_match.investor_type}) "
            f"but classifier returned role={classification.role}"
        )
    return issues


# --- Concrete Gemini-backed classifier ------------------------------------

class GeminiClassifier:
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
                "google-generativeai not installed. pip install google-generativeai"
            ) from e
        genai.configure(api_key=api_key)
        self._model_name = model
        self._model = genai.GenerativeModel(model, system_instruction=SYSTEM_PROMPT)
        self._log_path = log_path or (
            Path(__file__).resolve().parent.parent.parent / "data" / "dossier_classifications.jsonl"
        )
        self._log_path.parent.mkdir(parents=True, exist_ok=True)

    def classify(self, bundle: EnrichmentBundle) -> Classification:
        prompt = build_user_prompt(bundle)
        try:
            resp = self._model.generate_content(
                prompt,
                generation_config={
                    "response_mime_type": "application/json",
                    "temperature": 0.1,
                },
            )
            raw_text = getattr(resp, "text", "") or ""
            input_tokens = 0
            try:
                input_tokens = int(resp.usage_metadata.prompt_token_count)  # type: ignore[attr-defined]
            except Exception:
                pass
            classification = parse_classification(raw_text)
            classification = Classification(
                **{**asdict(classification),
                   "model": self._model_name,
                   "input_tokens": input_tokens,
                   "raw_response": raw_text}
            )
        except Exception as e:
            logger.warning("gemini classify failed: %s", e)
            classification = Classification(
                role="unclear", confidence=0.0,
                narrative=f"gemini error: {type(e).__name__}: {e}",
                key_signals=[], recommended_action="investigate_further",
                cross_check_kb={"is_known_investor": False,
                                "investor_type": None,
                                "agreement_with_kb": "kb_silent"},
                model=self._model_name,
            )
        self._log(bundle, classification)
        return classification

    def _log(self, bundle: EnrichmentBundle, classification: Classification) -> None:
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "target_id": bundle.target_person.canonical_id,
            "user_id": bundle.user_id,
            "model": classification.model,
            "input_tokens": classification.input_tokens,
            "role": classification.role,
            "confidence": classification.confidence,
            "narrative": classification.narrative,
        }
        with self._log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")


# --- Module-level convenience --------------------------------------------

def classify(bundle: EnrichmentBundle, *, llm: LLMClassifier | None = None) -> Classification:
    """Run classification with the supplied LLM (or default Gemini)."""
    if llm is None:
        llm = GeminiClassifier()
    return llm.classify(bundle)
