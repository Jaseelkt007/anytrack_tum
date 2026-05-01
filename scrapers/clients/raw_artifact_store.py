"""Persist raw scraping-provider responses before parsing.

Rationale (PHASE_2_PLAN.md M8 mitigations): if Scrapebadger changes its schema
or shuts down, having every raw response on disk lets us replay against an
updated parser without re-billing the API.

Files are stored as:
    data/raw_artifacts/{provider}/{kind}/{subject}-{captured_at}.json

Captured-at is ISO-8601 with colons replaced by '-' so the filename is portable.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path


_FILENAME_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_segment(value: str) -> str:
    return _FILENAME_SAFE.sub("_", value).strip("_") or "unknown"


class RawArtifactStore:
    """Filesystem-backed raw artifact store.

    Cheap and dumb on purpose — one JSON file per call. Indexing into Neo4j
    (RawArtifact pointer node) is intentionally deferred; do it from the
    file index when needed.
    """

    def __init__(self, root: Path | str = "data/raw_artifacts"):
        self._root = Path(root)

    def write(
        self,
        provider: str,
        kind: str,
        subject: str,
        payload: dict | list,
        *,
        captured_at: datetime | None = None,
    ) -> Path:
        captured_at = captured_at or datetime.now(timezone.utc)
        stamp = captured_at.isoformat().replace(":", "-")
        directory = self._root / _safe_segment(provider) / _safe_segment(kind)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{_safe_segment(subject)}-{stamp}.json"
        with path.open("w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        return path

    @property
    def root(self) -> Path:
        return self._root
