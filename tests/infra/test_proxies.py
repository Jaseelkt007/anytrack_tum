"""Proxy router — health filter, geo filter, watcher stickiness."""
from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import text


pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="needs DATABASE_URL",
)


async def _wipe(session) -> None:
    await session.execute(text("DELETE FROM proxy WHERE provider = '__test__'"))
    await session.commit()


async def _seed(session, **kwargs) -> int:
    defaults = {
        "kind": "residential",
        "provider": "__test__",
        "host": "127.0.0.1",
        "port": 8080,
        "health": "healthy",
    }
    defaults.update(kwargs)
    cols = ", ".join(defaults.keys())
    placeholders = ", ".join(f":{k}" for k in defaults.keys())
    row = (await session.execute(
        text(f"INSERT INTO proxy ({cols}) VALUES ({placeholders}) RETURNING id"),
        defaults,
    )).first()
    await session.commit()
    return row[0]


@pytest.mark.asyncio
async def test_pick_proxy_returns_healthy_only():
    from db.engine import session_scope
    from infra.proxies import NoProxyAvailable, pick_proxy

    async with session_scope() as s:
        await _wipe(s)
        await _seed(s, host="10.0.0.1", health="banned")
        await _seed(s, host="10.0.0.2", health="cooldown")
        with pytest.raises(NoProxyAvailable):
            await pick_proxy(s)
        await _wipe(s)


@pytest.mark.asyncio
async def test_pick_proxy_geo_filter():
    from db.engine import session_scope
    from infra.proxies import pick_proxy

    async with session_scope() as s:
        await _wipe(s)
        await _seed(s, host="10.0.0.10", geo="US")
        await _seed(s, host="10.0.0.11", geo="DE")
        de = await pick_proxy(s, geo="DE")
        assert de.host == "10.0.0.11"
        await _wipe(s)


@pytest.mark.asyncio
async def test_pick_proxy_sticky_for_watcher():
    """Same watcher gets the same proxy across calls (deterministic hash)."""
    from db.engine import session_scope
    from infra.proxies import pick_proxy

    w = uuid.UUID("33333333-3333-3333-3333-333333333333")

    async with session_scope() as s:
        await _wipe(s)
        for i in range(5):
            await _seed(s, host=f"10.1.0.{i}")

    async with session_scope() as s1:
        first = await pick_proxy(s1, watcher_id=w)
    async with session_scope() as s2:
        second = await pick_proxy(s2, watcher_id=w)

    assert first.id == second.id

    async with session_scope() as s:
        await _wipe(s)
