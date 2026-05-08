"""Person detail reads."""
from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


_PERSON_DETAIL = text("""
    SELECT
        p.id::text                                                      AS id,
        p.display_name                                                  AS name,
        p.investor_type                                                 AS investor_type,
        p.role_tags                                                     AS role_tags,
        p.country                                                       AS country,
        wm.tier                                                         AS watch_tier,
        wm.archetype                                                    AS archetype,
        MAX(CASE WHEN pi.platform = 'linkedin' THEN pi.profile_url END) AS linkedin_url,
        MAX(CASE WHEN pi.platform = 'twitter'  THEN pi.handle      END) AS twitter_handle,
        MAX(CASE WHEN pi.platform = 'github'   THEN pi.handle      END) AS github_handle,
        MAX(CASE WHEN pi.platform = 'github'   THEN pi.profile_url END) AS github_url
    FROM person p
    LEFT JOIN watchlist_member wm
      ON wm.person_id = p.id AND wm.user_id = :user_id
    LEFT JOIN platform_identity pi ON pi.person_id = p.id
    WHERE p.id::text = :id
    GROUP BY p.id, p.display_name, p.investor_type, p.role_tags, p.country,
             wm.tier, wm.archetype
""")


async def get_person(session: AsyncSession, *, person_id: str, user_id: str) -> dict | None:
    result = await session.execute(_PERSON_DETAIL, {"id": person_id, "user_id": user_id})
    row = result.mappings().first()
    return dict(row) if row else None
