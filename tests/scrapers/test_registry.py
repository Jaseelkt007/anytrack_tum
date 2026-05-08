"""Source registry — name → Source instance contracts."""
from __future__ import annotations

import pytest

from scrapers.base import Source
from scrapers.registry import get_source, known_sources


def test_known_sources_includes_github_and_twitter():
    names = known_sources()
    assert "github" in names
    assert "twitter" in names


def test_get_source_returns_source_instance():
    gh = get_source("github")
    assert isinstance(gh, Source)
    assert gh.name == "github"


def test_get_source_unknown_raises_keyerror():
    with pytest.raises(KeyError):
        get_source("linkedin")  # arrives in sub-project #5


def test_twitter_source_raises_until_subproject_5():
    import asyncio
    import uuid

    from scrapers.registry import get_source
    from scrapers.types import WatcherInfo

    src = get_source("twitter")
    watcher = WatcherInfo(
        canonical_id=uuid.uuid4(), display_name="x", handle="y",
    )

    async def _call():
        await src.crawl_watcher(None, watcher=watcher, org_id="demo")

    with pytest.raises(NotImplementedError):
        asyncio.run(_call())
