# Sub-project #1 — Event Log + Postgres Foundation

**Author:** Mohammed Jaseel Kunnathodika
**Date:** 2026-05-08
**Status:** Draft for review
**Parent:** [`2026-05-08-anytrace-v2-architecture-design.md`](./2026-05-08-anytrace-v2-architecture-design.md)
**Estimate:** ~2 weeks (1 engineer)

---

## 1. Goal

Replace Neo4j with **Postgres + pgvector** as the single source of truth for AnyTrace, and introduce an **immutable event log** (`edge_event`) as the data-modeling primitive every other sub-project depends on.

After this sub-project ships:

- All read and write paths in the FastAPI backend talk to Postgres, not Neo4j.
- Every observed signal (a follow, a star, a connection, etc.) lives in `edge_event` as an immutable row, with provenance (source, evidence URL, raw artifact ref).
- Multi-tenancy seams are in place (`org_id` on every table) even though real auth comes in sub-project #7.
- pgvector extension is enabled (used in sub-project #6).
- The Neo4j Docker service, driver, and Cypher modules are deleted.

The convergence math stays simple in this sub-project (a direct port of the current Cypher to SQL CTE). The **rewrite** of the math is sub-project #3 — out of scope here.

## 2. Why this is the foundation

The umbrella doc names this as the foundation because:

- Every other sub-project reads or writes to this schema. If we get it wrong, every later spec is built on shifting ground.
- The event log enables backfill, replay, debugging, multi-source fan-in, and time-windowed scoring — none of which the current Neo4j schema does cleanly.
- Migrating later (after we've added LinkedIn, multi-tenancy, billing) is 10× more disruptive than now, when we have no paying customers.

## 3. Decisions (locked for this spec)

| Decision | Choice | Why |
|---|---|---|
| Postgres host | **Neon** (one project, branches: `main` = prod, `dev` = dev, ephemeral per-PR) | Branching is the productivity multiplier; pure Postgres, no lock-in |
| Migration strategy | **Greenfield** (no Neo4j → Postgres ETL) | User confirmed Neo4j docker volume is already deleted; hackathon data is small and re-derivable from CSVs and re-scraping |
| ORM | **SQLAlchemy 2.0 async** (with asyncpg driver) | Mature, typed (`Mapped[T]`), code-first migrations via Alembic, async-native |
| Migrations | **Alembic** | Python ecosystem standard; integrates with SQLAlchemy 2.0 cleanly |
| ID strategy | UUIDv5 for canonical `person.id` (preserves determinism), `BIGSERIAL` for log tables, `TEXT` for org/user/repo IDs | Deterministic Person IDs preserve the existing logic in `scrapers/cypher.py:github_person_id` so re-bootstrapping the demo data produces identical IDs |
| Vector extension | **pgvector** enabled from day 1 even though unused until #6 | Avoids a future migration; free to enable |
| Multi-tenancy seam | Every row carries `org_id`; bootstrap a single `'demo'` org | Sub-project #7 adds Clerk + RLS later; the column is the seam |
| Connection pooling | Neon's pooler (PgBouncer-compatible) for prod; direct connection for migrations | Standard Neon pattern |

## 4. Schema

### 4.1 Multi-tenancy roots

```sql
CREATE TABLE org (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE app_user (
    id          TEXT PRIMARY KEY,
    org_id      TEXT NOT NULL REFERENCES org(id),
    email       TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_app_user_org ON app_user(org_id);

-- Bootstrap rows so the demo path keeps working without auth.
INSERT INTO org      (id, name)         VALUES ('demo', 'Demo Org');
INSERT INTO app_user (id, org_id, email) VALUES ('demo', 'demo', NULL);
```

### 4.2 Canonical entities

```sql
CREATE TABLE person (
    id                 UUID PRIMARY KEY,
    org_id             TEXT NOT NULL REFERENCES org(id),
    display_name       TEXT NOT NULL,
    investor_type      TEXT,
    country            TEXT,
    sector_tags        TEXT[],
    stage_tags         TEXT[],
    role_tags          TEXT[],
    confidence_score   REAL,
    entity_type        TEXT,                 -- 'User' | 'Investor' | etc.
    first_observed_at  TIMESTAMPTZ,
    last_observed_at   TIMESTAMPTZ,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_person_org           ON person(org_id);
CREATE INDEX idx_person_entity_type   ON person(entity_type);

CREATE TABLE platform_identity (
    id                  BIGSERIAL PRIMARY KEY,
    person_id           UUID NOT NULL REFERENCES person(id) ON DELETE CASCADE,
    platform            TEXT NOT NULL CHECK (platform IN ('github','twitter','linkedin')),
    handle              TEXT NOT NULL,        -- lowercased
    handle_original     TEXT,
    profile_url         TEXT,
    verified_via        TEXT,                 -- 'observed' | 'manual' | 'llm_arbitrated'
    confidence          REAL,
    kind                TEXT,
    first_observed_at   TIMESTAMPTZ,
    last_observed_at    TIMESTAMPTZ,
    UNIQUE (platform, handle)
);
CREATE INDEX idx_pi_person ON platform_identity(person_id);
```

### 4.3 Watchlist (replaces `WATCHED_BY` edges)

```sql
CREATE TABLE watchlist_member (
    org_id      TEXT NOT NULL REFERENCES org(id),
    user_id     TEXT NOT NULL REFERENCES app_user(id),
    person_id   UUID NOT NULL REFERENCES person(id),
    tier        TEXT NOT NULL CHECK (tier IN ('active','vip','reference')),
    archetype   TEXT,
    rationale   TEXT,
    added_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (org_id, user_id, person_id)
);
CREATE INDEX idx_wm_user_tier ON watchlist_member(user_id, tier);
```

### 4.4 Repositories

```sql
CREATE TABLE repository (
    github_id            TEXT PRIMARY KEY,    -- GitHub's numeric ID as string
    owner_handle         TEXT NOT NULL,
    name                 TEXT NOT NULL,
    full_name            TEXT NOT NULL,
    description          TEXT,
    language             TEXT,
    star_count_observed  INTEGER,
    html_url             TEXT,
    last_fetched_at      TIMESTAMPTZ,
    first_observed_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_repo_full_name ON repository(full_name);

-- Repository ownership: structural, not a signal — kept separate from edge_event.
CREATE TABLE repository_owner (
    repo_id          TEXT NOT NULL REFERENCES repository(github_id),
    owner_person_id  UUID NOT NULL REFERENCES person(id),
    PRIMARY KEY (repo_id, owner_person_id)
);
```

### 4.5 The event log — the central new primitive

```sql
CREATE TABLE edge_event (
    id                  BIGSERIAL PRIMARY KEY,
    org_id              TEXT NOT NULL REFERENCES org(id),
    source              TEXT NOT NULL CHECK (source IN ('github','twitter','linkedin')),
    action_type         TEXT NOT NULL,      -- 'follow' | 'star' | 'reply' | 'mention' | 'connection' | ...
    watcher_person_id   UUID NOT NULL REFERENCES person(id),
    target_kind         TEXT NOT NULL CHECK (target_kind IN ('person','repository')),
    target_person_id    UUID    REFERENCES person(id),
    target_repo_id      TEXT    REFERENCES repository(github_id),
    observed_at         TIMESTAMPTZ NOT NULL,    -- when we believe the action happened
    first_seen_at       TIMESTAMPTZ NOT NULL,    -- first time WE saw it
    last_seen_at        TIMESTAMPTZ NOT NULL,    -- most recent time we saw it
    removed_at          TIMESTAMPTZ,             -- NULL = still observed
    evidence_url        TEXT,
    edge_confidence     REAL,
    raw_artifact_ref    TEXT,                    -- R2/S3 path to raw scrape
    metadata            JSONB,
    CHECK (
        (target_kind = 'person'     AND target_person_id IS NOT NULL AND target_repo_id IS NULL) OR
        (target_kind = 'repository' AND target_repo_id   IS NOT NULL AND target_person_id IS NULL)
    ),
    UNIQUE (source, action_type, watcher_person_id,
            COALESCE(target_person_id::text, target_repo_id))
);

-- Indexes shaped for the convergence query (target-side aggregation, time-windowed).
CREATE INDEX idx_ee_target_person_observed
    ON edge_event(target_person_id, observed_at)
    WHERE target_kind = 'person';

CREATE INDEX idx_ee_target_repo_observed
    ON edge_event(target_repo_id, observed_at)
    WHERE target_kind = 'repository';

CREATE INDEX idx_ee_watcher_observed   ON edge_event(watcher_person_id, observed_at);
CREATE INDEX idx_ee_org_source_observed ON edge_event(org_id, source, observed_at);
```

**Why immutable:** every observation is a fact. We don't UPDATE state in place; we write new rows. `last_seen_at` and `removed_at` are the only mutable columns, and they are the *story of the same edge over time*. This is what makes backfill, replay, debugging, and time-windowed scoring trivial in later sub-projects.

### 4.6 Convergence events (derived/materialized)

```sql
CREATE TABLE convergence_event (
    id                    TEXT PRIMARY KEY,        -- "cv-{org}-{target}-{window_end_date}"
    org_id                TEXT NOT NULL REFERENCES org(id),
    target_person_id      UUID NOT NULL REFERENCES person(id),
    fired_at              TIMESTAMPTZ NOT NULL,
    window_start          TIMESTAMPTZ NOT NULL,
    window_end            TIMESTAMPTZ NOT NULL,
    distinct_member_count INTEGER NOT NULL,
    member_person_ids     UUID[] NOT NULL,
    score                 REAL NOT NULL,
    score_breakdown       JSONB,
    first_signal_at       TIMESTAMPTZ,
    last_signal_at        TIMESTAMPTZ,
    signal_type_counts    JSONB,
    evidence              JSONB
);
CREATE INDEX idx_ce_org_score   ON convergence_event(org_id, score DESC);
CREATE INDEX idx_ce_target      ON convergence_event(target_person_id);
```

### 4.7 Existing artifacts ported from JSONL/CSV files

```sql
-- Replaces data/alert_rules.json
CREATE TABLE alert_rule (
    org_id      TEXT NOT NULL REFERENCES org(id),
    user_id     TEXT NOT NULL REFERENCES app_user(id),
    payload     JSONB NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (org_id, user_id)
);

-- Replaces data/dossier_classifications.jsonl
CREATE TABLE dossier_classification (
    convergence_event_id  TEXT PRIMARY KEY REFERENCES convergence_event(id),
    classification        TEXT NOT NULL,
    rationale             TEXT,
    classified_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Replaces data/identity_decisions.jsonl + data/identity_overrides.csv
CREATE TABLE identity_decision (
    id           BIGSERIAL PRIMARY KEY,
    person_id    UUID NOT NULL REFERENCES person(id),
    decision_type TEXT NOT NULL CHECK (decision_type IN ('merge','distinct','override')),
    rationale    TEXT,
    decided_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    decided_by   TEXT                  -- 'llm:gemini' | user_id | 'manual'
);

-- New — captures user thumbs-up/down on alerts and dossiers.
CREATE TABLE feedback_event (
    id           BIGSERIAL PRIMARY KEY,
    org_id       TEXT NOT NULL REFERENCES org(id),
    user_id      TEXT NOT NULL REFERENCES app_user(id),
    target_type  TEXT NOT NULL CHECK (target_type IN ('convergence_event','dossier')),
    target_id    TEXT NOT NULL,
    rating       TEXT,                  -- 'good' | 'noise' | 'wrong'
    comment      TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_fe_target ON feedback_event(target_type, target_id);
```

### 4.8 Placeholder tables for future sub-projects

Defined now to avoid future migrations. Empty until later sub-projects populate them.

```sql
-- Used by sub-project #4
CREATE TABLE scraper_account (
    id            BIGSERIAL PRIMARY KEY,
    source        TEXT NOT NULL,
    credentials   JSONB NOT NULL,        -- app-encrypted
    daily_quota   INTEGER,
    used_today    INTEGER NOT NULL DEFAULT 0,
    last_used_at  TIMESTAMPTZ,
    health        TEXT NOT NULL DEFAULT 'healthy'
                  CHECK (health IN ('healthy','cooldown','banned')),
    ban_count     INTEGER NOT NULL DEFAULT 0,
    org_id        TEXT REFERENCES org(id),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Used by sub-project #6
CREATE TABLE human_review_queue (
    id            BIGSERIAL PRIMARY KEY,
    org_id        TEXT NOT NULL REFERENCES org(id),
    item_type     TEXT NOT NULL,         -- 'identity_match' | 'dossier_low_confidence' | ...
    payload       JSONB NOT NULL,
    status        TEXT NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending','resolved','dismissed')),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()  ,
    resolved_at   TIMESTAMPTZ,
    resolution    JSONB
);

-- pgvector extension — enabled now even though unused until #6.
CREATE EXTENSION IF NOT EXISTS vector;
```

## 5. Convergence query (Cypher → SQL port)

Today's `intelligence/convergence.py:UNIFIED_CONVERGENCE_QUERY` does a UNION across `FOLLOWS_ON_GITHUB` and `STARRED_REPO` edges. The SQL equivalent reads from `edge_event`. The query below is **illustrative** — the exact aggregation shape (especially the `signal_type_counts` and `evidence` payloads) will be tuned during implementation against a fixture dataset to match the existing dataclass output:

```sql
WITH window_signals AS (
    -- Person-targeted signals (follow, mention, reply, connection)
    SELECT  ee.target_person_id           AS target_id,
            ee.watcher_person_id          AS watcher_id,
            ee.observed_at                AS edge_at,
            ee.action_type                AS signal_type,
            NULL::text                    AS repo_full_name,
            ee.evidence_url               AS evidence_url
    FROM edge_event ee
    JOIN watchlist_member w
      ON w.person_id = ee.watcher_person_id
     AND w.user_id   = :user_id
     AND w.tier      = 'active'
    WHERE ee.target_kind = 'person'
      AND ee.observed_at >= :window_start
      AND ee.observed_at <= :window_end
      AND ee.org_id = :org_id
      AND NOT EXISTS (
          SELECT 1 FROM watchlist_member wx
          WHERE wx.person_id = ee.target_person_id
            AND wx.user_id   = :user_id
            AND wx.tier      IN ('active','vip')
      )

    UNION ALL

    -- Repo-targeted signals (star) attributed to the repo's owner
    SELECT  ro.owner_person_id            AS target_id,
            ee.watcher_person_id          AS watcher_id,
            ee.observed_at                AS edge_at,
            ee.action_type                AS signal_type,
            r.full_name                   AS repo_full_name,
            ee.evidence_url               AS evidence_url
    FROM edge_event ee
    JOIN repository r        ON r.github_id        = ee.target_repo_id
    JOIN repository_owner ro ON ro.repo_id         = r.github_id
    JOIN watchlist_member w
      ON w.person_id = ee.watcher_person_id
     AND w.user_id   = :user_id
     AND w.tier      = 'active'
    WHERE ee.target_kind = 'repository'
      AND ee.action_type = 'star'
      AND ee.observed_at >= :window_start
      AND ee.observed_at <= :window_end
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
    COUNT(DISTINCT watcher_id)            AS distinct_member_count,
    ARRAY_AGG(DISTINCT watcher_id)        AS member_ids,
    MIN(edge_at)                          AS first_signal_at,
    MAX(edge_at)                          AS last_signal_at,
    JSONB_OBJECT_AGG(signal_type, signal_count)
        FILTER (WHERE signal_type IS NOT NULL) AS signal_type_counts,
    JSONB_AGG(JSONB_BUILD_OBJECT(
        'watcher_id',   watcher_id,
        'edge_at',      edge_at,
        'signal_type',  signal_type,
        'repo_full_name', repo_full_name,
        'evidence_url', evidence_url
    ))                                    AS evidence
FROM (
    SELECT target_id, watcher_id, edge_at, signal_type, repo_full_name, evidence_url,
           COUNT(*) OVER (PARTITION BY target_id, signal_type) AS signal_count
    FROM window_signals
) AS s
GROUP BY target_id
HAVING COUNT(DISTINCT watcher_id) >= :min_members;
```

This is a **direct port** of the existing math (`score = distinct_member_count + recency_bonus`). The math rewrite (watcher-tier weighting, base-rate calibration, time decay, independence checks, founder-prior multiplier) is sub-project #3.

## 6. Code layout

New module structure for the Python backend:

```
db/
  __init__.py
  engine.py         # SQLAlchemy 2.0 async engine + session factory
  models.py         # all ORM models, typed via Mapped[]
  migrations/       # Alembic
    env.py
    versions/
      0001_initial.py
queries/
  __init__.py
  investors.py      # was backend/queries.py
  founders.py
  convergence.py
  alerts.py
intelligence/
  convergence.py    # SQL CTE + ConvergenceEvent dataclass (kept for now; rewritten in #3)
  ...
scrapers/
  github_client.py  # unchanged
  pipeline.py       # writes to edge_event instead of Cypher MERGE
  jobs/             # unchanged interface; new persistence layer
backend/
  app.py            # reads via queries/*; no neo4j driver
scripts/
  bootstrap_demo_data.py   # CSV → Postgres (replaces scripts/load_*_to_neo4j.py)
```

**Deleted:** `scrapers/cypher.py`, all `cypher.<UPSERT|MERGE|RETURN>_*` constants, every `from neo4j import` line, the Neo4j service in `docker-compose.yml`, the `neo4j==5.28.1` line in `requirements.txt`.

## 7. Bootstrap script

`scripts/bootstrap_demo_data.py` — single command that brings a fresh Postgres branch to a useful demo state:

1. Apply migrations (`alembic upgrade head`).
2. Verify bootstrap rows in `org` and `app_user` (created by migration).
3. Load `data/investors_clean.csv` into `person` (tier `'reference'`) + `platform_identity` rows for each available platform handle.
4. Load `data/active_watchlist.csv` into `watchlist_member` (tier `'active'`) + `platform_identity` for the GitHub handle.
5. Load any high-priority VIPs from a config list into `watchlist_member` (tier `'vip'`).

Idempotent — re-running yields the same row set (UPSERTs on UUIDv5-derived `person.id`).

## 8. Tasks

| # | Task | Output | Done when |
|---|---|---|---|
| 1 | Provision Neon project | `main` and `dev` branches; connection strings in `.env.example` | API can connect from local dev |
| 2 | Add deps | Add `sqlalchemy[asyncio]`, `asyncpg`, `alembic`, `pgvector` to `requirements.txt`. Remove `neo4j`. | `requirements.txt` updated |
| 3 | Define ORM models | `db/models.py` with all tables in §4 | Models import cleanly; type-checked |
| 4 | Generate Alembic baseline | `db/migrations/versions/0001_initial.py` | `alembic upgrade head` creates the schema |
| 5 | Bootstrap script | `scripts/bootstrap_demo_data.py` | Fresh Neon branch becomes demo-ready in <1 min |
| 6 | Port `intelligence/convergence.py` | Cypher → SQL CTE; dataclass unchanged | Convergence test passes against new schema |
| 7 | Port read paths in `backend/app.py` and `backend/queries.py` | `queries/*.py` with typed return shapes | API endpoints return identical shapes to today |
| 8 | Port write paths in `scrapers/pipeline.py` | Inserts into `edge_event` instead of MERGE Cypher | Pipeline run produces convergence events end-to-end |
| 9 | Port identity decisions and feedback writes | Postgres tables instead of JSONL files | LLM-arbitrated decisions land in `identity_decision`; feedback in `feedback_event` |
| 10 | Update `docker-compose.yml` | Drop Neo4j service; add Postgres service | `docker-compose up` runs Postgres for local dev |
| 11 | Delete Neo4j code | `scrapers/cypher.py` removed; `from neo4j import` lines gone; driver out of requirements | `grep -r neo4j` returns docs only |
| 12 | Tests | Schema migration test, convergence parity snapshot test, bootstrap idempotency test | `pytest` green |

## 9. Tests

- **Migration test**: `alembic upgrade head` then `alembic downgrade base` succeeds without errors on a clean DB.
- **Bootstrap idempotency**: running `scripts/bootstrap_demo_data.py` twice yields identical row counts.
- **Convergence parity**: with a fixture set of `edge_event` rows, the SQL CTE returns the same `(target_id, distinct_member_count, member_ids)` tuples as the old Cypher would have. Stored as a golden JSON snapshot in `intelligence/test_convergence.py`.
- **API smoke tests**: the existing `backend/test_*` tests run green against Postgres.

## 10. Risks and rollback

| Risk | Mitigation |
|---|---|
| Schema bugs surface late | Per-endpoint feature flag `USE_POSTGRES_READS` (default `true` in dev, `false` in prod until cutover); endpoints can flip individually |
| SQL CTE slower than Cypher at scale | Bench against a 100K-event fixture; add covering indexes if any query >100ms |
| Bootstrap script duplicates rows on re-run | Use UUIDv5 for canonical `person.id`; UPSERT on `(platform, handle)` for `platform_identity` |
| Neon branch limits hit during dev | Free tier supports 10 branches; clean up stale PR branches via Neon GitHub Action |
| Deleted Neo4j code is needed for retro debugging | Tag the last Neo4j commit (`v0.1-neo4j-final`) before deletion; revert is one `git checkout` away |

Greenfield = no production data loss risk.

## 11. Out of scope (deferred to later sub-projects)

- **Convergence math rewrite** (watcher tiers, base rates, time decay, independence, founder priors) → **#3**.
- **Source abstraction protocol** + Procrastinate queue + worker process split → **#2**. *In this sub-project, APScheduler stays in `backend/app.py:lifespan` — we replace the storage, not the orchestration.*
- **Account / browser / proxy pools** → **#4**.
- **LinkedIn `Source` impl** → **#5**.
- **Identity resolution v2** (pgvector embeddings, structured DSPy/Pydantic-AI pipeline, human review UI) → **#6**. *In this sub-project, the existing Gemini arbiter logic stays unchanged; only its persistence moves to Postgres.*
- **Real Clerk auth + RLS policies** → **#7**. *In this sub-project, `org_id = 'demo'` is hardcoded everywhere reads/writes happen, just like `DEMO_USER_ID = 'demo'` is today.*

## 12. Done criteria

- All FastAPI endpoints serve from Postgres; no `neo4j` import remains in production code.
- `docker-compose up` runs Postgres locally (no Neo4j).
- Convergence events are produced end-to-end from a `python -m scrapers.pipeline` run, written via `edge_event`, surfaced in the dashboard.
- The umbrella architecture doc's §4 changes 1, 3, and 9 (Source abstraction is **partially** in place — typed write helpers per source — but the full Source Protocol comes in #2; event log fully in place; three-tier storage **partially** in place — normalized Postgres tier exists, raw R2 tier comes with #4) are reflected in the running system.
- All §8 tasks are merged to `main`; tests in §9 are green in CI.
- `git tag v0.2-postgres-foundation` cut.
