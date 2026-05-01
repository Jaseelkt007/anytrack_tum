# Social Sourcing for VCs

Detect when N investors from a user-configured watchlist independently converge on the same founder across GitHub (Phase 1), Twitter (Phase 2), and LinkedIn (Phase 3) — before that founder publicly raises.

## Documents

- **[`PROBLEM_STATEMENT.md`](./PROBLEM_STATEMENT.md)** — the validated user problem (interview with Omar)
- **[`BUILD_PLAN_V2.md`](./BUILD_PLAN_V2.md)** — full architecture and vision (low mutation rate)
- **[`PHASE_1_PLAN.md`](./PHASE_1_PLAN.md)** — Phase 1 implementation contract (the doc the team executes against)

Read these in order if you're new to the project.

## Phase 1 quickstart for new team members

You are setting up so the M1 acceptance check passes on your laptop:

> `python scripts/healthcheck.py` prints `Neo4j OK, GitHub PAT OK`.

### 1. Prerequisites

- Python 3.11+ (3.12 recommended)
- Node.js 20+ (only needed for `/frontend`, M7)
- A Neo4j AuraDB Free account (free tier, no credit card)
- A personal GitHub account

### 2. Clone and install

```bash
git clone <repo-url> tum_ai
cd tum_ai

python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 3. Provision Neo4j AuraDB Free

1. Sign up at <https://console.neo4j.io>.
2. Create a new **Aura Free** instance. Free tier: 200K nodes / 400K relationships — plenty for Phase 1.
3. **Save the generated password** (it is only shown once).
4. Copy the connection URI from the dashboard. It looks like `neo4j+s://xxxxxxxx.databases.neo4j.io`.

The team should agree on **one shared instance** for Phase 1 so everyone queries the same data. P1 provisions and shares credentials via a private channel (do not paste into the repo).

### 4. Create your GitHub PAT

1. Go to <https://github.com/settings/tokens?type=beta> (fine-grained) or the classic tokens page.
2. Generate a new token. Required scope: `public_repo` (or no scopes — we only read public data in Phase 1).
3. Copy the token immediately.

Each team member uses their own PAT. Four PATs × 5K req/hour = 20K req/hour, which is what the M3 ingestion pipeline budget assumes.

### 5. Set up `.env`

```bash
cp .env.example .env
```

Edit `.env` and fill in:
- `NEO4J_URI`, `NEO4J_USER`, `NEO4J_PASSWORD` — from the Aura dashboard
- `GITHUB_TOKEN` — your PAT
- `GITHUB_USERNAME` — your GitHub username

`.env` is git-ignored. Never commit it.

### 6. Apply the schema

This loads the uniqueness constraints and indexes from `scripts/schema.cypher`.

```bash
python scripts/apply_schema.py
```

You only need to run this **once** per Neo4j instance. If you provisioned the shared instance, run it; otherwise skip it (your teammates already did).

### 7. Run the healthcheck

```bash
python scripts/healthcheck.py
```

Expected output:

```
Phase 1 healthcheck
--------------------------------------------------
  [OK  ] Neo4j: connected to neo4j+s://...
  [OK  ] GitHub PAT: PAT valid, rate limit 5000/5000
  [OK  ] Anthropic (optional): not set (optional in Phase 1)  [optional]
--------------------------------------------------
M1 acceptance met for this laptop. Neo4j OK, GitHub PAT OK.
```

If you see FAIL lines, fix them before moving on.

### 8. Verify the schema in Neo4j Browser

Open the Aura console → "Open with" → Neo4j Browser. Run:

```cypher
SHOW CONSTRAINTS;
```

You should see the constraints listed in `scripts/schema.cypher` (Person, Repository, User, ConvergenceEvent, PlatformIdentity).

## Repository layout

```
.
├── backend/          # FastAPI app (M6)
├── frontend/         # Next.js app (M7)
├── scrapers/         # GitHub ingestion (M3)
├── intelligence/     # Convergence detection (M4)
├── backtest/         # Lovable case + validation (M5)
├── data/             # Seed CSVs, fixtures, identity overrides
├── scripts/          # One-off operational scripts (schema, healthcheck, loaders)
├── docs/             # Internal docs (API key inventory, etc.)
├── requirements.txt
├── .env.example
├── PROBLEM_STATEMENT.md
├── BUILD_PLAN_V2.md
└── PHASE_1_PLAN.md
```

## Working agreements

- Read `PHASE_1_PLAN.md` before starting any milestone.
- Anything tagged `STABLE` in that doc requires team sign-off to change.
- Add a one-line entry to the decision log at the bottom of `PHASE_1_PLAN.md` when you make a decision the team should remember.

## Per-milestone owners (Phase 1)

See `PHASE_1_PLAN.md` for the full breakdown. Quick reference:

- **P1 — Data**: M1 lead, M3 owner
- **P2 — Intelligence**: M4 owner
- **P3 — Backtest / Demo data**: M2, M2.5, M5 owner
- **P4 — Frontend + API**: M6, M7 owner
- **All**: M8 (demo dry-run + retro)
