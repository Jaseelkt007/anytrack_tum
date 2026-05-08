"""Source registry — name → Source instance.

Adding a new platform = add the import here. The worker only knows about
sources by name.
"""
from __future__ import annotations

from scrapers.base import Source
from scrapers.github_source import GitHubSource
from scrapers.twitter_source import TwitterSource

_REGISTRY: dict[str, Source] = {
    GitHubSource.name: GitHubSource(),
    TwitterSource.name: TwitterSource(),
}


def get_source(name: str) -> Source:
    try:
        return _REGISTRY[name]
    except KeyError as exc:
        raise KeyError(
            f"unknown source {name!r}; registered: {sorted(_REGISTRY)}"
        ) from exc


def known_sources() -> list[str]:
    return sorted(_REGISTRY)
