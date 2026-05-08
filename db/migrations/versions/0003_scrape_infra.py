"""scrape infra: proxy table + scraper_account expansion + crawl_lease

Revision ID: 0003
Revises: 0002
Create Date: 2026-05-08

Sub-project #4 schema. Lays down account pool, proxy router, and crawl
lease audit so sub-project #5 (LinkedIn) can plug in.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: Union[str, Sequence[str], None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- scraper_account expansion ----------------------------------------
    op.add_column("scraper_account",
                  sa.Column("geo", sa.Text(), nullable=True))
    op.add_column("scraper_account",
                  sa.Column("sticky_watcher_id",
                            postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column("scraper_account",
                  sa.Column("cooldown_until",
                            sa.DateTime(timezone=True), nullable=True))
    op.add_column("scraper_account",
                  sa.Column("notes", sa.Text(), nullable=True))
    op.create_foreign_key(
        "scraper_account_sticky_watcher_id_fkey",
        "scraper_account", "person",
        ["sticky_watcher_id"], ["id"],
    )
    op.create_index(
        "ix_scraper_account_source", "scraper_account", ["source"],
    )

    # --- proxy ------------------------------------------------------------
    op.create_table(
        "proxy",
        sa.Column("id", sa.BigInteger(), autoincrement=True, primary_key=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=True),
        sa.Column("geo", sa.Text(), nullable=True),
        sa.Column("host", sa.Text(), nullable=False),
        sa.Column("port", sa.Integer(), nullable=False),
        sa.Column("username", sa.Text(), nullable=True),
        sa.Column("password", sa.Text(), nullable=True),
        sa.Column("health", sa.Text(), server_default="healthy", nullable=False),
        sa.Column("ban_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("cooldown_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "kind IN ('residential','mobile','datacenter')", name="ck_proxy_kind",
        ),
        sa.CheckConstraint(
            "health IN ('healthy','cooldown','banned')", name="ck_proxy_health",
        ),
    )

    # --- crawl_lease ------------------------------------------------------
    op.create_table(
        "crawl_lease",
        sa.Column("id", sa.BigInteger(), autoincrement=True, primary_key=True),
        sa.Column("account_id", sa.BigInteger(), nullable=True),
        sa.Column("proxy_id", sa.BigInteger(), nullable=True),
        sa.Column("watcher_person_id",
                  postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("leased_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.Text(), server_default="held", nullable=False),
        sa.Column("outcome", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["account_id"], ["scraper_account.id"]),
        sa.ForeignKeyConstraint(["proxy_id"], ["proxy.id"]),
        sa.ForeignKeyConstraint(["watcher_person_id"], ["person.id"]),
        sa.CheckConstraint(
            "status IN ('held','released','expired','failed')",
            name="ck_lease_status",
        ),
    )
    op.create_index(
        "idx_crawl_lease_status_held", "crawl_lease", ["status", "leased_at"],
    )


def downgrade() -> None:
    op.drop_index("idx_crawl_lease_status_held", table_name="crawl_lease")
    op.drop_table("crawl_lease")
    op.drop_table("proxy")
    op.drop_index("ix_scraper_account_source", table_name="scraper_account")
    op.drop_constraint(
        "scraper_account_sticky_watcher_id_fkey",
        "scraper_account", type_="foreignkey",
    )
    op.drop_column("scraper_account", "notes")
    op.drop_column("scraper_account", "cooldown_until")
    op.drop_column("scraper_account", "sticky_watcher_id")
    op.drop_column("scraper_account", "geo")
