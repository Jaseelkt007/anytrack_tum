"""Fetch a user's starred repos with timestamps.

Uses Accept: application/vnd.github.star+json which returns each entry as
{ "starred_at": "<iso>", "repo": { ...repo metadata... } }.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator, Optional

from scrapers.github_client import GitHubClient

STAR_ACCEPT = "application/vnd.github.star+json"


@dataclass(frozen=True)
class StarEvent:
    starred_at: str         # ISO 8601 string from GitHub
    repo_full_name: str     # 'owner/name'
    repo_owner: str
    repo_name: str
    repo_description: Optional[str]
    repo_language: Optional[str]
    repo_star_count: int
    repo_html_url: str
    repo_github_id: int


def _to_event(entry: dict) -> StarEvent:
    repo = entry["repo"]
    full_name: str = repo["full_name"]
    owner_handle = full_name.split("/", 1)[0]
    return StarEvent(
        starred_at=entry["starred_at"],
        repo_full_name=full_name,
        repo_owner=owner_handle,
        repo_name=repo["name"],
        repo_description=repo.get("description"),
        repo_language=repo.get("language"),
        repo_star_count=int(repo.get("stargazers_count", 0)),
        repo_html_url=repo["html_url"],
        repo_github_id=int(repo["id"]),
    )


def fetch_starred(client: GitHubClient, user_handle: str,
                  max_pages: Optional[int] = None) -> Iterator[StarEvent]:
    """Yield StarEvents for a user, ordered by starred_at descending (newest first)."""
    path = f"users/{user_handle}/starred"
    for page in client.paginate(path, accept=STAR_ACCEPT, max_pages=max_pages):
        for entry in page:
            # Defensive: malformed entries are skipped, not crashed on.
            try:
                yield _to_event(entry)
            except (KeyError, TypeError):
                continue
