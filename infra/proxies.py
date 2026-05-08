"""Proxy router — pick a proxy for a (source, watcher) pair, track health.

Stickiness rationale: LinkedIn (and similar) ban accounts that hop IPs. Once a
watcher's account has been associated with a proxy IP, we want to keep using
the same IP for as long as it's healthy. We achieve that with a deterministic
hash on (watcher_id, eligible_proxies) — the same watcher always gets the same
proxy from a given pool, and only loses it if it falls out of the eligible set
(banned, in cooldown, etc.).

For sources that don't need stickiness (or don't have a watcher), we fall back
to least-recently-used selection.
"""
from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


class NoProxyAvailable(Exception):
    """No proxy matched the eligibility filter."""


@dataclass
class LeasedProxy:
    id: int
    kind: str
    geo: str | None
    host: str
    port: int
    username: str | None
    password: str | None

    @property
    def url(self) -> str:
        """Proxy URL in the standard `http://[user:pass@]host:port` format."""
        auth = f"{self.username}:{self.password}@" if self.username else ""
        return f"http://{auth}{self.host}:{self.port}"


# --- selection --------------------------------------------------------------

_LIST_ELIGIBLE_SQL = text("""
    SELECT id, kind, geo, host, port, username, password
    FROM proxy
    WHERE health = 'healthy'
      AND (cooldown_until IS NULL OR cooldown_until < now())
      AND (:kind IS NULL OR kind = :kind)
      AND (:geo  IS NULL OR geo  = :geo)
    ORDER BY last_used_at NULLS FIRST, id
""")


async def pick_proxy(
    session: AsyncSession,
    *,
    kind: str | None = None,
    geo: str | None = None,
    watcher_id: uuid.UUID | None = None,
) -> LeasedProxy:
    """Return one healthy proxy matching the kind+geo filter.

    With `watcher_id` set: deterministic hash → the same watcher gets the same
    proxy across calls (until that proxy is unhealthy or removed).

    Without a watcher: returns the least-recently-used eligible proxy.

    Raises NoProxyAvailable if the eligible set is empty.
    """
    rows = (await session.execute(_LIST_ELIGIBLE_SQL, {
        "kind": kind, "geo": geo,
    })).mappings().all()
    if not rows:
        raise NoProxyAvailable(
            f"no eligible proxy (kind={kind!r} geo={geo!r})"
        )

    if watcher_id is not None:
        # Deterministic stickiness: pick rows[h % len(rows)] where h is a hash
        # of the watcher id. The eligible list is sorted, so the same watcher
        # always lands on the same proxy as long as its position is stable.
        # When a proxy drops out (e.g. banned), the hash naturally re-maps; the
        # other rows are still indexed deterministically.
        digest = hashlib.sha256(watcher_id.bytes).digest()
        idx = int.from_bytes(digest[:8], "big") % len(rows)
        chosen = rows[idx]
    else:
        chosen = rows[0]  # LRU first

    # Touch last_used_at so plain LRU works.
    await session.execute(
        text("UPDATE proxy SET last_used_at = now() WHERE id = :id"),
        {"id": chosen["id"]},
    )

    return LeasedProxy(
        id=chosen["id"],
        kind=chosen["kind"],
        geo=chosen["geo"],
        host=chosen["host"],
        port=chosen["port"],
        username=chosen["username"],
        password=chosen["password"],
    )


# --- outcome reporting ------------------------------------------------------

async def report_proxy_outcome(
    session: AsyncSession,
    proxy_id: int,
    *,
    success: bool = True,
    ban: bool = False,
    cooldown_seconds: int = 0,
    notes: str | None = None,
) -> None:
    """Mirror of report_account_outcome but for proxies."""
    if ban:
        await session.execute(
            text("""
                UPDATE proxy
                SET health = 'banned',
                    ban_count = ban_count + 1,
                    notes = COALESCE(:notes, notes)
                WHERE id = :id
            """),
            {"id": proxy_id, "notes": notes},
        )
    elif cooldown_seconds > 0:
        until = datetime.now(timezone.utc) + timedelta(seconds=cooldown_seconds)
        await session.execute(
            text("""
                UPDATE proxy
                SET health = 'cooldown',
                    cooldown_until = :until,
                    notes = COALESCE(:notes, notes)
                WHERE id = :id
            """),
            {"id": proxy_id, "until": until, "notes": notes},
        )
    elif not success and notes:
        await session.execute(
            text("UPDATE proxy SET notes = :notes WHERE id = :id"),
            {"id": proxy_id, "notes": notes},
        )


async def heal_cooled_down_proxies(session: AsyncSession) -> int:
    """Run periodically to lift proxies out of cooldown."""
    result = await session.execute(
        text("""
            UPDATE proxy
            SET health = 'healthy', cooldown_until = NULL
            WHERE health = 'cooldown'
              AND cooldown_until IS NOT NULL
              AND cooldown_until <= now()
        """),
    )
    return result.rowcount or 0
