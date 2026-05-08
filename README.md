# AnyTrace — Backend

> Detect when N investors from a user-configured watchlist independently converge on the same founder across GitHub, Twitter, and LinkedIn — *before* that founder publicly raises.

This repo is the backend (FastAPI + Postgres + Procrastinate worker fleet). The companion SPA lives at [`signal-convergence`](../signal-convergence) (or wherever you cloned it).

**Status: v0.5** — sub-projects 1–4 of 7 shipped. See [Roadmap status](#roadmap-status) below.

---

## What's working today

- ✅ GitHub crawl (follows + stars) → Postgres `edge_event` log
- ✅ Convergence detection v2 (watcher tier × time decay × surprise × founder prior)
- ✅ FastAPI serving the live frontend (investors / founders / alerts / graph / person / founder / alert-rule)
- ✅ Procrastinate-backed worker fleet with periodic jobs (no more APScheduler)
- ✅ Source plugin protocol (GitHub live; Twitter + LinkedIn stubs)
- ✅ Account pool + proxy router + Browserless browser pool — infrastructure ready for #5

## What's deferred

- 🚫 Twitter (X) crawler — schema-ready, no live crawler. Ships in #5.
- 🚫 LinkedIn crawler — the differentiator. Ships in #5.
- 🚫 Dossier generation, email digest, identity-resolution v2 — these endpoints currently 501. Ship in #6.
- 🚫 Multi-tenancy (Clerk + RLS) — currently single-org `'demo'`. Ships in #7.

---

## Quick start (local dev)

### Prerequisites

- Python **3.12** (3.11 should also work)
- Docker (for the local Postgres instance — Postgres 16 + pgvector)
- A GitHub personal access token (any classic token with no scopes is fine; we only read public data)
- Optional: a Gemini API key (only needed once #6 lands)

### 1. Clone + install

```bash
git clone <repo-url> tum_ai
cd tum_ai

python3.12 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Configure environment

```bash
cp .env.example .env
```

Edit `.env`:

```ini
# Use the local docker fallback (uncomment these two lines)
DATABASE_URL=postgresql+asyncpg://anytrace:anytrace@127.0.0.1:5433/anytrace
DATABASE_URL_DIRECT=postgresql+psycopg://anytrace:anytrace@127.0.0.1:5433/anytrace

GITHUB_TOKEN=ghp_xxx                # required for the pipeline
GITHUB_USERNAME=your-handle         # informational, used in logs
```

You can leave the Neon-flavoured commented examples for the day you switch to a managed DB.

### 3. Bring up Postgres

```bash
docker compose up -d postgres
```

Wait ~5 seconds for it to become healthy (`docker compose ps` shows `(healthy)`).

### 4. Apply the schema

```bash
python -m alembic upgrade head
python -m scripts.apply_procrastinate_schema   # one-off, adds Procrastinate's tables
```

### 5. Seed demo data

```bash
python -m scripts.bootstrap_demo_data
```

You should see `loaded 187 reference investors, 31 active watchers`. The seed is idempotent — running twice produces the same row counts.

### 6. Crawl a few watchers (live GitHub data)

The first time, run inline so you don't need a worker process:

```bash
python -m scrapers.pipeline --inline --limit 10 --max-pages 2 --skip-stars
```

That fetches the followings of the first 10 active watchers (~600 follow events) and writes them to `edge_event`. Stars are skipped because they pull a lot more pages — turn them on once your token has headroom.

### 7. Compute convergence

```bash
python -m intelligence.convergence --window 365 --min-members 2 --persist
```

You'll see something like:

```
Convergences fired: 3

#   score   N target        watchers
1  41.68   2  dgryski       Ben Johnson, Brad Fitzpatrick
2  41.68   2  josharian     Ben Johnson, Brad Fitzpatrick
3  41.68   2  progrium      Ben Johnson, Brad Fitzpatrick

Persisted 3 ConvergenceEvent rows.
```

### 8. Start the API

```bash
.venv/bin/uvicorn backend.app:app --host 127.0.0.1 --port 8001 --reload
```

Quick smoke checks:

```bash
curl http://127.0.0.1:8001/api/health
curl http://127.0.0.1:8001/api/founders | jq
```

Now point the [frontend](../signal-convergence) at `http://localhost:8001` and you have the full demo loop.

---

## Running the worker fleet

For continuous crawling (instead of inline runs), spin up a worker:

```bash
python -m worker
```

The worker drains scrape + intel jobs from Procrastinate. It also picks up the periodic schedules (`pipeline_sweep` every 6h, `convergence_recompute` every 1h).

Enqueue a sweep manually:

```bash
python -m scrapers.pipeline --limit 10 --max-pages 2     # default mode is --queue
```

Each active watcher becomes one `crawl_watcher_for_source` job; once they drain, a `recompute_convergence` runs.

### Multiple processes via docker-compose (optional)

Local dev can run native (faster). For a prod-shaped topology:

```bash
docker compose --profile api --profile worker up
```

For LinkedIn / Twitter scraping (#5):

```bash
docker compose --profile browser up postgres browserless
```

---

## Repository layout

```
.
├── backend/                   # FastAPI app (api process)
│   ├── app.py                 # endpoints — investors, founders, alerts, graph, person, founder, alert-rule
│   └── mappers.py             # DB rows → frontend DTO shapes
├── db/
│   ├── engine.py              # async SQLAlchemy 2.0 engine + session factory
│   ├── models.py              # ORM (16 tables) — see schema below
│   └── migrations/            # Alembic migrations 0001..0003
├── queries/                   # Read helpers (one module per domain)
├── scrapers/
│   ├── base.py + types.py     # Source Protocol, WatcherInfo, CrawlResult, LeasedResources
│   ├── github_source.py       # GitHubSource — follows + stars
│   ├── twitter_source.py      # TwitterSource (stub until #5)
│   ├── persistence.py         # write helpers (edge_event UPSERT, repo + person upserts)
│   ├── pipeline.py            # orchestrator with --queue (default) and --inline modes
│   └── github_client.py       # PyGithub wrapper with multi-token rotation
├── intelligence/
│   ├── convergence.py         # Cypher → SQL CTE port; uses scoring v2
│   ├── scoring.py             # composable score components (weight × decay × surprise × prior)
│   └── rule.py                # AlertRule (per-org policy; backed by alert_rule table)
├── infra/                     # Sub-project #4 — scrape infrastructure
│   ├── accounts.py            # account checkout (FOR UPDATE SKIP LOCKED), outcome reporting
│   ├── proxies.py             # proxy router with deterministic watcher stickiness
│   └── browser.py             # Playwright-over-Browserless session helper
├── worker/                    # Procrastinate worker process
│   ├── app.py                 # App instance + connector
│   ├── jobs.py                # crawl_watcher_for_source, recompute_convergence, dispatch_pipeline_sweep
│   └── periodic.py            # cron-like schedules
├── scripts/
│   ├── bootstrap_demo_data.py
│   └── apply_procrastinate_schema.py
├── tests/                     # 40 tests, all green
├── data/                      # Seed CSVs (investors_clean, active_watchlist)
├── docs/                      # superpowers/ specs and plans for each sub-project
├── alembic.ini
├── docker-compose.yml         # postgres (default) + api/worker/browserless (opt-in profiles)
├── Dockerfile                 # shared image for api and worker services
└── requirements.txt
```

---

## Schema overview

16 tables, all in `db/models.py`. Highlights:

| Table | Purpose |
|---|---|
| `org`, `app_user` | Multi-tenancy roots (single-org `'demo'` until #7) |
| `person` | Canonical entity — UUIDv5 from `gh:<handle>` / `li:<slug>` for determinism |
| `platform_identity` | (person_id, platform, handle) — github / twitter / linkedin |
| `watchlist_member` | (org, user, person, tier, archetype, weight) — drives convergence inputs |
| `edge_event` | **The event log.** Immutable signal observations (follows, stars, mentions, …) |
| `repository`, `repository_owner` | GitHub repos + ownership links |
| `convergence_event` | Materialized convergence detections; the API reads from here |
| `alert_rule` | Per-(org,user) JSON policy — thresholds, weights, half-lives |
| `scraper_account`, `proxy`, `crawl_lease` | Sub-project #4 infrastructure for stealth scraping |
| `human_review_queue`, `identity_decision`, `dossier_classification`, `feedback_event` | Filled in by sub-projects #6 / #7 |

`pgvector` extension is enabled (used by #6 for identity-resolution embeddings).

---

## Convergence scoring v2 — the moat

```
for each (watcher, signal):
    weight   = archetype_weight (angel=3.0, vc_partner=2.0, …) ×  per-watcher override
    decay    = 0.5 ** (age_days / source_half_life)                 # 30d for follows, 14d for stars
    surprise = log1p((population + α) / (watcher_outbound + β))     # base-rate calibrated
    contrib  = weight × decay × surprise

independence:  stars/retweets within 60min collapse to one contribution
               follows pass through (crawl-time, not event-time)

raw           = sum(post-independence contribs)
founder_prior = 1 + log10(stars/100), capped at 4×
score         = raw × founder_prior
```

All knobs live on `intelligence.rule.AlertRule` and are persisted to the `alert_rule` table per `(org_id, user_id)`. Edit them via `PUT /api/alert-rule`.

---

## Roadmap status

| # | Sub-project | Tag | Status |
|---|---|---|---|
| 1 | Postgres foundation + event log | `v0.2-postgres-foundation` | ✅ shipped |
| 2 | Source abstraction + Procrastinate workers | `v0.3-source-abstraction` | ✅ shipped |
| 3 | Convergence math v2 | `v0.4-convergence-math` | ✅ shipped |
| 4 | Scrape infra (accounts / proxies / browsers) | `v0.5-scrape-infra` | ✅ shipped |
| 5 | LinkedIn (+ own-infra Twitter) source impl | — | ⏳ next |
| 6 | Identity resolution v2 + dossier + feedback | — | ⏳ |
| 7 | Multi-tenancy (Clerk) + RLS + billing | — | ⏳ |

Detailed specs and implementation plans live in `docs/superpowers/specs/` and `docs/superpowers/plans/`.

---

## Tests

```bash
.venv/bin/pytest tests/ -v
```

Should report 40 passing. The migration round-trip test wipes demo data — re-run `python -m scripts.bootstrap_demo_data` after the test suite if you want fresh dashboard data.

---

## Key environment variables

| Var | Purpose |
|---|---|
| `DATABASE_URL` | asyncpg URL used by the API + worker |
| `DATABASE_URL_DIRECT` | psycopg URL used by Alembic + sync rule helpers |
| `GITHUB_TOKEN` (+ `_2`/`_3`/`_4` for rotation) | PyGithub client tokens |
| `BROWSERLESS_WS_URL` | optional, only when running stealth sources (#5) |
| `PIPELINE_INTERVAL_HOURS` | periodic sweep cadence (default 6) |
| `CONVERGENCE_INTERVAL_HOURS` | periodic recompute cadence (default 1) |
| `CONVERGENCE_MIN_WATCHERS` | API floor for `/api/founders` (default 2) |
| `GRAPH_EDGE_LIMIT`, `FOUNDER_LIMIT` | API result caps |

---

## Pointing the frontend at this backend

In the [`signal-convergence`](../signal-convergence) repo, set:

```bash
echo "VITE_API_BASE_URL=http://localhost:8001" >> .env
bun install && bun run dev
```

That's it — the SPA reads the same shapes the API serves.
