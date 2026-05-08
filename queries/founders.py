"""Founder candidate reads — sourced from convergence_event."""
from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


_LIST_FOUNDER_CANDIDATES = text("""
    SELECT
        ce.target_person_id::text   AS id,
        p.display_name              AS name,
        ce.distinct_member_count    AS watcher_count,
        ce.score                    AS score,
        MAX(CASE WHEN pi.platform = 'github'   THEN pi.handle      END) AS github_handle,
        MAX(CASE WHEN pi.platform = 'github'   THEN pi.profile_url END) AS github_url,
        MAX(CASE WHEN pi.platform = 'linkedin' THEN pi.profile_url END) AS linkedin_url,
        MAX(CASE WHEN pi.platform = 'twitter'  THEN pi.handle      END) AS twitter_handle
    FROM convergence_event ce
    JOIN person p ON p.id = ce.target_person_id
    LEFT JOIN platform_identity pi ON pi.person_id = ce.target_person_id
    WHERE ce.org_id = :org_id
      AND ce.distinct_member_count >= :min_watchers
      AND COALESCE(p.entity_type, 'User') = 'User'
      AND NOT EXISTS (
          SELECT 1 FROM watchlist_member wx
          WHERE wx.person_id = ce.target_person_id
            AND wx.user_id   = :user_id
            AND wx.tier      IN ('active','vip')
      )
    GROUP BY ce.target_person_id, p.display_name, ce.distinct_member_count, ce.score
    ORDER BY ce.score DESC, ce.distinct_member_count DESC, p.display_name
    LIMIT :lim
""")


async def list_founder_candidates(session: AsyncSession, *, org_id: str, user_id: str,
                                   min_watchers: int, limit: int) -> list[dict]:
    result = await session.execute(_LIST_FOUNDER_CANDIDATES, {
        "org_id": org_id, "user_id": user_id,
        "min_watchers": min_watchers, "lim": limit,
    })
    return [dict(r) for r in result.mappings()]
