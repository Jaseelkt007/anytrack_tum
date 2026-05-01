"""Scrapebadger HTTP client implementing TwitterFollowingClient.

API:
  GET https://scrapebadger.com/v1/twitter/users/{username}/by_username
  GET https://scrapebadger.com/v1/twitter/users/{username}/followings[?cursor=...]

Auth: header `x-api-key: <SCRAPEBADGER_API_KEY>`. Read from env. Never logged.

Behavior:
  - Backoff with exponential delay on 429 / 5xx.
  - Every successful response payload is handed to a RawArtifactStore (if
    provided) before parsing — see PHASE_2_PLAN.md M8 mitigation #2.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

import requests

from scrapers.clients.raw_artifact_store import RawArtifactStore
from scrapers.clients.twitter_following_client import (
    FollowingsPage,
    LatestTweetsPage,
    TweetRecord,
    TwitterUserRecord,
)

logger = logging.getLogger(__name__)

_BASE_URL = "https://scrapebadger.com/v1/twitter"
_DEFAULT_TIMEOUT_S = 20
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
_MAX_RETRIES = 3


class ScrapebadgerError(RuntimeError):
    pass


class ScrapebadgerClient:
    def __init__(
        self,
        api_key: str | None = None,
        *,
        artifact_store: RawArtifactStore | None = None,
        session: requests.Session | None = None,
        timeout_s: float = _DEFAULT_TIMEOUT_S,
    ):
        api_key = api_key or os.environ.get("SCRAPEBADGER_API_KEY")
        if not api_key:
            raise ScrapebadgerError("SCRAPEBADGER_API_KEY not set in environment")
        self._api_key = api_key
        self._session = session or requests.Session()
        self._timeout = timeout_s
        self._artifacts = artifact_store

    # --- HTTP --------------------------------------------------------------

    def _get(self, path: str, params: dict | None = None) -> dict[str, Any]:
        url = f"{_BASE_URL}{path}"
        headers = {"x-api-key": self._api_key}
        attempt = 0
        while True:
            attempt += 1
            try:
                resp = self._session.get(
                    url, headers=headers, params=params, timeout=self._timeout
                )
            except requests.RequestException as e:
                if attempt > _MAX_RETRIES:
                    raise ScrapebadgerError(
                        f"network error after {attempt} attempts: {e}"
                    ) from e
                self._sleep(attempt)
                continue

            if resp.status_code == 200:
                try:
                    return resp.json()
                except ValueError as e:
                    raise ScrapebadgerError(
                        f"non-JSON response from {path}: {resp.text[:200]}"
                    ) from e

            if resp.status_code in _RETRYABLE_STATUS and attempt <= _MAX_RETRIES:
                logger.warning(
                    "scrapebadger %s -> %d (attempt %d/%d), backing off",
                    path, resp.status_code, attempt, _MAX_RETRIES,
                )
                self._sleep(attempt)
                continue

            raise ScrapebadgerError(
                f"scrapebadger {path} -> HTTP {resp.status_code}: {resp.text[:200]}"
            )

    @staticmethod
    def _sleep(attempt: int) -> None:
        time.sleep(min(2 ** attempt, 30))

    # --- Public API --------------------------------------------------------

    def lookup_user(self, username: str) -> TwitterUserRecord:
        username = _normalize_username(username)
        payload = self._get(f"/users/{username}/by_username")
        if self._artifacts:
            self._artifacts.write("scrapebadger", "by_username", username, payload)
        data = payload.get("data") or payload
        return _to_record(data)

    def list_followings(
        self,
        username: str,
        cursor: str | None = None,
    ) -> FollowingsPage:
        username = _normalize_username(username)
        params = {"cursor": cursor} if cursor else None
        payload = self._get(f"/users/{username}/followings", params=params)
        if self._artifacts:
            cursor_tag = (cursor or "p1")[:16]
            self._artifacts.write(
                "scrapebadger", "followings",
                f"{username}-{cursor_tag}", payload,
            )
        users_raw = payload.get("data") or []
        return FollowingsPage(
            users=[_to_record(u) for u in users_raw],
            next_cursor=payload.get("next_cursor") or None,
        )

    def list_latest_tweets(
        self,
        username: str,
        cursor: str | None = None,
    ) -> LatestTweetsPage:
        """Fetch the most recent tweets for a user. Endpoint:
        GET /v1/twitter/users/{username}/latest_tweets[?cursor=...]

        Used by the M9.5 dossier enrichment layer. Field shape is preserved in
        `TweetRecord.raw` since the exact provider schema may evolve.
        """
        username = _normalize_username(username)
        params = {"cursor": cursor} if cursor else None
        payload = self._get(f"/users/{username}/latest_tweets", params=params)
        if self._artifacts:
            cursor_tag = (cursor or "p1")[:16]
            self._artifacts.write(
                "scrapebadger", "latest_tweets",
                f"{username}-{cursor_tag}", payload,
            )
        tweets_raw = payload.get("data") or []
        return LatestTweetsPage(
            tweets=[_to_tweet(t) for t in tweets_raw],
            next_cursor=payload.get("next_cursor") or None,
        )


# --- Parsers ---------------------------------------------------------------

def _to_record(data: dict[str, Any]) -> TwitterUserRecord:
    return TwitterUserRecord(
        id=str(data.get("id") or ""),
        username=str(data.get("username") or ""),
        name=str(data.get("name") or ""),
        verified=bool(data.get("verified") or False),
        followers_count=int(data.get("followers_count") or 0),
        following_count=int(data.get("following_count") or 0),
    )


def _to_tweet(data: dict[str, Any]) -> TweetRecord:
    """Tolerant tweet parser. Pulls a known core set of fields and stashes the
    full payload in `raw` so downstream code can pick up additional fields
    without a parser change."""
    def _ifield(*keys: str) -> int:
        for k in keys:
            if k in data:
                try:
                    return int(data[k] or 0)
                except (TypeError, ValueError):
                    return 0
        return 0
    text = data.get("full_text") or data.get("text") or ""
    return TweetRecord(
        id=str(data.get("id") or data.get("rest_id") or data.get("tweet_id") or ""),
        text=str(text),
        created_at=str(data.get("created_at") or ""),
        favorite_count=_ifield("favorite_count", "like_count", "favoriteCount"),
        retweet_count=_ifield("retweet_count", "retweetCount"),
        reply_count=_ifield("reply_count", "replyCount"),
        quote_count=_ifield("quote_count", "quoteCount"),
        view_count=_ifield("view_count", "views", "viewCount", "impression_count"),
        raw=dict(data),
    )


def _normalize_username(username: str) -> str:
    """Strip @ and any URL prefix the caller might have passed in."""
    u = username.strip().lstrip("@")
    for prefix in ("https://", "http://", "www."):
        if u.startswith(prefix):
            u = u[len(prefix):]
    for host in ("twitter.com/", "x.com/", "mobile.twitter.com/"):
        if u.startswith(host):
            u = u[len(host):]
    return u.split("/", 1)[0].split("?", 1)[0]
