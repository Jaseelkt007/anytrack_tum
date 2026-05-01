"""Thin GitHub REST wrapper for Phase 1.

Responsibilities:
  - Bearer auth (multiple tokens supported; rotates on rate-limit exhaustion).
  - Pagination via the Link header, surfaced as a streaming generator.
  - Rate-limit awareness: backs off when Remaining is low; sleeps until Reset
    on 403 secondary rate limit.

Not in scope (yet):
  - GraphQL.
  - Conditional requests (ETag-based caching). Would be a Phase 2 cost optimization.

Usage:
    client = GitHubClient(tokens=[os.environ['GITHUB_TOKEN']])
    for page in client.paginate('users/AntonOsika/starred',
                                accept='application/vnd.github.star+json'):
        for entry in page:
            ...
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Iterator, Optional

logger = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"
DEFAULT_PER_PAGE = 100
MIN_REMAINING_BEFORE_ROTATE = 50  # rotate token (or pause) when below this


@dataclass
class _TokenState:
    token: str
    remaining: int = 5000
    reset_at: float = 0.0          # epoch seconds
    exhausted_until: float = 0.0   # set on secondary rate limits


class GitHubRateLimitError(RuntimeError):
    pass


class GitHubAPIError(RuntimeError):
    pass


@dataclass
class GitHubClient:
    """Minimal GitHub REST client with token rotation and backoff."""

    tokens: list[str]
    user_agent: str = "tum-ai-phase1-scraper/0.1"
    timeout: float = 30.0
    _states: list[_TokenState] = field(init=False, default_factory=list)
    _idx: int = field(init=False, default=0)

    def __post_init__(self) -> None:
        if not self.tokens:
            raise ValueError("at least one GitHub token required")
        self._states = [_TokenState(token=t) for t in self.tokens]

    # --- low-level request loop ----------------------------------------------

    def _pick_token(self) -> _TokenState:
        """Round-robin among tokens, skipping exhausted ones if any have headroom."""
        now = time.time()
        # Prefer a non-exhausted token with high remaining.
        candidates = sorted(
            (s for s in self._states if s.exhausted_until <= now),
            key=lambda s: -s.remaining,
        )
        if candidates and candidates[0].remaining >= MIN_REMAINING_BEFORE_ROTATE:
            return candidates[0]
        if candidates:
            return candidates[0]
        # All exhausted — pick the one whose exhaustion expires soonest and sleep.
        nearest = min(self._states, key=lambda s: s.exhausted_until)
        wait = max(1.0, nearest.exhausted_until - now)
        logger.warning("All GitHub tokens exhausted; sleeping %.0fs", wait)
        time.sleep(wait)
        return nearest

    def _update_state_from_headers(self, state: _TokenState, headers: dict) -> None:
        rem = headers.get("x-ratelimit-remaining") or headers.get("X-RateLimit-Remaining")
        rst = headers.get("x-ratelimit-reset") or headers.get("X-RateLimit-Reset")
        if rem is not None:
            try:
                state.remaining = int(rem)
            except ValueError:
                pass
        if rst is not None:
            try:
                state.reset_at = float(rst)
            except ValueError:
                pass

    def _request(self, path: str, *, accept: Optional[str] = None,
                 params: Optional[dict] = None,
                 max_retries: int = 5) -> tuple[object, dict]:
        """Single GET, returning (json_body, headers). Retries on transient errors."""
        try:
            import requests
        except ImportError as exc:
            raise GitHubAPIError(f"requests not installed: {exc}") from exc

        url = path if path.startswith("http") else f"{GITHUB_API}/{path.lstrip('/')}"

        for attempt in range(1, max_retries + 1):
            state = self._pick_token()
            headers = {
                "Authorization": f"Bearer {state.token}",
                "Accept": accept or "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": self.user_agent,
            }
            try:
                resp = requests.get(url, headers=headers, params=params, timeout=self.timeout)
            except requests.RequestException as exc:
                logger.warning("GitHub request failed (attempt %d): %s", attempt, exc)
                time.sleep(min(2 ** attempt, 30))
                continue

            self._update_state_from_headers(state, resp.headers)

            if resp.status_code == 200:
                return resp.json(), dict(resp.headers)

            if resp.status_code == 404:
                # Caller may want to handle (e.g., user not found). Return None.
                return None, dict(resp.headers)

            if resp.status_code in (403, 429):
                # Could be rate limit (primary or secondary).
                retry_after = resp.headers.get("retry-after") or resp.headers.get("Retry-After")
                if retry_after:
                    wait = float(retry_after)
                else:
                    # Fall back to reset header
                    reset = resp.headers.get("x-ratelimit-reset")
                    wait = max(5.0, (float(reset) - time.time())) if reset else 30.0
                state.exhausted_until = time.time() + wait
                logger.warning("GitHub %s on %s; backing off %.0fs", resp.status_code, url, wait)
                time.sleep(min(wait, 60))
                continue

            if 500 <= resp.status_code < 600:
                wait = min(2 ** attempt, 30)
                logger.warning("GitHub %s; retry in %ds", resp.status_code, wait)
                time.sleep(wait)
                continue

            raise GitHubAPIError(f"GET {url} -> {resp.status_code}: {resp.text[:200]}")

        raise GitHubAPIError(f"max retries exhausted for {url}")

    # --- public API -----------------------------------------------------------

    def get(self, path: str, *, accept: Optional[str] = None,
            params: Optional[dict] = None) -> object:
        """Single GET. Returns parsed JSON or None on 404."""
        body, _ = self._request(path, accept=accept, params=params)
        return body

    def paginate(self, path: str, *, accept: Optional[str] = None,
                 params: Optional[dict] = None,
                 per_page: int = DEFAULT_PER_PAGE,
                 max_pages: Optional[int] = None) -> Iterator[list]:
        """Yield each page (list of items) by following the Link rel=\"next\" header."""
        params = dict(params or {})
        params.setdefault("per_page", per_page)
        url = path
        page_num = 0
        while True:
            body, headers = self._request(url, accept=accept, params=params if page_num == 0 else None)
            if body is None:
                return
            if not isinstance(body, list):
                # Some endpoints return objects with the payload inside (e.g., search).
                # Phase 1 scope only paginates list endpoints; surface anything else.
                raise GitHubAPIError(f"expected list response from {url}, got {type(body).__name__}")
            yield body
            page_num += 1
            if max_pages is not None and page_num >= max_pages:
                return
            link = headers.get("link") or headers.get("Link")
            next_url = _parse_next_link(link) if link else None
            if not next_url:
                return
            url = next_url


_LINK_NEXT_RE = re.compile(r'<([^>]+)>;\s*rel="next"')


def _parse_next_link(link_header: str) -> Optional[str]:
    """Extract the rel=next URL from a Link header. Returns None if absent."""
    m = _LINK_NEXT_RE.search(link_header)
    return m.group(1) if m else None
