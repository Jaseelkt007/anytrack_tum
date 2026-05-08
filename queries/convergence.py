"""Convergence-event read helpers (the data behind ConvergenceAlert API)."""
from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


_LIST_CONVERGENCE_SIGNALS = text("""
    SELECT
        ce.target_person_id::text                                       AS founder_id,
        p.display_name                                                  AS founder_name,
        MAX(CASE WHEN pi.platform = 'github' THEN pi.handle      END)   AS github_handle,
        MAX(CASE WHEN pi.platform = 'github' THEN pi.profile_url END)   AS github_url,
        ce.evidence                                                     AS evidence_json,
        ce.distinct_member_count                                        AS distinct_watchers,
        ce.score                                                        AS score,
        ce.score_breakdown                                              AS score_breakdown_json,
        ce.signal_type_counts                                           AS signal_type_counts_json,
        ce.first_signal_at                                              AS first_signal_at,
        ce.last_signal_at                                               AS last_signal_at,
        ce.window_start                                                 AS window_start,
        ce.window_end                                                   AS window_end,
        ce.fired_at                                                     AS fired_at
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
    GROUP BY ce.target_person_id, p.display_name, ce.evidence, ce.distinct_member_count,
             ce.score, ce.score_breakdown, ce.signal_type_counts,
             ce.first_signal_at, ce.last_signal_at, ce.window_start, ce.window_end,
             ce.fired_at
    ORDER BY ce.score DESC, p.display_name
    LIMIT :lim
""")


async def list_convergence_signals(session: AsyncSession, *, org_id: str, user_id: str,
                                    min_watchers: int, limit: int) -> list[dict]:
    result = await session.execute(_LIST_CONVERGENCE_SIGNALS, {
        "org_id": org_id, "user_id": user_id,
        "min_watchers": min_watchers, "lim": limit,
    })
    return [dict(r) for r in result.mappings()]
