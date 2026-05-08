"""Account pool — checkout, exhaustion, cooldown, ban, sticky."""
from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import text


pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="needs DATABASE_URL",
)


_TEST_SOURCE = "test_acct_pool"


async def _wipe(session) -> None:
    await session.execute(
        text("DELETE FROM scraper_account WHERE source = :s"),
        {"s": _TEST_SOURCE},
    )
    await session.commit()


async def _seed(session, **kwargs) -> int:
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from db.models import ScraperAccount

    defaults = {
        "source": _TEST_SOURCE,
        "credentials": {"token": "fake"},
        "daily_quota": 5,
        "used_today": 0,
        "health": "healthy",
        "ban_count": 0,
        "org_id": "demo",
    }
    defaults.update(kwargs)
    stmt = pg_insert(ScraperAccount).values(**defaults).returning(ScraperAccount.id)
    row = (await session.execute(stmt)).first()
    await session.commit()
    return row[0]


@pytest.mark.asyncio
async def test_checkout_returns_healthy_account_and_increments_used_today():
    from db.engine import session_scope
    from infra.accounts import checkout_account

    async with session_scope() as s:
        await _wipe(s)
        await _seed(s, used_today=0, daily_quota=5)
        a = await checkout_account(s, source=_TEST_SOURCE)
        await s.commit()

    async with session_scope() as s:
        used = (await s.execute(text(
            "SELECT used_today FROM scraper_account WHERE id = :id"
        ), {"id": a.id})).scalar()
        assert used == 1
        await _wipe(s)


@pytest.mark.asyncio
async def test_exhausted_account_is_excluded():
    from db.engine import session_scope
    from infra.accounts import NoAccountAvailable, checkout_account

    async with session_scope() as s:
        await _wipe(s)
        await _seed(s, used_today=5, daily_quota=5)  # exhausted
        with pytest.raises(NoAccountAvailable):
            await checkout_account(s, source=_TEST_SOURCE)
        await _wipe(s)


@pytest.mark.asyncio
async def test_banned_account_is_excluded():
    from db.engine import session_scope
    from infra.accounts import NoAccountAvailable, checkout_account

    async with session_scope() as s:
        await _wipe(s)
        await _seed(s, health="banned")
        with pytest.raises(NoAccountAvailable):
            await checkout_account(s, source=_TEST_SOURCE)
        await _wipe(s)


@pytest.mark.asyncio
async def test_two_concurrent_checkouts_get_different_accounts():
    """SKIP LOCKED ensures two parallel callers each get a different row."""
    from db.engine import session_scope
    from infra.accounts import checkout_account

    async with session_scope() as s:
        await _wipe(s)
        a1 = await _seed(s)
        a2 = await _seed(s)

    # Two independent transactions — each must claim a distinct row.
    async with session_scope() as s1, session_scope() as s2:
        leased1 = await checkout_account(s1, source=_TEST_SOURCE)
        leased2 = await checkout_account(s2, source=_TEST_SOURCE)
        assert leased1.id != leased2.id
        assert {leased1.id, leased2.id} == {a1, a2}

    async with session_scope() as s:
        await _wipe(s)


@pytest.mark.asyncio
async def test_cooldown_then_heal_reactivates_account():
    from db.engine import session_scope
    from infra.accounts import (
        checkout_account,
        heal_cooled_down_accounts,
    )

    async with session_scope() as s:
        await _wipe(s)
        a = await _seed(s)
        # Force cooldown directly with an already-expired cooldown_until.
        await s.execute(text("""
            UPDATE scraper_account
            SET health = 'cooldown',
                cooldown_until = now() - interval '1 minute'
            WHERE id = :id
        """), {"id": a})
        await s.commit()

    async with session_scope() as s:
        healed = await heal_cooled_down_accounts(s)
        await s.commit()
        assert healed >= 1

    async with session_scope() as s:
        leased2 = await checkout_account(s, source=_TEST_SOURCE)
        assert leased2.id == a
        await _wipe(s)


@pytest.mark.asyncio
async def test_sticky_account_preferred_for_its_watcher():
    """When two accounts exist and one is sticky-pinned to watcher X, X gets it."""
    from db.engine import session_scope
    from infra.accounts import checkout_account

    pinned_watcher = uuid.UUID("11111111-1111-1111-1111-111111111111")
    other_watcher = uuid.UUID("22222222-2222-2222-2222-222222222222")

    async with session_scope() as s:
        await _wipe(s)
        # Seed a real Person row first so the FK is satisfiable
        await s.execute(text("""
            INSERT INTO person (id, org_id, display_name, entity_type)
            VALUES (:p, 'demo', 'pinned watcher', 'User')
            ON CONFLICT DO NOTHING
        """), {"p": pinned_watcher})
        await s.execute(text("""
            INSERT INTO person (id, org_id, display_name, entity_type)
            VALUES (:p, 'demo', 'other watcher', 'User')
            ON CONFLICT DO NOTHING
        """), {"p": other_watcher})
        await s.commit()
        free = await _seed(s)
        pinned = await _seed(s, sticky_watcher_id=pinned_watcher)

    async with session_scope() as s:
        a = await checkout_account(
            s, source=_TEST_SOURCE, watcher_id=pinned_watcher,
        )
        # The pinned account should win even though `free` has earlier last_used_at.
        assert a.id == pinned
        await _wipe(s)
        await s.execute(text("DELETE FROM person WHERE id IN (:p1, :p2)"),
                         {"p1": pinned_watcher, "p2": other_watcher})
        await s.commit()
