"""Fetch the list of users a given GitHub user follows.

GitHub does NOT expose follow timestamps via REST. The pipeline approximates
first_seen_at as the poll time of the first observation; subsequent observations
update last_seen_at only.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator, Optional

from scrapers.github_client import GitHubClient


@dataclass(frozen=True)
class FollowEntry:
    handle: str            # the followed user's login
    github_id: int
    profile_url: str
    avatar_url: Optional[str]
    type: str              # 'User' or 'Organization'


def _to_entry(item: dict) -> FollowEntry:
    return FollowEntry(
        handle=item["login"],
        github_id=int(item["id"]),
        profile_url=item["html_url"],
        avatar_url=item.get("avatar_url"),
        type=item.get("type") or "User",
    )


def fetch_following(client: GitHubClient, user_handle: str,
                    max_pages: Optional[int] = None) -> Iterator[FollowEntry]:
    """Yield FollowEntry objects for everyone `user_handle` follows."""
    path = f"users/{user_handle}/following"
    for page in client.paginate(path, max_pages=max_pages):
        for item in page:
            try:
                yield _to_entry(item)
            except (KeyError, TypeError):
                continue
