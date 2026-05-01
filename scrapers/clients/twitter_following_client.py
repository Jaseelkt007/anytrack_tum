"""Provider-agnostic interface for Twitter follow-list ingest.

Concrete implementations:
  - scrapers.clients.scrapebadger.ScrapebadgerClient (default for M8)
  - (future) twscrape adapter, X API adapter

Phase 2 wording rule (per CLAUDE_SCRAPEBADGER_IMPLEMENTATION_BRIEF.md):
  Never claim exact follow timestamps. The contract is "list_followings returns
  the current page-N view." All temporal semantics are observation-based.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator, Protocol


@dataclass(frozen=True)
class TwitterUserRecord:
    id: str
    username: str
    name: str = ""
    verified: bool = False
    followers_count: int = 0
    following_count: int = 0


@dataclass(frozen=True)
class FollowingsPage:
    """One page of a user's followings list."""
    users: list[TwitterUserRecord]
    next_cursor: str | None


@dataclass(frozen=True)
class TweetRecord:
    """A single tweet. Fields beyond the core ones are kept in `raw` for forensics
    since Scrapebadger's exact response shape may evolve."""
    id: str
    text: str = ""
    created_at: str = ""
    favorite_count: int = 0
    retweet_count: int = 0
    reply_count: int = 0
    quote_count: int = 0
    view_count: int = 0
    raw: dict = None  # type: ignore[assignment]


@dataclass(frozen=True)
class LatestTweetsPage:
    tweets: list[TweetRecord]
    next_cursor: str | None


class TwitterFollowingClient(Protocol):
    def lookup_user(self, username: str) -> TwitterUserRecord: ...

    def list_followings(
        self,
        username: str,
        cursor: str | None = None,
    ) -> FollowingsPage: ...


def iter_followings(
    client: TwitterFollowingClient,
    username: str,
    max_pages: int = 1,
) -> Iterator[TwitterUserRecord]:
    """Iterate up to max_pages of followings, transparently paginating via cursor.

    Pagination invariant: if you baseline at depth N, diff at depth N. Mixing depths
    creates false 'newly observed' signals — the brief's #1 known limitation.
    """
    if max_pages <= 0:
        return
    cursor: str | None = None
    pages_fetched = 0
    while pages_fetched < max_pages:
        page = client.list_followings(username, cursor=cursor)
        for user in page.users:
            yield user
        pages_fetched += 1
        if not page.next_cursor:
            return
        cursor = page.next_cursor
