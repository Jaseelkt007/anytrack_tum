"""Scraper account pool — checkout, report outcome, periodic quota reset.

Concurrent workers ask for an account; the pool hands out one healthy + within-quota
account at a time using `SELECT FOR UPDATE SKIP LOCKED`. The same query bumps
last_used_at and used_today inside the same transaction so two workers can never
double-claim the same row.

Lifecycle:
    account = await checkout_account(session, source="github")
    try:
        # do work using account.credentials
        await report_account_outcome(session, account.id, success=True)
    except BannedException:
        await report_account_outcome(session, account.id, ban=True)
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


class NoAccountAvailable(Exception):
    """Raised when no eligible account is in the pool."""


@dataclass
class LeasedAccount:
    """Snapshot of a checked-out account passed to the worker."""
    id: int
    source: str
    credentials: dict[str, Any]
    daily_quota: int | None
    used_today: int
    geo: str | None
    sticky_watcher_id: uuid.UUID | None


# --- checkout ---------------------------------------------------------------

_CHECKOUT_SQL = text("""
    WITH chosen AS (
        SELECT id
        FROM scraper_account
        WHERE source = :source
          AND health = 'healthy'
          AND (cooldown_until IS NULL OR cooldown_until < now())
          AND (daily_quota IS NULL OR used_today < daily_quota)
          AND (
                CAST(:match_sticky AS boolean) = false
                OR sticky_watcher_id IS NULL
                OR sticky_watcher_id = :watcher_id
          )
        ORDER BY
            -- Sticky-pinned accounts win, then least-recently-used.
            (sticky_watcher_id IS NOT NULL AND sticky_watcher_id = :watcher_id) DESC,
            last_used_at NULLS FIRST
        LIMIT 1
        FOR UPDATE SKIP LOCKED
    )
    UPDATE scraper_account
    SET used_today = used_today + 1,
        last_used_at = now()
    WHERE id IN (SELECT id FROM chosen)
    RETURNING id, source, credentials, daily_quota, used_today, geo, sticky_watcher_id
""")


async def checkout_account(
    session: AsyncSession,
    *,
    source: str,
    watcher_id: uuid.UUID | None = None,
    require_sticky: bool = False,
) -> LeasedAccount:
    """Atomically lease one healthy in-quota account for `source`.

    `watcher_id` + `require_sticky` controls stickiness:
      - require_sticky=False (default): prefers an account already pinned to
        this watcher, falls back to any unpinned-and-healthy account.
      - require_sticky=True: only returns an account whose sticky_watcher_id
        is NULL or equal to watcher_id (never crosses watchers).

    Raises NoAccountAvailable if nothing matches.
    """
    result = await session.execute(_CHECKOUT_SQL, {
        "source": source,
        "watcher_id": watcher_id,
        "match_sticky": require_sticky,
    })
    row = result.mappings().first()
    if not row:
        raise NoAccountAvailable(
            f"no eligible account for source={source!r} watcher={watcher_id}"
        )
    creds = row["credentials"]
    if isinstance(creds, str):
        import json
        creds = json.loads(creds)
    return LeasedAccount(
        id=row["id"],
        source=row["source"],
        credentials=creds or {},
        daily_quota=row["daily_quota"],
        used_today=row["used_today"],
        geo=row["geo"],
        sticky_watcher_id=row["sticky_watcher_id"],
    )


# --- outcome reporting ------------------------------------------------------

async def report_account_outcome(
    session: AsyncSession,
    account_id: int,
    *,
    success: bool = True,
    ban: bool = False,
    cooldown_seconds: int = 0,
    notes: str | None = None,
) -> None:
    """Update health/cooldown after a crawl attempt.

      success=True              → no change beyond the increment in checkout
      ban=True                  → health='banned', ban_count+=1
      cooldown_seconds > 0      → health='cooldown', cooldown_until=now+secs
    """
    if ban:
        await session.execute(
            text("""
                UPDATE scraper_account
                SET health = 'banned',
                    ban_count = ban_count + 1,
                    notes = COALESCE(:notes, notes)
                WHERE id = :id
            """),
            {"id": account_id, "notes": notes},
        )
    elif cooldown_seconds > 0:
        until = datetime.now(timezone.utc) + timedelta(seconds=cooldown_seconds)
        await session.execute(
            text("""
                UPDATE scraper_account
                SET health = 'cooldown',
                    cooldown_until = :until,
                    notes = COALESCE(:notes, notes)
                WHERE id = :id
            """),
            {"id": account_id, "until": until, "notes": notes},
        )
    elif not success and notes:
        await session.execute(
            text("UPDATE scraper_account SET notes = :notes WHERE id = :id"),
            {"id": account_id, "notes": notes},
        )


# --- periodic helpers -------------------------------------------------------

async def reset_daily_quotas(session: AsyncSession) -> int:
    """Zero used_today on every account. Run from a daily cron task.

    Returns the row count touched (for observability).
    """
    result = await session.execute(
        text("""
            UPDATE scraper_account
            SET used_today = 0
            WHERE used_today > 0
        """),
    )
    return result.rowcount or 0


async def heal_cooled_down_accounts(session: AsyncSession) -> int:
    """Move 'cooldown' accounts back to 'healthy' once their cooldown window
    expires. Run periodically (e.g. every 5 minutes)."""
    result = await session.execute(
        text("""
            UPDATE scraper_account
            SET health = 'healthy', cooldown_until = NULL
            WHERE health = 'cooldown'
              AND cooldown_until IS NOT NULL
              AND cooldown_until <= now()
        """),
    )
    return result.rowcount or 0
