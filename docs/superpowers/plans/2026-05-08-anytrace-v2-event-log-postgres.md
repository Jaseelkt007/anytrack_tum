# Sub-project #1 — Event Log + Postgres Foundation: Implementation Plan

**Spec:** [`docs/superpowers/specs/2026-05-08-anytrace-v2-event-log-postgres-design.md`](../specs/2026-05-08-anytrace-v2-event-log-postgres-design.md)

**Goal:** Replace Neo4j with Postgres + pgvector as the single source of truth, introduce `edge_event` as the immutable signal log, drop the Neo4j Docker service / driver / Cypher modules.

**Architecture:** Greenfield Postgres on Neon (no ETL from Neo4j — data already deleted). SQLAlchemy 2.0 async + asyncpg + Alembic. UUIDv5 for canonical `person.id` (preserves the existing deterministic-ID logic). pgvector enabled from day 1 even though unused until sub-project #6.

**Tech Stack:** Postgres 16 (Neon), SQLAlchemy 2.0 async, asyncpg, Alembic, pgvector, FastAPI (existing), pytest (existing).

**Estimate:** ~2 weeks for one engineer.

---

## File Structure

### Created

```
db/
  __init__.py
  engine.py                    # async engine + session factory + dependency for FastAPI
  models.py                    # all SQLAlchemy 2.0 models
  migrations/
    env.py                     # Alembic env (reads DATABASE_URL from env)
    script.py.mako             # Alembic template
    versions/
      0001_initial.py          # all schema in one baseline migration
alembic.ini                    # Alembic config
queries/
  __init__.py
  investors.py                 # active/vip watchlist reads
  founders.py                  # founder candidate reads
  convergence.py               # convergence reads + writes
  alerts.py                    # alert_rule reads + writes
  dossier.py                   # dossier_classification reads + writes
  identity.py                  # identity_decision reads + writes
  feedback.py                  # feedback_event writes
scripts/
  bootstrap_demo_data.py       # CSV → Postgres (replaces the load_*_to_neo4j.py scripts)
tests/
  db/
    test_migration.py          # alembic up/down round-trip
    test_bootstrap.py          # bootstrap idempotency
  queries/
    test_convergence_parity.py # SQL CTE produces expected events for a fixture set
docker-compose.yml             # if not present, create; replaces any Neo4j-using compose
```

### Modified

```
backend/app.py                 # SQLAlchemy session lifecycle replaces Neo4j driver
backend/queries.py             # delete; redirect imports to queries/*.py
backend/mappers.py             # mostly unchanged (works on dicts) — verify call sites
intelligence/convergence.py    # Cypher → SQL CTE; ConvergenceEvent dataclass unchanged
intelligence/notifier.py       # alert_rule reads from Postgres
intelligence/dossier/dossier.py        # dossier_classification reads/writes from Postgres
intelligence/dossier/feedback.py       # feedback_event writes from Postgres
identity/resolver.py           # identity_decision reads/writes from Postgres
identity/llm_arbiter.py        # decision persistence to Postgres
scrapers/pipeline.py           # MERGE Cypher → INSERT/UPSERT against edge_event + person + repository
scrapers/jobs/fetch_following.py       # writes via queries/* helpers
scrapers/jobs/fetch_starred_repos.py   # writes via queries/* helpers
scrapers/jobs/fetch_twitter_followings.py  # writes via queries/* helpers
scrapers/jobs/load_twitter_signals_to_neo4j.py  # rename to load_twitter_signals.py; Postgres
requirements.txt               # add sqlalchemy[asyncio] asyncpg alembic pgvector; remove neo4j
.env.example                   # remove NEO4J_*; add DATABASE_URL
```

### Deleted

```
scrapers/cypher.py             # all Cypher templates — gone
backend/queries.py             # after queries/* takes over
data/identity_decisions.jsonl  # archive once migrated
data/identity_overrides.csv    # archive once migrated
data/dossier_classifications.jsonl  # archive once migrated
data/alert_rules.json          # archive once migrated
```

---

## Task 1 — Provision Neon, install deps, configure env

**Files:** `requirements.txt`, `.env.example`, `.env` (local only).

1. **User action (manual):** create a Neon project at console.neon.tech. Get the pooled and direct connection strings for the `main` branch and a `dev` branch.

2. **Update `requirements.txt`:** remove `neo4j==5.28.1`; add:
   ```
   sqlalchemy[asyncio]==2.0.36
   asyncpg==0.30.0
   alembic==1.14.0
   pgvector==0.3.6
   ```
   Keep all other entries unchanged.

3. **Update `.env.example`:** remove the `NEO4J_*` block; add at the top:
   ```
   # Postgres (Neon)
   # Pooled URL for the app; direct URL for migrations.
   DATABASE_URL=postgresql+asyncpg://USER:PASSWORD@HOST/DB?ssl=require
   DATABASE_URL_DIRECT=postgresql+psycopg://USER:PASSWORD@HOST/DB?ssl=require
   ```
   (asyncpg for the app, psycopg for Alembic — Alembic doesn't run async cleanly.)

4. **Update local `.env`:** populate both URLs from Neon.

5. **Verify install:**
   ```
   pip install -r requirements.txt
   python -c "import sqlalchemy, asyncpg, alembic, pgvector; print('ok')"
   ```
   Expected: `ok`.

6. **Commit:**
   ```
   git add requirements.txt .env.example
   git commit -m "deps: replace neo4j driver with sqlalchemy + asyncpg + alembic + pgvector"
   ```

---

## Task 2 — SQLAlchemy engine + session factory

**Files:** create `db/__init__.py`, `db/engine.py`.

1. **Create `db/__init__.py`:** empty file.

2. **Create `db/engine.py`:**
   ```python
   """Async SQLAlchemy engine + session factory.

   The app uses the pooled URL (DATABASE_URL). Migrations use the direct URL.
   """
   from __future__ import annotations

   import os
   from collections.abc import AsyncIterator
   from contextlib import asynccontextmanager

   from sqlalchemy.ext.asyncio import (
       AsyncSession,
       async_sessionmaker,
       create_async_engine,
   )

   _engine = None
   _Session: async_sessionmaker[AsyncSession] | None = None


   def get_engine():
       global _engine, _Session
       if _engine is None:
           url = os.environ["DATABASE_URL"]
           _engine = create_async_engine(url, pool_pre_ping=True)
           _Session = async_sessionmaker(_engine, expire_on_commit=False)
       return _engine


   def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
       get_engine()
       assert _Session is not None
       return _Session


   @asynccontextmanager
   async def session_scope() -> AsyncIterator[AsyncSession]:
       """Use in scripts/CLIs. FastAPI uses Depends(get_session) instead."""
       Session = get_sessionmaker()
       async with Session() as session:
           try:
               yield session
               await session.commit()
           except Exception:
               await session.rollback()
               raise


   async def get_session() -> AsyncIterator[AsyncSession]:
       """FastAPI dependency."""
       Session = get_sessionmaker()
       async with Session() as session:
           yield session


   async def dispose_engine() -> None:
       global _engine, _Session
       if _engine is not None:
           await _engine.dispose()
           _engine = None
           _Session = None
   ```

3. **Smoke test:**
   ```
   python -c "
   import asyncio, os
   from dotenv import load_dotenv; load_dotenv()
   from sqlalchemy import text
   from db.engine import session_scope, dispose_engine
   async def main():
       async with session_scope() as s:
           r = await s.execute(text('SELECT 1'))
           print(r.scalar())
       await dispose_engine()
   asyncio.run(main())
   "
   ```
   Expected: `1`.

4. **Commit:**
   ```
   git add db/__init__.py db/engine.py
   git commit -m "db: async sqlalchemy engine + session factory"
   ```

---

## Task 3 — Define all SQLAlchemy models

**Files:** create `db/models.py`. (One file, all tables — they're declarative and read together better than split.)

1. **Create `db/models.py`** with the full set from spec §4. Key choices:
   - `DeclarativeBase` subclass `Base`.
   - UUID columns use `sqlalchemy.dialects.postgresql.UUID(as_uuid=True)`.
   - `JSONB` from `sqlalchemy.dialects.postgresql`.
   - Array columns use `ARRAY(String)`.

   ```python
   """SQLAlchemy 2.0 models for AnyTrace v2.

   Schema mirrors docs/superpowers/specs/2026-05-08-anytrace-v2-event-log-postgres-design.md §4.
   """
   from __future__ import annotations

   import uuid
   from datetime import datetime
   from typing import Any

   from sqlalchemy import (
       ARRAY,
       BigInteger,
       CheckConstraint,
       DateTime,
       ForeignKey,
       Index,
       Integer,
       PrimaryKeyConstraint,
       String,
       Text,
       UniqueConstraint,
       func,
   )
   from sqlalchemy.dialects.postgresql import JSONB, UUID
   from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


   class Base(DeclarativeBase):
       pass


   # --- multi-tenancy ----------------------------------------------------------

   class Org(Base):
       __tablename__ = "org"
       id: Mapped[str] = mapped_column(Text, primary_key=True)
       name: Mapped[str] = mapped_column(Text, nullable=False)
       created_at: Mapped[datetime] = mapped_column(
           DateTime(timezone=True), nullable=False, server_default=func.now()
       )


   class AppUser(Base):
       __tablename__ = "app_user"
       id: Mapped[str] = mapped_column(Text, primary_key=True)
       org_id: Mapped[str] = mapped_column(Text, ForeignKey("org.id"), nullable=False, index=True)
       email: Mapped[str | None] = mapped_column(Text)
       created_at: Mapped[datetime] = mapped_column(
           DateTime(timezone=True), nullable=False, server_default=func.now()
       )


   # --- canonical entities -----------------------------------------------------

   class Person(Base):
       __tablename__ = "person"
       id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
       org_id: Mapped[str] = mapped_column(Text, ForeignKey("org.id"), nullable=False, index=True)
       display_name: Mapped[str] = mapped_column(Text, nullable=False)
       investor_type: Mapped[str | None] = mapped_column(Text)
       country: Mapped[str | None] = mapped_column(Text)
       sector_tags: Mapped[list[str] | None] = mapped_column(ARRAY(String))
       stage_tags: Mapped[list[str] | None] = mapped_column(ARRAY(String))
       role_tags: Mapped[list[str] | None] = mapped_column(ARRAY(String))
       confidence_score: Mapped[float | None] = mapped_column()
       entity_type: Mapped[str | None] = mapped_column(Text, index=True)
       first_observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
       last_observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
       created_at: Mapped[datetime] = mapped_column(
           DateTime(timezone=True), nullable=False, server_default=func.now()
       )

       identities: Mapped[list[PlatformIdentity]] = relationship(back_populates="person", cascade="all, delete-orphan")


   class PlatformIdentity(Base):
       __tablename__ = "platform_identity"
       id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
       person_id: Mapped[uuid.UUID] = mapped_column(
           UUID(as_uuid=True), ForeignKey("person.id", ondelete="CASCADE"), nullable=False, index=True
       )
       platform: Mapped[str] = mapped_column(Text, nullable=False)
       handle: Mapped[str] = mapped_column(Text, nullable=False)
       handle_original: Mapped[str | None] = mapped_column(Text)
       profile_url: Mapped[str | None] = mapped_column(Text)
       verified_via: Mapped[str | None] = mapped_column(Text)
       confidence: Mapped[float | None] = mapped_column()
       kind: Mapped[str | None] = mapped_column(Text)
       first_observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
       last_observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

       person: Mapped[Person] = relationship(back_populates="identities")

       __table_args__ = (
           UniqueConstraint("platform", "handle", name="uq_pi_platform_handle"),
           CheckConstraint("platform IN ('github','twitter','linkedin')", name="ck_pi_platform"),
       )


   # --- watchlist --------------------------------------------------------------

   class WatchlistMember(Base):
       __tablename__ = "watchlist_member"
       org_id: Mapped[str] = mapped_column(Text, ForeignKey("org.id"), nullable=False)
       user_id: Mapped[str] = mapped_column(Text, ForeignKey("app_user.id"), nullable=False)
       person_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("person.id"), nullable=False)
       tier: Mapped[str] = mapped_column(Text, nullable=False)
       archetype: Mapped[str | None] = mapped_column(Text)
       rationale: Mapped[str | None] = mapped_column(Text)
       added_at: Mapped[datetime] = mapped_column(
           DateTime(timezone=True), nullable=False, server_default=func.now()
       )

       __table_args__ = (
           PrimaryKeyConstraint("org_id", "user_id", "person_id"),
           CheckConstraint("tier IN ('active','vip','reference')", name="ck_wm_tier"),
           Index("idx_wm_user_tier", "user_id", "tier"),
       )


   # --- repositories -----------------------------------------------------------

   class Repository(Base):
       __tablename__ = "repository"
       github_id: Mapped[str] = mapped_column(Text, primary_key=True)
       owner_handle: Mapped[str] = mapped_column(Text, nullable=False)
       name: Mapped[str] = mapped_column(Text, nullable=False)
       full_name: Mapped[str] = mapped_column(Text, nullable=False, index=True)
       description: Mapped[str | None] = mapped_column(Text)
       language: Mapped[str | None] = mapped_column(Text)
       star_count_observed: Mapped[int | None] = mapped_column(Integer)
       html_url: Mapped[str | None] = mapped_column(Text)
       last_fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
       first_observed_at: Mapped[datetime] = mapped_column(
           DateTime(timezone=True), nullable=False, server_default=func.now()
       )


   class RepositoryOwner(Base):
       __tablename__ = "repository_owner"
       repo_id: Mapped[str] = mapped_column(Text, ForeignKey("repository.github_id"), nullable=False)
       owner_person_id: Mapped[uuid.UUID] = mapped_column(
           UUID(as_uuid=True), ForeignKey("person.id"), nullable=False
       )
       __table_args__ = (PrimaryKeyConstraint("repo_id", "owner_person_id"),)


   # --- the event log ----------------------------------------------------------

   class EdgeEvent(Base):
       __tablename__ = "edge_event"
       id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
       org_id: Mapped[str] = mapped_column(Text, ForeignKey("org.id"), nullable=False)
       source: Mapped[str] = mapped_column(Text, nullable=False)
       action_type: Mapped[str] = mapped_column(Text, nullable=False)
       watcher_person_id: Mapped[uuid.UUID] = mapped_column(
           UUID(as_uuid=True), ForeignKey("person.id"), nullable=False
       )
       target_kind: Mapped[str] = mapped_column(Text, nullable=False)
       target_person_id: Mapped[uuid.UUID | None] = mapped_column(
           UUID(as_uuid=True), ForeignKey("person.id")
       )
       target_repo_id: Mapped[str | None] = mapped_column(Text, ForeignKey("repository.github_id"))
       observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
       first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
       last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
       removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
       evidence_url: Mapped[str | None] = mapped_column(Text)
       edge_confidence: Mapped[float | None] = mapped_column()
       raw_artifact_ref: Mapped[str | None] = mapped_column(Text)
       metadata_: Mapped[dict[str, Any] | None] = mapped_column("metadata", JSONB)

       __table_args__ = (
           CheckConstraint("source IN ('github','twitter','linkedin')", name="ck_ee_source"),
           CheckConstraint("target_kind IN ('person','repository')", name="ck_ee_target_kind"),
           CheckConstraint(
               "(target_kind = 'person'     AND target_person_id IS NOT NULL AND target_repo_id IS NULL) OR "
               "(target_kind = 'repository' AND target_repo_id   IS NOT NULL AND target_person_id IS NULL)",
               name="ck_ee_target_oneof",
           ),
           UniqueConstraint(
               "source", "action_type", "watcher_person_id",
               # COALESCE in a unique constraint requires an expression index, not a column constraint.
               # We enforce it via a CREATE UNIQUE INDEX in the migration; this Python-level constraint
               # is intentionally narrower (we leave dedup to the migration's expression index).
               name="uq_ee_observation_partial",
           ),
           Index(
               "idx_ee_target_person_observed",
               "target_person_id", "observed_at",
               postgresql_where="target_kind = 'person'",
           ),
           Index(
               "idx_ee_target_repo_observed",
               "target_repo_id", "observed_at",
               postgresql_where="target_kind = 'repository'",
           ),
           Index("idx_ee_watcher_observed", "watcher_person_id", "observed_at"),
           Index("idx_ee_org_source_observed", "org_id", "source", "observed_at"),
       )


   # --- convergence ------------------------------------------------------------

   class ConvergenceEventRow(Base):
       __tablename__ = "convergence_event"
       id: Mapped[str] = mapped_column(Text, primary_key=True)
       org_id: Mapped[str] = mapped_column(Text, ForeignKey("org.id"), nullable=False)
       target_person_id: Mapped[uuid.UUID] = mapped_column(
           UUID(as_uuid=True), ForeignKey("person.id"), nullable=False, index=True
       )
       fired_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
       window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
       window_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
       distinct_member_count: Mapped[int] = mapped_column(Integer, nullable=False)
       member_person_ids: Mapped[list[uuid.UUID]] = mapped_column(ARRAY(UUID(as_uuid=True)), nullable=False)
       score: Mapped[float] = mapped_column(nullable=False)
       score_breakdown: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
       first_signal_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
       last_signal_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
       signal_type_counts: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
       evidence: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB)

       __table_args__ = (Index("idx_ce_org_score", "org_id", "score"),)


   # --- ported-from-files tables -----------------------------------------------

   class AlertRule(Base):
       __tablename__ = "alert_rule"
       org_id: Mapped[str] = mapped_column(Text, ForeignKey("org.id"), nullable=False)
       user_id: Mapped[str] = mapped_column(Text, ForeignKey("app_user.id"), nullable=False)
       payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
       updated_at: Mapped[datetime] = mapped_column(
           DateTime(timezone=True), nullable=False, server_default=func.now()
       )
       __table_args__ = (PrimaryKeyConstraint("org_id", "user_id"),)


   class DossierClassification(Base):
       __tablename__ = "dossier_classification"
       convergence_event_id: Mapped[str] = mapped_column(
           Text, ForeignKey("convergence_event.id"), primary_key=True
       )
       classification: Mapped[str] = mapped_column(Text, nullable=False)
       rationale: Mapped[str | None] = mapped_column(Text)
       classified_at: Mapped[datetime] = mapped_column(
           DateTime(timezone=True), nullable=False, server_default=func.now()
       )


   class IdentityDecision(Base):
       __tablename__ = "identity_decision"
       id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
       person_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("person.id"), nullable=False)
       decision_type: Mapped[str] = mapped_column(Text, nullable=False)
       rationale: Mapped[str | None] = mapped_column(Text)
       decided_at: Mapped[datetime] = mapped_column(
           DateTime(timezone=True), nullable=False, server_default=func.now()
       )
       decided_by: Mapped[str | None] = mapped_column(Text)
       __table_args__ = (
           CheckConstraint("decision_type IN ('merge','distinct','override')", name="ck_id_type"),
       )


   class FeedbackEvent(Base):
       __tablename__ = "feedback_event"
       id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
       org_id: Mapped[str] = mapped_column(Text, ForeignKey("org.id"), nullable=False)
       user_id: Mapped[str] = mapped_column(Text, ForeignKey("app_user.id"), nullable=False)
       target_type: Mapped[str] = mapped_column(Text, nullable=False)
       target_id: Mapped[str] = mapped_column(Text, nullable=False)
       rating: Mapped[str | None] = mapped_column(Text)
       comment: Mapped[str | None] = mapped_column(Text)
       created_at: Mapped[datetime] = mapped_column(
           DateTime(timezone=True), nullable=False, server_default=func.now()
       )
       __table_args__ = (
           CheckConstraint("target_type IN ('convergence_event','dossier')", name="ck_fe_target_type"),
           Index("idx_fe_target", "target_type", "target_id"),
       )


   # --- placeholders for sub-projects #4 / #6 ---------------------------------

   class ScraperAccount(Base):
       __tablename__ = "scraper_account"
       id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
       source: Mapped[str] = mapped_column(Text, nullable=False)
       credentials: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
       daily_quota: Mapped[int | None] = mapped_column(Integer)
       used_today: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
       last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
       health: Mapped[str] = mapped_column(Text, nullable=False, server_default="healthy")
       ban_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
       org_id: Mapped[str | None] = mapped_column(Text, ForeignKey("org.id"))
       created_at: Mapped[datetime] = mapped_column(
           DateTime(timezone=True), nullable=False, server_default=func.now()
       )
       __table_args__ = (
           CheckConstraint("health IN ('healthy','cooldown','banned')", name="ck_sa_health"),
       )


   class HumanReviewQueue(Base):
       __tablename__ = "human_review_queue"
       id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
       org_id: Mapped[str] = mapped_column(Text, ForeignKey("org.id"), nullable=False)
       item_type: Mapped[str] = mapped_column(Text, nullable=False)
       payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
       status: Mapped[str] = mapped_column(Text, nullable=False, server_default="pending")
       created_at: Mapped[datetime] = mapped_column(
           DateTime(timezone=True), nullable=False, server_default=func.now()
       )
       resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
       resolution: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
       __table_args__ = (
           CheckConstraint("status IN ('pending','resolved','dismissed')", name="ck_hrq_status"),
       )
   ```

2. **Verify imports work:**
   ```
   python -c "from db.models import Base, Person, EdgeEvent, ConvergenceEventRow; print(len(Base.metadata.tables))"
   ```
   Expected: `15` (15 tables, exclusive of pgvector).

3. **Commit:**
   ```
   git add db/models.py
   git commit -m "db: declare all sqlalchemy models for v2 schema"
   ```

---

## Task 4 — Alembic configuration + initial migration

**Files:** create `alembic.ini`, `db/migrations/env.py`, `db/migrations/script.py.mako`, `db/migrations/versions/0001_initial.py`.

1. **Create `alembic.ini`** at repo root:
   ```ini
   [alembic]
   script_location = db/migrations
   sqlalchemy.url = ${DATABASE_URL_DIRECT}
   prepend_sys_path = .
   path_separator = os

   [loggers]
   keys = root,sqlalchemy,alembic

   [handlers]
   keys = console

   [formatters]
   keys = generic

   [logger_root]
   level = WARN
   handlers = console
   qualname =

   [logger_sqlalchemy]
   level = WARN
   handlers =
   qualname = sqlalchemy.engine

   [logger_alembic]
   level = INFO
   handlers =
   qualname = alembic

   [handler_console]
   class = StreamHandler
   args = (sys.stderr,)
   level = NOTSET
   formatter = generic

   [formatter_generic]
   format = %(levelname)-5.5s [%(name)s] %(message)s
   datefmt = %H:%M:%S
   ```

2. **Create `db/migrations/env.py`:**
   ```python
   from __future__ import annotations

   import os
   from logging.config import fileConfig

   from alembic import context
   from dotenv import load_dotenv
   from sqlalchemy import engine_from_config, pool

   load_dotenv()

   from db.models import Base  # noqa: E402

   config = context.config
   config.set_main_option("sqlalchemy.url", os.environ["DATABASE_URL_DIRECT"])

   if config.config_file_name is not None:
       fileConfig(config.config_file_name)

   target_metadata = Base.metadata


   def run_migrations_online() -> None:
       connectable = engine_from_config(
           config.get_section(config.config_ini_section, {}),
           prefix="sqlalchemy.",
           poolclass=pool.NullPool,
       )
       with connectable.connect() as connection:
           context.configure(
               connection=connection,
               target_metadata=target_metadata,
               compare_type=True,
           )
           with context.begin_transaction():
               context.run_migrations()


   if context.is_offline_mode():
       raise RuntimeError("Offline migrations not supported.")
   else:
       run_migrations_online()
   ```

3. **Create `db/migrations/script.py.mako`** — copy the standard Alembic template:
   ```mako
   """${message}

   Revision ID: ${up_revision}
   Revises: ${down_revision | comma,n}
   Create Date: ${create_date}

   """
   from typing import Sequence, Union

   from alembic import op
   import sqlalchemy as sa
   ${imports if imports else ""}

   revision: str = ${repr(up_revision)}
   down_revision: Union[str, Sequence[str], None] = ${repr(down_revision)}
   branch_labels: Union[str, Sequence[str], None] = ${repr(branch_labels)}
   depends_on: Union[str, Sequence[str], None] = ${repr(depends_on)}


   def upgrade() -> None:
       ${upgrades if upgrades else "pass"}


   def downgrade() -> None:
       ${downgrades if downgrades else "pass"}
   ```

4. **Generate the baseline migration:**
   ```
   alembic revision --autogenerate -m "initial schema"
   ```
   This creates `db/migrations/versions/<hash>_initial_schema.py`. Rename it to `0001_initial.py` and edit `revision: str = '0001'`. Set `down_revision = None`.

5. **Edit the generated migration to add what autogen misses:**
   - Append the `pgvector` extension creation at the top of `upgrade()`:
     ```python
     op.execute("CREATE EXTENSION IF NOT EXISTS vector")
     ```
   - Append the partial unique index that the model couldn't express:
     ```python
     op.execute(
         "CREATE UNIQUE INDEX uq_ee_observation_dedup ON edge_event "
         "(source, action_type, watcher_person_id, "
         " COALESCE(target_person_id::text, target_repo_id))"
     )
     ```
   - Append bootstrap rows so `org_id = 'demo'` works out of the box:
     ```python
     op.execute("INSERT INTO org (id, name) VALUES ('demo', 'Demo Org')")
     op.execute("INSERT INTO app_user (id, org_id) VALUES ('demo', 'demo')")
     ```
   - Mirror in `downgrade()`:
     ```python
     op.execute("DELETE FROM app_user WHERE id = 'demo'")
     op.execute("DELETE FROM org      WHERE id = 'demo'")
     op.execute("DROP INDEX IF EXISTS uq_ee_observation_dedup")
     # vector extension intentionally not dropped — other DBs may use it.
     ```

6. **Apply and verify:**
   ```
   alembic upgrade head
   psql "$DATABASE_URL_DIRECT" -c "\dt"
   ```
   Expected: 15 tables listed (org, app_user, person, platform_identity, watchlist_member, repository, repository_owner, edge_event, convergence_event, alert_rule, dossier_classification, identity_decision, feedback_event, scraper_account, human_review_queue) plus `alembic_version`.

7. **Commit:**
   ```
   git add alembic.ini db/migrations/
   git commit -m "db: alembic baseline migration creates v2 schema on neon"
   ```

---

## Task 5 — Migration round-trip test

**Files:** create `tests/db/__init__.py`, `tests/db/test_migration.py`.

1. **Add `tests/db/__init__.py`:** empty.

2. **Create `tests/db/test_migration.py`:**
   ```python
   """Round-trip migration test against a Neon dev branch.

   Skipped if DATABASE_URL_DIRECT is not set (so CI without a DB doesn't fail).
   """
   from __future__ import annotations

   import os
   import subprocess

   import pytest


   @pytest.mark.skipif(
       not os.environ.get("DATABASE_URL_DIRECT"),
       reason="needs DATABASE_URL_DIRECT pointing at a disposable branch",
   )
   def test_alembic_upgrade_then_downgrade_clean():
       env = {**os.environ}
       up = subprocess.run(["alembic", "upgrade", "head"], env=env, capture_output=True, text=True)
       assert up.returncode == 0, up.stderr
       down = subprocess.run(["alembic", "downgrade", "base"], env=env, capture_output=True, text=True)
       assert down.returncode == 0, down.stderr
       up2 = subprocess.run(["alembic", "upgrade", "head"], env=env, capture_output=True, text=True)
       assert up2.returncode == 0, up2.stderr
   ```

3. **Run:**
   ```
   pytest tests/db/test_migration.py -v
   ```
   Expected: `1 passed` against a Neon dev branch (skipped on CI without DB).

4. **Commit:**
   ```
   git add tests/db/
   git commit -m "test: alembic upgrade/downgrade round-trip against a neon dev branch"
   ```

---

## Task 6 — Bootstrap script (CSV → Postgres)

**Files:** create `scripts/bootstrap_demo_data.py`.

The hackathon ID convention (deterministic UUIDv5 from `gh:<handle>`) is preserved from `scrapers/cypher.py:NAMESPACE`. The bootstrap mirrors the previous Cypher-based seed scripts.

1. **Create `scripts/bootstrap_demo_data.py`:**
   ```python
   """Bootstrap a fresh Postgres branch with demo data.

   Idempotent — re-running yields the same row set.

   Run:
       python -m scripts.bootstrap_demo_data
   """
   from __future__ import annotations

   import asyncio
   import csv
   import logging
   import uuid
   from datetime import datetime, timezone
   from pathlib import Path

   from dotenv import load_dotenv
   from sqlalchemy import select
   from sqlalchemy.dialects.postgresql import insert as pg_insert

   load_dotenv()

   from db.engine import session_scope, dispose_engine
   from db.models import Person, PlatformIdentity, WatchlistMember

   logger = logging.getLogger("bootstrap")
   logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

   ROOT = Path(__file__).resolve().parent.parent
   DATA = ROOT / "data"
   NAMESPACE = uuid.UUID("8e1b3f2a-1c5d-4e7f-9a8b-2c3d4e5f6a7b")
   DEMO_ORG = "demo"
   DEMO_USER = "demo"


   def gh_person_id(handle: str) -> uuid.UUID:
       return uuid.uuid5(NAMESPACE, f"gh:{handle.lower()}")


   def li_person_id(slug: str) -> uuid.UUID:
       return uuid.uuid5(NAMESPACE, f"li:{slug.lower()}")


   def tw_person_id(handle: str) -> uuid.UUID:
       return uuid.uuid5(NAMESPACE, f"tw:{handle.lower()}")


   async def upsert_person(session, *, person_id: uuid.UUID, display_name: str,
                           investor_type: str | None, country: str | None,
                           sector_tags: list[str] | None, stage_tags: list[str] | None,
                           entity_type: str = "Investor") -> None:
       now = datetime.now(timezone.utc)
       stmt = pg_insert(Person).values(
           id=person_id,
           org_id=DEMO_ORG,
           display_name=display_name,
           investor_type=investor_type,
           country=country,
           sector_tags=sector_tags,
           stage_tags=stage_tags,
           entity_type=entity_type,
           first_observed_at=now,
           last_observed_at=now,
       ).on_conflict_do_update(
           index_elements=["id"],
           set_={
               "display_name": display_name,
               "investor_type": investor_type,
               "country": country,
               "sector_tags": sector_tags,
               "stage_tags": stage_tags,
               "entity_type": entity_type,
               "last_observed_at": now,
           },
       )
       await session.execute(stmt)


   async def upsert_identity(session, *, person_id: uuid.UUID, platform: str,
                             handle: str, profile_url: str | None) -> None:
       now = datetime.now(timezone.utc)
       stmt = pg_insert(PlatformIdentity).values(
           person_id=person_id,
           platform=platform,
           handle=handle.lower(),
           handle_original=handle,
           profile_url=profile_url,
           verified_via="manual",
           confidence=1.0,
           first_observed_at=now,
           last_observed_at=now,
       ).on_conflict_do_update(
           index_elements=["platform", "handle"],
           set_={
               "person_id": person_id,
               "handle_original": handle,
               "profile_url": profile_url,
               "last_observed_at": now,
           },
       )
       await session.execute(stmt)


   async def upsert_watchlist(session, *, person_id: uuid.UUID, tier: str,
                              archetype: str | None = None, rationale: str | None = None) -> None:
       stmt = pg_insert(WatchlistMember).values(
           org_id=DEMO_ORG, user_id=DEMO_USER, person_id=person_id,
           tier=tier, archetype=archetype, rationale=rationale,
       ).on_conflict_do_update(
           index_elements=["org_id", "user_id", "person_id"],
           set_={"tier": tier, "archetype": archetype, "rationale": rationale},
       )
       await session.execute(stmt)


   async def load_reference_investors(session) -> int:
       count = 0
       with (DATA / "investors_clean.csv").open() as fh:
           for row in csv.DictReader(fh):
               # Pick a stable canonical id: prefer linkedin slug, fall back to twitter handle.
               slug = (row.get("linkedin_slug") or "").strip()
               twh  = (row.get("twitter_handle") or "").strip()
               if not slug and not twh:
                   continue
               pid = li_person_id(slug) if slug else tw_person_id(twh)
               await upsert_person(
                   session,
                   person_id=pid,
                   display_name=row["display_name"],
                   investor_type=(row.get("investor_type") or None),
                   country=(row.get("country") or None),
                   sector_tags=[t for t in (row.get("sector_tags") or "").split("|") if t],
                   stage_tags=[t for t in (row.get("stage_tags") or "").split("|") if t],
                   entity_type="Investor",
               )
               if slug:
                   await upsert_identity(session, person_id=pid, platform="linkedin",
                                          handle=slug, profile_url=(row.get("linkedin_url") or None))
               if twh:
                   await upsert_identity(session, person_id=pid, platform="twitter",
                                          handle=twh, profile_url=None)
               await upsert_watchlist(session, person_id=pid, tier="reference")
               count += 1
       return count


   async def load_active_watchlist(session) -> int:
       count = 0
       with (DATA / "active_watchlist.csv").open() as fh:
           for row in csv.DictReader(fh):
               handle = row["github_handle"].strip()
               pid = gh_person_id(handle)
               await upsert_person(
                   session,
                   person_id=pid,
                   display_name=row["display_name"],
                   investor_type=None,
                   country=None,
                   sector_tags=None,
                   stage_tags=None,
                   entity_type="Investor",
               )
               await upsert_identity(
                   session, person_id=pid, platform="github",
                   handle=handle, profile_url=f"https://github.com/{handle}",
               )
               await upsert_watchlist(
                   session, person_id=pid, tier="active",
                   archetype=row.get("archetype") or None,
                   rationale=row.get("rationale") or None,
               )
               count += 1
       return count


   async def main() -> None:
       async with session_scope() as session:
           ref = await load_reference_investors(session)
           act = await load_active_watchlist(session)
           logger.info("loaded %d reference investors, %d active watchers", ref, act)
       await dispose_engine()


   if __name__ == "__main__":
       asyncio.run(main())
   ```

2. **Run:**
   ```
   python -m scripts.bootstrap_demo_data
   ```
   Expected: `loaded N reference investors, M active watchers` where N≈1000 and M≈10–20.

3. **Verify idempotency (run twice):**
   ```
   python -m scripts.bootstrap_demo_data
   psql "$DATABASE_URL_DIRECT" -c "SELECT COUNT(*) FROM person; SELECT COUNT(*) FROM watchlist_member;"
   ```
   Expected: counts unchanged across runs.

4. **Add `tests/db/test_bootstrap.py`:**
   ```python
   import os
   import subprocess

   import pytest


   @pytest.mark.skipif(
       not os.environ.get("DATABASE_URL_DIRECT"),
       reason="needs DATABASE_URL_DIRECT",
   )
   def test_bootstrap_idempotent():
       def run() -> int:
           r = subprocess.run(
               ["python", "-m", "scripts.bootstrap_demo_data"],
               capture_output=True, text=True, env=os.environ,
           )
           assert r.returncode == 0, r.stderr
           return r.returncode

       run()
       run()  # second run must not error and must not duplicate rows
   ```

5. **Commit:**
   ```
   git add scripts/bootstrap_demo_data.py tests/db/test_bootstrap.py
   git commit -m "scripts: bootstrap demo data from CSVs into Postgres (idempotent)"
   ```

---

## Task 7 — Port `intelligence/convergence.py` (Cypher → SQL CTE)

**Files:** modify `intelligence/convergence.py`. The dataclass and CLI stay; the query and persistence change.

1. **Replace the Cypher-using sections.** Top of the file, replace `from neo4j ...` and `UNIFIED_CONVERGENCE_QUERY` with a SQL builder. Keep the `ConvergenceEvent` dataclass and CLI loop unchanged.

   Add this function (and remove the old Cypher constant):
   ```python
   from sqlalchemy import text

   UNIFIED_CONVERGENCE_SQL = text("""
   WITH window_signals AS (
       SELECT
           ee.target_person_id  AS target_id,
           ee.watcher_person_id AS watcher_id,
           ee.observed_at       AS edge_at,
           ee.action_type       AS signal_type,
           NULL::text           AS repo_full_name,
           ee.evidence_url      AS evidence_url
       FROM edge_event ee
       JOIN watchlist_member w
         ON w.person_id = ee.watcher_person_id
        AND w.user_id   = :user_id
        AND w.tier      = 'active'
       WHERE ee.target_kind = 'person'
         AND ee.observed_at BETWEEN :window_start AND :window_end
         AND ee.org_id = :org_id
         AND NOT EXISTS (
             SELECT 1 FROM watchlist_member wx
             WHERE wx.person_id = ee.target_person_id
               AND wx.user_id   = :user_id
               AND wx.tier      IN ('active','vip')
         )

       UNION ALL

       SELECT
           ro.owner_person_id   AS target_id,
           ee.watcher_person_id AS watcher_id,
           ee.observed_at       AS edge_at,
           ee.action_type       AS signal_type,
           r.full_name          AS repo_full_name,
           ee.evidence_url      AS evidence_url
       FROM edge_event ee
       JOIN repository r        ON r.github_id = ee.target_repo_id
       JOIN repository_owner ro ON ro.repo_id  = r.github_id
       JOIN watchlist_member w
         ON w.person_id = ee.watcher_person_id
        AND w.user_id   = :user_id
        AND w.tier      = 'active'
       WHERE ee.target_kind = 'repository'
         AND ee.action_type = 'star'
         AND ee.observed_at BETWEEN :window_start AND :window_end
         AND ee.org_id = :org_id
         AND ro.owner_person_id <> ee.watcher_person_id
         AND NOT EXISTS (
             SELECT 1 FROM watchlist_member wx
             WHERE wx.person_id = ro.owner_person_id
               AND wx.user_id   = :user_id
               AND wx.tier      IN ('active','vip')
         )
   )
   SELECT
       target_id,
       COUNT(DISTINCT watcher_id) AS distinct_member_count,
       ARRAY_AGG(DISTINCT watcher_id) AS member_ids,
       MIN(edge_at) AS first_signal_at,
       MAX(edge_at) AS last_signal_at,
       JSONB_AGG(JSONB_BUILD_OBJECT(
           'watcher_id',     watcher_id,
           'edge_at',        edge_at,
           'signal_type',    signal_type,
           'repo_full_name', repo_full_name,
           'evidence_url',   evidence_url
       )) AS evidence
   FROM window_signals
   GROUP BY target_id
   HAVING COUNT(DISTINCT watcher_id) >= :min_members
   """)


   async def fetch_window_aggregates(session, *, org_id: str, user_id: str,
                                    window_start, window_end, min_members: int):
       result = await session.execute(
           UNIFIED_CONVERGENCE_SQL,
           {
               "org_id": org_id, "user_id": user_id,
               "window_start": window_start, "window_end": window_end,
               "min_members": min_members,
           },
       )
       return list(result.mappings())
   ```

2. **Rewrite the score loop and persistence** (replace what currently uses Neo4j to MERGE `ConvergenceEvent` nodes):
   ```python
   from sqlalchemy.dialects.postgresql import insert as pg_insert
   from db.models import ConvergenceEventRow, Person


   async def detect_and_persist(session, *, org_id: str, user_id: str,
                                 window_start, window_end, min_members: int,
                                 fired_at) -> list[ConvergenceEvent]:
       rows = await fetch_window_aggregates(
           session, org_id=org_id, user_id=user_id,
           window_start=window_start, window_end=window_end, min_members=min_members,
       )
       events: list[ConvergenceEvent] = []
       for r in rows:
           target_id = r["target_id"]
           # Resolve target display_name
           name_row = await session.execute(
               text("SELECT display_name FROM person WHERE id = :id"),
               {"id": target_id},
           )
           display_name = (name_row.scalar() or "").strip()

           ev = ConvergenceEvent(
               target_id=str(target_id),
               target_name=display_name,
               user_id=user_id,
               fired_at=fired_at.isoformat(),
               window_start=window_start.isoformat(),
               window_end=window_end.isoformat(),
               distinct_member_count=r["distinct_member_count"],
               member_ids=[str(m) for m in r["member_ids"]],
               member_names=[],  # filled by mappers when surfaced via API
               score=float(r["distinct_member_count"]),  # math rewrite is sub-project #3
               score_breakdown={"distinct_members": float(r["distinct_member_count"])},
               first_signal_at=r["first_signal_at"].isoformat() if r["first_signal_at"] else None,
               last_signal_at=r["last_signal_at"].isoformat() if r["last_signal_at"] else None,
               signal_type_counts={},
               evidence=list(r["evidence"] or []),
           )

           stmt = pg_insert(ConvergenceEventRow).values(
               id=ev.event_id,
               org_id=org_id,
               target_person_id=target_id,
               fired_at=fired_at,
               window_start=window_start,
               window_end=window_end,
               distinct_member_count=ev.distinct_member_count,
               member_person_ids=r["member_ids"],
               score=ev.score,
               score_breakdown=ev.score_breakdown,
               first_signal_at=r["first_signal_at"],
               last_signal_at=r["last_signal_at"],
               signal_type_counts=ev.signal_type_counts,
               evidence=ev.evidence,
           ).on_conflict_do_update(
               index_elements=["id"],
               set_={
                   "fired_at": fired_at,
                   "distinct_member_count": ev.distinct_member_count,
                   "member_person_ids": r["member_ids"],
                   "score": ev.score,
                   "score_breakdown": ev.score_breakdown,
                   "evidence": ev.evidence,
               },
           )
           await session.execute(stmt)
           events.append(ev)
       await session.commit()
       return events
   ```

3. **Rewrite the CLI entry** (`if __name__ == "__main__":`) to use `asyncio.run` + `session_scope` instead of the old Neo4j driver. Keep flag parsing and logging.

4. **Smoke test the CLI:**
   ```
   python -m intelligence.convergence --window 365 --min-members 2
   ```
   Expected: emits 0 events on a freshly bootstrapped DB (no `edge_event` rows yet), exits 0.

5. **Commit:**
   ```
   git add intelligence/convergence.py
   git commit -m "intelligence: port convergence query and persistence to postgres"
   ```

---

## Task 8 — Convergence parity test against a fixture

**Files:** create `tests/queries/__init__.py`, `tests/queries/test_convergence_parity.py`.

1. **Create `tests/queries/test_convergence_parity.py`:**
   ```python
   """Convergence parity test.

   Inserts a small fixture set of edge_event rows and verifies the SQL CTE
   returns the expected (target_id, distinct_member_count, member_ids).

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
       reason="needs DATABASE_URL to a clean dev branch",
   )


   @pytest.mark.asyncio
   async def test_convergence_returns_target_with_three_distinct_watchers(monkeypatch, tmp_path):
       from db.engine import session_scope
       from intelligence.convergence import detect_and_persist

       org_id = "demo"
       user_id = "demo"
       now = datetime.now(timezone.utc)
       window_start = now - timedelta(days=90)

       # Fabricate 4 watchers + 1 target
       w_ids = [uuid.uuid4() for _ in range(4)]
       target_id = uuid.uuid4()

       async with session_scope() as s:
           # Wipe + insert fixture data scoped to a temp org so we don't pollute demo data
           await s.execute(text("INSERT INTO org (id, name) VALUES ('test_parity', 'parity') ON CONFLICT DO NOTHING"))
           await s.execute(text("INSERT INTO app_user (id, org_id) VALUES ('test_parity', 'test_parity') ON CONFLICT DO NOTHING"))
           await s.execute(text("DELETE FROM edge_event WHERE org_id = 'test_parity'"))
           await s.execute(text("DELETE FROM watchlist_member WHERE org_id = 'test_parity'"))
           await s.execute(text("DELETE FROM person WHERE org_id = 'test_parity'"))

           for i, w in enumerate(w_ids):
               await s.execute(text(
                   "INSERT INTO person (id, org_id, display_name, entity_type) "
                   "VALUES (:id, 'test_parity', :name, 'User')"),
                   {"id": w, "name": f"Watcher {i}"})
               await s.execute(text(
                   "INSERT INTO watchlist_member (org_id, user_id, person_id, tier) "
                   "VALUES ('test_parity','test_parity',:p,'active')"),
                   {"p": w})

           await s.execute(text(
               "INSERT INTO person (id, org_id, display_name, entity_type) "
               "VALUES (:id, 'test_parity', 'Target', 'User')"),
               {"id": target_id})

           # 3 of 4 watchers follow target inside the window
           for w in w_ids[:3]:
               await s.execute(text(
                   "INSERT INTO edge_event "
                   "(org_id, source, action_type, watcher_person_id, target_kind, target_person_id, "
                   " observed_at, first_seen_at, last_seen_at) "
                   "VALUES ('test_parity', 'github', 'follow', :w, 'person', :t, "
                   " :obs, :obs, :obs)"),
                   {"w": w, "t": target_id, "obs": now - timedelta(days=10)})

       async with session_scope() as s:
           events = await detect_and_persist(
               s, org_id="test_parity", user_id="test_parity",
               window_start=window_start, window_end=now,
               min_members=2, fired_at=now,
           )

       assert len(events) == 1
       assert events[0].target_id == str(target_id)
       assert events[0].distinct_member_count == 3
       assert set(events[0].member_ids) == {str(w) for w in w_ids[:3]}
   ```

2. **Add `pytest-asyncio`** to dev section of `requirements.txt`:
   ```
   pytest-asyncio==0.24.0
   ```
   Re-`pip install -r requirements.txt`.

3. **Run:**
   ```
   pytest tests/queries/test_convergence_parity.py -v
   ```
   Expected: `1 passed`.

4. **Commit:**
   ```
   git add tests/queries/ requirements.txt
   git commit -m "test: convergence parity test for the SQL CTE"
   ```

---

## Task 9 — Read paths in `queries/*.py`

**Files:** create `queries/__init__.py`, `queries/investors.py`, `queries/founders.py`, `queries/convergence.py`, `queries/alerts.py`.

These mirror the four domain blocks in the old `backend/queries.py`. Each function returns the same dict shape the FastAPI mappers already expect.

1. **Create `queries/__init__.py`:** empty.

2. **Create `queries/investors.py`:**
   ```python
   """Read helpers for active/vip watchlist members (replaces backend/queries.py LIST_INVESTORS)."""
   from __future__ import annotations

   from sqlalchemy import text
   from sqlalchemy.ext.asyncio import AsyncSession


   LIST_INVESTORS = text("""
       SELECT
           p.id::text                                 AS id,
           p.display_name                             AS name,
           p.investor_type                            AS investor_type,
           wm.archetype                               AS archetype,
           p.country                                  AS country,
           MAX(CASE WHEN pi.platform = 'linkedin' THEN pi.profile_url END) AS linkedin_url,
           MAX(CASE WHEN pi.platform = 'twitter'  THEN pi.handle      END) AS twitter_handle,
           MAX(CASE WHEN pi.platform = 'github'   THEN pi.handle      END) AS github_handle
       FROM watchlist_member wm
       JOIN person p ON p.id = wm.person_id
       LEFT JOIN platform_identity pi ON pi.person_id = p.id
       WHERE wm.user_id = :user_id
         AND wm.tier IN ('active','vip')
       GROUP BY p.id, p.display_name, p.investor_type, wm.archetype, p.country
       ORDER BY p.display_name
   """)


   async def list_investors(session: AsyncSession, *, user_id: str) -> list[dict]:
       result = await session.execute(LIST_INVESTORS, {"user_id": user_id})
       return [dict(row) for row in result.mappings()]
   ```

3. **Create `queries/founders.py`:** read founder candidates from `convergence_event` joined to `person` and `platform_identity`. Mirror the LIST_FOUNDER_CANDIDATES Cypher's projection (id, name, watcher_count, score, github_*, linkedin_*, twitter_*).
   ```python
   from __future__ import annotations

   from sqlalchemy import text
   from sqlalchemy.ext.asyncio import AsyncSession


   LIST_FOUNDER_CANDIDATES = text("""
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
       result = await session.execute(LIST_FOUNDER_CANDIDATES, {
           "org_id": org_id, "user_id": user_id,
           "min_watchers": min_watchers, "lim": limit,
       })
       return [dict(r) for r in result.mappings()]
   ```

4. **Create `queries/convergence.py`:** the read of `convergence_event` rows joined with target/identity for the ConvergenceAlert API.
   ```python
   from __future__ import annotations

   from sqlalchemy import text
   from sqlalchemy.ext.asyncio import AsyncSession


   LIST_CONVERGENCE_SIGNALS = text("""
       SELECT
           ce.target_person_id::text     AS founder_id,
           p.display_name                AS founder_name,
           MAX(CASE WHEN pi.platform = 'github' THEN pi.handle      END) AS github_handle,
           MAX(CASE WHEN pi.platform = 'github' THEN pi.profile_url END) AS github_url,
           ce.evidence                   AS evidence_json,
           ce.distinct_member_count      AS distinct_watchers,
           ce.score                      AS score,
           ce.score_breakdown            AS score_breakdown_json,
           ce.signal_type_counts         AS signal_type_counts_json,
           ce.first_signal_at            AS first_signal_at,
           ce.last_signal_at             AS last_signal_at,
           ce.window_start               AS window_start,
           ce.window_end                 AS window_end
       FROM convergence_event ce
       JOIN person p ON p.id = ce.target_person_id
       LEFT JOIN platform_identity pi ON pi.person_id = ce.target_person_id
       WHERE ce.org_id = :org_id
         AND ce.distinct_member_count >= :min_watchers
         AND NOT EXISTS (
             SELECT 1 FROM watchlist_member wx
             WHERE wx.person_id = ce.target_person_id
               AND wx.user_id   = :user_id
               AND wx.tier      IN ('active','vip')
         )
       GROUP BY ce.target_person_id, p.display_name, ce.evidence, ce.distinct_member_count,
                ce.score, ce.score_breakdown, ce.signal_type_counts,
                ce.first_signal_at, ce.last_signal_at, ce.window_start, ce.window_end
       ORDER BY ce.score DESC, ce.distinct_member_count DESC, p.display_name
   """)


   async def list_convergence_signals(session: AsyncSession, *, org_id: str, user_id: str,
                                       min_watchers: int) -> list[dict]:
       result = await session.execute(LIST_CONVERGENCE_SIGNALS, {
           "org_id": org_id, "user_id": user_id, "min_watchers": min_watchers,
       })
       return [dict(r) for r in result.mappings()]
   ```

5. **Create `queries/alerts.py`** — alert_rule CRUD using `pg_insert(...).on_conflict_do_update`:
   ```python
   from __future__ import annotations

   from datetime import datetime, timezone

   from sqlalchemy import select
   from sqlalchemy.dialects.postgresql import insert as pg_insert
   from sqlalchemy.ext.asyncio import AsyncSession

   from db.models import AlertRule


   async def get_rule(session: AsyncSession, *, org_id: str, user_id: str) -> dict | None:
       row = await session.execute(
           select(AlertRule).where(AlertRule.org_id == org_id, AlertRule.user_id == user_id)
       )
       obj = row.scalar_one_or_none()
       return dict(obj.payload) if obj else None


   async def save_rule(session: AsyncSession, *, org_id: str, user_id: str, payload: dict) -> None:
       stmt = pg_insert(AlertRule).values(
           org_id=org_id, user_id=user_id, payload=payload,
           updated_at=datetime.now(timezone.utc),
       ).on_conflict_do_update(
           index_elements=["org_id", "user_id"],
           set_={"payload": payload, "updated_at": datetime.now(timezone.utc)},
       )
       await session.execute(stmt)
       await session.commit()
   ```

6. **Run a smoke check:**
   ```
   python -c "
   import asyncio
   from dotenv import load_dotenv; load_dotenv()
   from db.engine import session_scope, dispose_engine
   from queries.investors import list_investors
   async def main():
       async with session_scope() as s:
           print(len(await list_investors(s, user_id='demo')))
       await dispose_engine()
   asyncio.run(main())
   "
   ```
   Expected: count >= active watchlist size (after Task 6 bootstrap).

7. **Commit:**
   ```
   git add queries/
   git commit -m "queries: postgres read helpers for investors, founders, convergence, alerts"
   ```

---

## Task 10 — Port `backend/app.py` driver lifecycle and read paths

**Files:** modify `backend/app.py`. Eventually delete `backend/queries.py`.

1. **Replace the `Neo4jState` + `lifespan` block** at the top of `backend/app.py`:
   ```python
   from contextlib import asynccontextmanager

   from db.engine import dispose_engine, get_engine, get_session

   DEMO_ORG_ID = "demo"
   DEMO_USER_ID = "demo"
   MIN_WATCHERS = int(os.getenv("CONVERGENCE_MIN_WATCHERS", "2"))
   GRAPH_EDGE_LIMIT = int(os.getenv("GRAPH_EDGE_LIMIT", "500"))
   FOUNDER_LIMIT = int(os.getenv("FOUNDER_LIMIT", "100"))


   @asynccontextmanager
   async def lifespan(app: FastAPI):
       get_engine()  # eager init so connection issues surface at startup

       if os.environ.get("PIPELINE_SCHEDULER_DISABLED", "").lower() not in ("1", "true", "yes"):
           from backend import scheduler as _sched
           _sched.start_scheduler()

       yield

       try:
           from backend import scheduler as _sched
           _sched.stop_scheduler()
       except Exception:
           pass
       await dispose_engine()


   app = FastAPI(title="signal-convergence backend", version="0.2.0", lifespan=lifespan)
   ```
   Remove every `from neo4j ...` line and the `Neo4jState` class.

2. **Replace each endpoint's session usage.** Pattern, per endpoint:
   ```python
   from fastapi import Depends
   from sqlalchemy.ext.asyncio import AsyncSession

   from queries import investors as q_investors

   @app.get("/api/investors")
   async def list_investors_endpoint(session: AsyncSession = Depends(get_session)):
       rows = await q_investors.list_investors(session, user_id=DEMO_USER_ID)
       return [map_investor(r) for r in rows]
   ```
   Repeat for `/api/founders`, `/api/alerts`, `/api/graph`, `/api/person/{id}`, `/api/founder/{id}`, the alert-rule endpoints, the dossier regenerate endpoint, the notifier send-now endpoint, the pipeline run endpoint. Each maps to a `queries/*.py` helper.

3. **`/api/health`** — Postgres health check:
   ```python
   from sqlalchemy import text

   @app.get("/api/health")
   async def health(session: AsyncSession = Depends(get_session)):
       result = await session.execute(text("SELECT 1"))
       return {"ok": True, "postgres": result.scalar() == 1, "generatedAt": datetime.now(timezone.utc).isoformat()}
   ```

4. **Run the dev server:**
   ```
   uvicorn backend.app:app --reload --host 127.0.0.1 --port 8000
   ```
   In another shell:
   ```
   curl -s http://127.0.0.1:8000/api/health
   curl -s http://127.0.0.1:8000/api/investors | head -c 400
   ```
   Expected: health returns `{"ok": true, "postgres": true, ...}`; investors returns the bootstrapped active+vip watchlist.

5. **Delete `backend/queries.py`** once all endpoints route through `queries/*.py`.

6. **Commit:**
   ```
   git add backend/app.py
   git rm backend/queries.py
   git commit -m "backend: replace neo4j driver and queries with sqlalchemy + queries/*"
   ```

---

## Task 11 — Port write path in `scrapers/pipeline.py` + jobs

**Files:** modify `scrapers/pipeline.py`, `scrapers/jobs/fetch_following.py`, `scrapers/jobs/fetch_starred_repos.py`, `scrapers/jobs/fetch_twitter_followings.py`. Eventually delete `scrapers/cypher.py`.

The pattern: every Cypher MERGE becomes either an UPSERT against `person` / `platform_identity` / `repository` / `repository_owner`, **or** an UPSERT against `edge_event` (for the actual signal observations).

1. **Add a write-helper module** `scrapers/persistence.py` to keep the pipeline thin:
   ```python
   """Write helpers for scraper jobs.

   Every Cypher MERGE in scrapers/cypher.py maps to one of these. After this
   module is in use, scrapers/cypher.py is deleted.
   """
   from __future__ import annotations

   import uuid
   from datetime import datetime, timezone
   from typing import Any

   from sqlalchemy import text
   from sqlalchemy.dialects.postgresql import insert as pg_insert
   from sqlalchemy.ext.asyncio import AsyncSession

   from db.models import (
       EdgeEvent, PlatformIdentity, Person, Repository, RepositoryOwner,
   )

   NAMESPACE = uuid.UUID("8e1b3f2a-1c5d-4e7f-9a8b-2c3d4e5f6a7b")


   def gh_person_id(handle: str) -> uuid.UUID:
       return uuid.uuid5(NAMESPACE, f"gh:{handle.lower()}")


   async def upsert_person_by_github(session: AsyncSession, *, org_id: str,
                                      handle: str, display_name: str,
                                      profile_url: str, kind: str = "User") -> uuid.UUID:
       pid = gh_person_id(handle)
       now = datetime.now(timezone.utc)
       await session.execute(
           pg_insert(Person).values(
               id=pid, org_id=org_id, display_name=display_name,
               entity_type=kind, role_tags=["observed"], confidence_score=0.6,
               first_observed_at=now, last_observed_at=now,
           ).on_conflict_do_update(
               index_elements=["id"],
               set_={"last_observed_at": now},
           )
       )
       await session.execute(
           pg_insert(PlatformIdentity).values(
               person_id=pid, platform="github",
               handle=handle.lower(), handle_original=handle,
               profile_url=profile_url, verified_via="observed", confidence=0.6,
               kind=kind, first_observed_at=now, last_observed_at=now,
           ).on_conflict_do_update(
               index_elements=["platform", "handle"],
               set_={"last_observed_at": now, "person_id": pid},
           )
       )
       return pid


   async def upsert_repository(session: AsyncSession, *, github_id: str,
                                owner_handle: str, name: str, full_name: str,
                                description: str | None, language: str | None,
                                star_count: int | None, html_url: str) -> None:
       now = datetime.now(timezone.utc)
       await session.execute(
           pg_insert(Repository).values(
               github_id=github_id, owner_handle=owner_handle,
               name=name, full_name=full_name, description=description,
               language=language, star_count_observed=star_count,
               html_url=html_url, last_fetched_at=now,
           ).on_conflict_do_update(
               index_elements=["github_id"],
               set_={
                   "owner_handle": owner_handle, "name": name, "full_name": full_name,
                   "description": description, "language": language,
                   "star_count_observed": star_count, "html_url": html_url,
                   "last_fetched_at": now,
               },
           )
       )


   async def link_repo_owner(session: AsyncSession, *, repo_github_id: str,
                              owner_handle: str, org_id: str,
                              owner_display_name: str | None = None) -> None:
       owner_pid = await upsert_person_by_github(
           session, org_id=org_id, handle=owner_handle,
           display_name=(owner_display_name or owner_handle),
           profile_url=f"https://github.com/{owner_handle}",
       )
       await session.execute(
           pg_insert(RepositoryOwner).values(
               repo_id=repo_github_id, owner_person_id=owner_pid,
           ).on_conflict_do_nothing()
       )


   async def record_edge_event(session: AsyncSession, *, org_id: str, source: str,
                                action_type: str, watcher_person_id: uuid.UUID,
                                target_kind: str,
                                target_person_id: uuid.UUID | None = None,
                                target_repo_id: str | None = None,
                                observed_at: datetime, evidence_url: str | None = None,
                                edge_confidence: float | None = None,
                                metadata: dict[str, Any] | None = None) -> None:
       """Idempotent: re-observation updates last_seen_at; first_seen_at is fixed.

       The dedup is by the partial unique index uq_ee_observation_dedup. We use raw
       SQL with ON CONFLICT on the index expression because SQLAlchemy doesn't
       compose this case cleanly.
       """
       now = datetime.now(timezone.utc)
       await session.execute(
           text("""
               INSERT INTO edge_event
                 (org_id, source, action_type, watcher_person_id, target_kind,
                  target_person_id, target_repo_id, observed_at, first_seen_at, last_seen_at,
                  evidence_url, edge_confidence, metadata)
               VALUES (:org_id, :source, :action_type, :watcher_id, :target_kind,
                       :target_person_id, :target_repo_id, :observed_at, :first_seen_at, :last_seen_at,
                       :evidence_url, :edge_confidence, CAST(:metadata AS JSONB))
               ON CONFLICT (source, action_type, watcher_person_id,
                            COALESCE(target_person_id::text, target_repo_id))
               DO UPDATE SET last_seen_at = EXCLUDED.last_seen_at,
                             evidence_url = COALESCE(EXCLUDED.evidence_url, edge_event.evidence_url)
           """),
           {
               "org_id": org_id, "source": source, "action_type": action_type,
               "watcher_id": watcher_person_id, "target_kind": target_kind,
               "target_person_id": target_person_id, "target_repo_id": target_repo_id,
               "observed_at": observed_at, "first_seen_at": now, "last_seen_at": now,
               "evidence_url": evidence_url, "edge_confidence": edge_confidence,
               "metadata": (metadata and __import__("json").dumps(metadata)),
           },
       )
   ```

2. **Update `scrapers/pipeline.py`:** replace `from scrapers import cypher` and the Cypher session calls with calls to `scrapers.persistence`. The orchestration (active watchlist read, GitHub client paging) stays. Each `session.run(cypher.MERGE_STARRED_REPO, …)` becomes:
   ```python
   await record_edge_event(
       session, org_id="demo", source="github", action_type="star",
       watcher_person_id=watcher_pid, target_kind="repository",
       target_repo_id=event.repo_github_id,
       observed_at=event.starred_at,
       evidence_url=event.repo_html_url,
   )
   ```
   Each `session.run(cypher.UPSERT_REPOSITORY, …)` becomes `await upsert_repository(session, …)`. Each `session.run(cypher.UPSERT_PERSON_BY_GITHUB, …)` becomes `await upsert_person_by_github(session, …)`.

3. **Update the three jobs in `scrapers/jobs/`** to call the new helpers. The jobs currently iterate and yield events; only the persistence-side handler changes.

4. **Smoke test:**
   ```
   python -m scrapers.pipeline --limit 1 --skip-stars
   ```
   Expected: emits log lines for following ingest; `psql ... -c "SELECT COUNT(*) FROM edge_event;"` returns >0.

5. **Delete `scrapers/cypher.py`:**
   ```
   git rm scrapers/cypher.py
   ```

6. **Commit:**
   ```
   git add scrapers/persistence.py scrapers/pipeline.py scrapers/jobs/
   git commit -m "scrapers: write to postgres edge_event/person/repository (delete cypher.py)"
   ```

---

## Task 12 — Port file-based persistence (identity, dossier, feedback, alerts)

**Files:** modify `identity/resolver.py`, `identity/llm_arbiter.py`, `intelligence/dossier/dossier.py`, `intelligence/dossier/feedback.py`, `intelligence/notifier.py`, `intelligence/rule.py`. Archive the JSONL/CSV files.

Each module has a small set of "write decision to JSONL" and "read decisions from JSONL" functions. Replace each with the corresponding `queries/*.py` or `db/*` calls.

1. **Add `queries/identity.py`:**
   ```python
   from sqlalchemy.ext.asyncio import AsyncSession
   from db.models import IdentityDecision


   async def record_identity_decision(session: AsyncSession, *, person_id, decision_type: str,
                                       rationale: str | None, decided_by: str) -> None:
       session.add(IdentityDecision(
           person_id=person_id, decision_type=decision_type,
           rationale=rationale, decided_by=decided_by,
       ))
       await session.commit()
   ```

2. **Add `queries/dossier.py`:**
   ```python
   from datetime import datetime, timezone
   from sqlalchemy.dialects.postgresql import insert as pg_insert
   from sqlalchemy.ext.asyncio import AsyncSession
   from db.models import DossierClassification


   async def upsert_classification(session: AsyncSession, *, convergence_event_id: str,
                                    classification: str, rationale: str | None) -> None:
       stmt = pg_insert(DossierClassification).values(
           convergence_event_id=convergence_event_id,
           classification=classification, rationale=rationale,
           classified_at=datetime.now(timezone.utc),
       ).on_conflict_do_update(
           index_elements=["convergence_event_id"],
           set_={"classification": classification, "rationale": rationale},
       )
       await session.execute(stmt)
       await session.commit()
   ```

3. **Add `queries/feedback.py`:**
   ```python
   from sqlalchemy.ext.asyncio import AsyncSession
   from db.models import FeedbackEvent


   async def record_feedback(session: AsyncSession, *, org_id: str, user_id: str,
                              target_type: str, target_id: str,
                              rating: str | None, comment: str | None) -> None:
       session.add(FeedbackEvent(
           org_id=org_id, user_id=user_id,
           target_type=target_type, target_id=target_id,
           rating=rating, comment=comment,
       ))
       await session.commit()
   ```

4. **Replace JSONL writes** in `identity/llm_arbiter.py`, `identity/resolver.py`, `intelligence/dossier/dossier.py`, `intelligence/dossier/feedback.py` with the helpers above. Each module currently has a small `_append_jsonl(path, obj)` helper — replace its call sites with the relevant Postgres helper. Keep the JSONL helper itself in place but unused (delete after smoke).

5. **Replace `intelligence/rule.py`'s `data/alert_rules.json` reads/writes** with `queries.alerts.get_rule` / `save_rule`.

6. **Archive the JSONL/CSV files** (optional but tidy):
   ```
   mkdir -p data/_archive
   mv data/identity_decisions.jsonl data/identity_overrides.csv \
      data/dossier_classifications.jsonl data/alert_rules.json data/_archive/
   ```

7. **Smoke test the API end-to-end:**
   ```
   uvicorn backend.app:app --reload --host 127.0.0.1 --port 8000 &
   sleep 2
   curl -s http://127.0.0.1:8000/api/health
   curl -s -XPOST http://127.0.0.1:8000/api/feedback \
        -H "content-type: application/json" \
        -d '{"target_type":"convergence_event","target_id":"x","rating":"good"}'
   psql "$DATABASE_URL_DIRECT" -c "SELECT COUNT(*) FROM feedback_event;"
   kill %1
   ```
   Expected: health OK; feedback insert returns success; feedback_event row count = 1.

8. **Commit:**
   ```
   git add queries/identity.py queries/dossier.py queries/feedback.py \
            identity/ intelligence/ data/_archive/
   git commit -m "persistence: replace JSONL/CSV decision logs with postgres tables"
   ```

---

## Task 13 — `docker-compose.yml` (drop Neo4j, add Postgres)

**Files:** modify `docker-compose.yml` (or create if absent).

1. **Replace any Neo4j service** with a Postgres+pgvector service:
   ```yaml
   services:
     postgres:
       image: pgvector/pgvector:pg16
       restart: unless-stopped
       environment:
         POSTGRES_USER: anytrace
         POSTGRES_PASSWORD: anytrace
         POSTGRES_DB: anytrace
       ports:
         - "5432:5432"
       volumes:
         - postgres_data:/var/lib/postgresql/data
       healthcheck:
         test: ["CMD-SHELL", "pg_isready -U anytrace -d anytrace"]
         interval: 5s
         timeout: 3s
         retries: 20

   volumes:
     postgres_data: {}
   ```

2. **Run:**
   ```
   docker compose up -d postgres
   docker compose ps
   ```
   Expected: `postgres` is `healthy`.

3. **Set local fallback `DATABASE_URL`** in `.env.example` for users who don't want Neon for dev:
   ```
   # Local Docker fallback (alternative to Neon for offline dev)
   # DATABASE_URL=postgresql+asyncpg://anytrace:anytrace@127.0.0.1:5432/anytrace
   # DATABASE_URL_DIRECT=postgresql+psycopg://anytrace:anytrace@127.0.0.1:5432/anytrace
   ```

4. **Commit:**
   ```
   git add docker-compose.yml .env.example
   git commit -m "docker-compose: postgres+pgvector service replaces neo4j"
   ```

---

## Task 14 — Final cleanup + tag

1. **Verify no Neo4j references remain in production code:**
   ```
   grep -rn "neo4j\|cypher\|Cypher\|GraphDatabase" --include="*.py" \
       --exclude-dir=.venv --exclude-dir=__pycache__ \
       backend intelligence identity scrapers queries db scripts
   ```
   Expected: no matches.

2. **Run all tests:**
   ```
   pytest -v
   ```
   Expected: all tests pass against the dev Neon branch.

3. **Tag the cutover:**
   ```
   git tag v0.2-postgres-foundation
   ```

4. **Commit any final tidy-ups:**
   ```
   git add -A
   git commit -m "chore: drop neo4j references and finalize postgres foundation" || true
   ```

---

## Done criteria (from spec §12)

- [ ] No `from neo4j` import in `backend/`, `intelligence/`, `identity/`, `scrapers/`, `queries/`, `db/`, `scripts/`.
- [ ] `docker compose up postgres` runs Postgres locally; no `neo4j` service in `docker-compose.yml`.
- [ ] `python -m scripts.bootstrap_demo_data` brings a fresh Neon branch to demo state, idempotently.
- [ ] `python -m intelligence.convergence` produces convergence events end-to-end against Postgres.
- [ ] FastAPI endpoints `/api/health`, `/api/investors`, `/api/founders`, `/api/alerts`, `/api/graph`, `/api/person/{id}`, `/api/founder/{id}` return identical shapes to today.
- [ ] `pytest` is green.
- [ ] `git tag v0.2-postgres-foundation` is in place.
