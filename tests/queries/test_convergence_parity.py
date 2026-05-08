"""Convergence parity test.

Inserts a tiny fixture into a dedicated test org and verifies the SQL CTE
produces the expected ConvergenceEvent shape.

Skipped if DATABASE_URL is not set.
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text


pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="needs DATABASE_URL to a running Postgres",
)


@pytest.mark.asyncio
async def test_convergence_returns_target_with_three_distinct_watchers():
    from db.engine import dispose_engine, session_scope
    from intelligence.convergence import find_convergences
    from intelligence.rule import AlertRule

    org = "test_parity"
    user = "test_parity"
    now = datetime.now(timezone.utc)
    window_start = now - timedelta(days=90)

    w_ids = [uuid.uuid4() for _ in range(4)]
    target_id = uuid.uuid4()

    async with session_scope() as s:
        await s.execute(text(
            "INSERT INTO org (id, name) VALUES (:id, :name) ON CONFLICT DO NOTHING"
        ), {"id": org, "name": "parity test"})
        await s.execute(text(
            "INSERT INTO app_user (id, org_id) VALUES (:id, :org) ON CONFLICT DO NOTHING"
        ), {"id": user, "org": org})

        # Clean any state from prior runs
        await s.execute(text("DELETE FROM convergence_event WHERE org_id = :org"), {"org": org})
        await s.execute(text("DELETE FROM edge_event WHERE org_id = :org"), {"org": org})
        await s.execute(text("DELETE FROM watchlist_member WHERE org_id = :org"), {"org": org})
        await s.execute(text("DELETE FROM person WHERE org_id = :org"), {"org": org})

        for i, w in enumerate(w_ids):
            await s.execute(text(
                "INSERT INTO person (id, org_id, display_name, entity_type) "
                "VALUES (:id, :org, :name, 'User')"
            ), {"id": w, "org": org, "name": f"Watcher {i}"})
            await s.execute(text(
                "INSERT INTO watchlist_member (org_id, user_id, person_id, tier) "
                "VALUES (:org, :user, :p, 'active')"
            ), {"org": org, "user": user, "p": w})

        await s.execute(text(
            "INSERT INTO person (id, org_id, display_name, entity_type) "
            "VALUES (:id, :org, 'Target', 'User')"
        ), {"id": target_id, "org": org})

        # 3 of 4 watchers follow target on github inside the window
        for w in w_ids[:3]:
            await s.execute(text(
                "INSERT INTO edge_event "
                "(org_id, source, action_type, watcher_person_id, target_kind, "
                " target_person_id, observed_at, first_seen_at, last_seen_at) "
                "VALUES (:org, 'github', 'follow', :w, 'person', :t, :obs, :obs, :obs)"
            ), {"org": org, "w": w, "t": target_id, "obs": now - timedelta(days=10)})

    async with session_scope() as s:
        rule = AlertRule()
        rule.window_days = 90
        rule.min_distinct_watchers = 2

        events = await find_convergences(
            s,
            org_id=org,
            user_id=user,
            as_of=now,
            rule=rule,
        )

    assert len(events) == 1, f"expected 1 event, got {len(events)}: {events}"
    ev = events[0]
    assert ev.target_id == str(target_id)
    assert ev.distinct_member_count == 3
    assert set(ev.member_ids) == {str(w) for w in w_ids[:3]}
    assert ev.signal_type_counts.get("FOLLOWS_ON_GITHUB") == 3

    await dispose_engine()
