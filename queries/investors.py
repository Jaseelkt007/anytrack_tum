"""Read helpers for active/vip watchlist members."""
from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


_LIST_INVESTORS = text("""
    SELECT
        p.id::text                                                        AS id,
        p.display_name                                                    AS name,
        p.investor_type                                                   AS investor_type,
        wm.archetype                                                      AS archetype,
        p.country                                                         AS country,
        MAX(CASE WHEN pi.platform = 'linkedin' THEN pi.profile_url END)   AS linkedin_url,
        MAX(CASE WHEN pi.platform = 'twitter'  THEN pi.handle      END)   AS twitter_handle,
        MAX(CASE WHEN pi.platform = 'github'   THEN pi.handle      END)   AS github_handle
    FROM watchlist_member wm
    JOIN person p ON p.id = wm.person_id
    LEFT JOIN platform_identity pi ON pi.person_id = p.id
    WHERE wm.user_id = :user_id
      AND wm.tier IN ('active','vip')
    GROUP BY p.id, p.display_name, p.investor_type, wm.archetype, p.country
    ORDER BY p.display_name
""")


async def list_investors(session: AsyncSession, *, user_id: str) -> list[dict]:
    result = await session.execute(_LIST_INVESTORS, {"user_id": user_id})
    return [dict(row) for row in result.mappings()]
