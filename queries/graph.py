"""Graph snapshot reads for the Explore page (React Flow)."""
from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


_GRAPH_NODES = text("""
    SELECT
        p.id::text       AS id,
        p.display_name   AS name,
        p.investor_type  AS investor_type,
        wm.archetype     AS archetype,
        p.country        AS country,
        MAX(CASE WHEN pi.platform = 'github'   THEN pi.handle      END) AS github_handle,
        MAX(CASE WHEN pi.platform = 'twitter'  THEN pi.handle      END) AS twitter_handle,
        MAX(CASE WHEN pi.platform = 'linkedin' THEN pi.profile_url END) AS linkedin_url,
        'investor'       AS kind
    FROM watchlist_member wm
    JOIN person p ON p.id = wm.person_id
    LEFT JOIN platform_identity pi ON pi.person_id = p.id
    WHERE wm.user_id = :user_id
      AND wm.tier IN ('active','vip')
    GROUP BY p.id, p.display_name, p.investor_type, wm.archetype, p.country
""")


_GRAPH_EDGES = text("""
    WITH follows AS (
        SELECT
            ee.target_person_id   AS founder_id,
            ee.watcher_person_id  AS watcher_id,
            ee.observed_at        AS first_seen_at
        FROM edge_event ee
        JOIN watchlist_member w
          ON w.person_id = ee.watcher_person_id
         AND w.user_id   = :user_id
         AND w.tier      = 'active'
        JOIN person tp ON tp.id = ee.target_person_id
        WHERE ee.target_kind = 'person'
          AND ee.source      = 'github'
          AND ee.action_type = 'follow'
          AND ee.org_id      = :org_id
          AND COALESCE(tp.entity_type, 'User') = 'User'
          AND NOT EXISTS (
              SELECT 1 FROM watchlist_member wx
              WHERE wx.person_id = ee.target_person_id
                AND wx.user_id   = :user_id
                AND wx.tier      IN ('active','vip')
          )
    ),
    qualifying AS (
        SELECT founder_id
        FROM follows
        GROUP BY founder_id
        HAVING COUNT(DISTINCT watcher_id) >= :min_watchers
    )
    SELECT
        f.founder_id::text   AS founder_id,
        tp.display_name      AS founder_name,
        MAX(CASE WHEN pi.platform = 'github' THEN pi.handle END) AS founder_github,
        f.watcher_id::text   AS watcher_id,
        wp.display_name      AS watcher_name,
        f.first_seen_at      AS first_seen_at
    FROM follows f
    JOIN qualifying q  ON q.founder_id  = f.founder_id
    JOIN person tp     ON tp.id         = f.founder_id
    JOIN person wp     ON wp.id         = f.watcher_id
    LEFT JOIN platform_identity pi ON pi.person_id = f.founder_id
    GROUP BY f.founder_id, tp.display_name, f.watcher_id, wp.display_name, f.first_seen_at
    ORDER BY tp.display_name, wp.display_name
    LIMIT :edge_limit
""")


async def list_graph_nodes(session: AsyncSession, *, user_id: str) -> list[dict]:
    result = await session.execute(_GRAPH_NODES, {"user_id": user_id})
    return [dict(r) for r in result.mappings()]


async def list_graph_edges(session: AsyncSession, *, org_id: str, user_id: str,
                            min_watchers: int, edge_limit: int) -> list[dict]:
    result = await session.execute(_GRAPH_EDGES, {
        "org_id": org_id, "user_id": user_id,
        "min_watchers": min_watchers, "edge_limit": edge_limit,
    })
    return [dict(r) for r in result.mappings()]
