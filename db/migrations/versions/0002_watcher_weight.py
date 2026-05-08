"""watcher weight + follow-stats view

Revision ID: 0002
Revises: 825bb97ca967
Create Date: 2026-05-08

Adds:
  - watchlist_member.weight (nullable; NULL = derive from archetype)
  - watcher_follow_stats view: per-watcher counts of observed follows by
    source, used by intelligence.scoring for base-rate calibration.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: Union[str, Sequence[str], None] = "825bb97ca967"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "watchlist_member",
        sa.Column("weight", sa.Float(), nullable=True),
    )

    # Per-watcher count of distinct outbound follow targets per source.
    # Used as the denominator in base-rate calibration:
    #   surprise = log(1 + total_population / max(1, watcher_follow_count_for_source))
    op.execute("""
        CREATE OR REPLACE VIEW watcher_follow_stats AS
        SELECT
            ee.org_id              AS org_id,
            ee.watcher_person_id   AS watcher_id,
            ee.source              AS source,
            COUNT(DISTINCT
                  COALESCE(ee.target_person_id::text, ee.target_repo_id)
            )                      AS distinct_outbound_targets
        FROM edge_event ee
        WHERE ee.action_type IN ('follow','star')
        GROUP BY ee.org_id, ee.watcher_person_id, ee.source
    """)


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS watcher_follow_stats")
    op.drop_column("watchlist_member", "weight")
