# AnyTrace v2 — Architecture Redesign

**Author:** Mohammed Jaseel Kunnathodika
**Date:** 2026-05-08
**Status:** Draft for review
**Type:** Umbrella architecture doc (referenced by per-milestone specs)

---

## 1. Why this document exists

AnyTrace started as a 24-hour TUM AI hackathon project. The product hypothesis (validated by Omar, a German VC) is sound: surface convergence signals where multiple trusted angels/investors independently engage with the same potential founder *before* that founder publicly raises. Specter tried this and failed (fabricated signals); Evertrace doesn't attempt it. There is real customer demand and a real moat available.

The hackathon stack was chosen for time-to-demo, not for scale, accuracy, or operational health. Now that the project is becoming a startup, the foundation needs to be redesigned with three properties in mind:

1. **Future-proof** — the same primitives carry us from 1 design partner to 250+ paying VCs without a rewrite.
2. **Source-pluggable** — adding LinkedIn (the #1 customer ask), and later Substack / Crunchbase / X / Instagram, is a 1–2 week add, not a 3-month rewrite.
3. **Signal-quality first** — the current convergence score is naïve and will produce the same noise that killed Specter's credibility. Fixing this is the product, not a side quest.

This document is the **target end-state**. Each per-milestone spec (sub-projects #1–#7 below) references it.

---

## 2. Today's stack — honest snapshot

### Backend (`/mnt/d/tum_ai`, ~5,900 LOC Python)

| Component | Today | Issue |
|---|---|---|
| Web API | FastAPI, exposed via ngrok for demo | Fine framework; deployment is hackathon glue |
| Database | Neo4j AuraDB Free | Operationally heavy, vendor-locked, expensive at scale, single store |
| Scheduler | APScheduler **inside FastAPI lifespan** | Cannot horizontally scale; jobs share process with HTTP |
| GitHub scraping | PyGithub with multi-token rotation | Works |
| Twitter scraping | Scrapebadger (3rd-party paid API) | Brittle, expensive at scale, no defensible moat |
| LinkedIn scraping | **Does not exist** | LinkedIn is the customer's #1 ask |
| Identity resolution | Module with file caches + Gemini LLM arbiter | Logic is sound; persistence layer is JSONL/CSV files |
| Feedback / decisions | JSONL files in `data/` | Not durable, no concurrency, no audit trail |
| Multi-tenancy | `DEMO_USER_ID = "demo"` hardcoded (`backend/app.py:67`) | None |
| Observability | None | Will fly blind once LinkedIn bans start |

### Frontend (`/mnt/d/signal-convergence`, Vite + React 18 SPA)

| Component | Today | Issue |
|---|---|---|
| Framework | Vite + React 18 + react-router-dom (SPA) | No SSR; fine for app, weak for marketing/SEO |
| UI library | shadcn/ui + Radix + TanStack Query + @xyflow/react | Excellent — keep |
| Auth | Supabase client wired in | Not used for org-scoped multi-tenancy |
| Backend coupling | Talks to FastAPI via ngrok | OK for now |

**Out of scope for this document:** the frontend stays on Vite for now. A future sub-project may revisit.

---

## 3. Stack decisions

These are locked decisions for v2. Per-milestone specs do not relitigate them.

### 3.1 Backend language: **Python everywhere**

- **FastAPI** for the HTTP API.
- **Procrastinate** (Postgres-backed job queue) for background work, replacing APScheduler.
- **Crawlee-Python** + **Camoufox** + **Patchright** for stealth scraping.
- **Pydantic-AI** (or DSPy / LangGraph as we evaluate) for LLM orchestration.
- **scikit-learn** / **numpy** for the convergence math.

**Why not polyglot:** A 1–3 person team cannot afford two CI pipelines, two debug stacks, two dependency systems. Python's ML, scraping, and web ecosystems are best-in-class for what AnyTrace is. The 5–10% concurrency gap vs. Node in browser automation is not worth a polyglot ops burden.

### 3.2 Datastore: **Postgres + pgvector** (drop Neo4j)

- **Postgres 16** — recommended baseline **Neon** (branching is a real productivity multiplier for a small team); **Supabase** acceptable if we keep Supabase auth. Final hosting choice tracked in §9.
- **pgvector** for identity-resolution embeddings — same database, no separate vector store.
- **Cloudflare R2** (or Vercel Blob) for raw scrape artifacts (HTML, JSON dumps, screenshots).
- Optional future: **FalkorDB** (Cypher-on-Redis, OSS) as a derived read-only graph index *if and only if* we hit a query pattern Postgres genuinely cannot handle.

**Why not Neo4j:** AnyTrace's actual queries are 1- and 2-hop, time-windowed aggregations with scoring. Postgres CTEs handle this cleanly with proper indexes up to ~50M edges. Neo4j's value (deep traversals, rich graph semantics) doesn't match this workload, while Aura's pricing trajectory and ops weight are real startup risks.

### 3.3 Job queue: **Procrastinate** (Postgres-native)

- Same database as the application; no Redis service to babysit.
- Crons defined in code, not in OS.
- Three separate processes: `api` (FastAPI), `worker` (Procrastinate), `scheduler` (Procrastinate cron). Each scales horizontally.

### 3.4 Scraping fleet: own infrastructure

- **Browser pool:** self-hosted `browserless/chromium` Docker container in dev; Browserbase managed when concurrency demands it.
- **Stealth:** Camoufox (anti-fingerprint Firefox) + Patchright (patched Playwright) + per-account `user_data_dir`.
- **Proxies:** IPRoyal residential to start (~$3/GB), graduate to Bright Data when volume justifies it. Mobile proxies for LinkedIn specifically.
- **Account pool:** every scraper credential (LinkedIn cookies, Twitter sessions, GitHub PATs) lives in a `scraper_account` table with quota tracking and health status.
- **Captcha fallback:** 2Captcha or CapSolver when stealth alone fails.

### 3.5 Multi-tenancy: **org-scoped from day one**

- Every Postgres row carries `org_id` (and `user_id` where relevant); composite indexes scope queries.
- Recommended baseline **Clerk** (Vercel Marketplace) for orgs/SSO/billing; **Supabase Auth** acceptable if we keep Supabase elsewhere. Final auth choice tracked in §9.
- Postgres **Row-Level Security (RLS)** as a defense-in-depth backstop.

### 3.6 Deployment topology

| Service | Runtime | Where |
|---|---|---|
| Frontend (Vite SPA) | Static | Vercel or Cloudflare Pages |
| API (FastAPI) | Long-lived process | Fly.io machines / Railway |
| Workers (Procrastinate) | Long-lived processes | Fly.io machines |
| Browsers (Browserless) | Docker | Fly.io machines / Hetzner cloud |
| Postgres | Managed | Neon |
| Object storage | Managed | Cloudflare R2 |
| Logs / metrics | Managed | Axiom or PostHog + Sentry |
| Email digest | Managed | Resend (already in use) |

**Local dev:** one `docker-compose.yml` runs Postgres + Browserless + worker + api + scheduler. Same primitives as prod, scaled to one replica.

### 3.7 Observability

Every scrape attempt logs `(source, account_id, proxy_ip, target, latency_ms, success, ban_indicator, evidence_url)`. Dashboards from day one of LinkedIn pipeline. Sentry for application errors. PostHog for product analytics once customers exist.

---

## 4. The 12 architectural changes

Ranked by importance. These are the *kinds of change*; the per-milestone specs translate them into concrete tasks.

### Foundation layer (data + scheduling)

1. **Pluggable Source abstraction.** Every platform implements one Protocol (`list_following`, `fetch_profile`, `resolve_identity`). Today every source has a bespoke shape (`scrapers/clients/twitter_following_client.py` vs. `scrapers/github_client.py`).
2. **Job queue with typed payloads.** Replace `scrapers/pipeline.py` (a script) with `job(source, action, target, options) → result`. Workers pull from Procrastinate. Procrastinate-backed (Postgres-native).
3. **Event-sourced edge log.** Every observed signal is an immutable row: `(id, source, observed_at, watcher_canonical_id, target_canonical_id, action_type, evidence_url, raw_artifact_ref, org_id)`. Convergence reads the event log. Backfill, debug, and replay become trivial.

### Scraping infrastructure (the LinkedIn enabler)

4. **Account pool service.** Generic `scraper_account(source, credentials_jsonb, daily_quota, used_today, last_used_at, health, ban_count, org_id)`. Workers checkout an account, do work, return it.
5. **Browser pool service.** Browserless (or Browserbase). Worker requests a session, does N actions, releases.
6. **Proxy router.** Geo-aware proxy selection. Track per-IP health. Stick a watcher to a proxy/account combo for LinkedIn.
7. **Per-source rate limiting & budget enforcement.** Each source has a budget (X requests/account/day). Queue respects budgets.
8. **Stealth/fingerprint config per source.** Camoufox + per-account `user_data_dir` + cookie persistence for LinkedIn.

### Data layer

9. **Three-tier storage** — raw → normalized → enriched. Raw artifacts to R2; normalized to Postgres typed tables; enriched (with identity resolution + scoring) downstream. Each layer reproducible from the prior.
10. **Identity resolution as a service.** `resolve(IdentityHint) → CanonicalPerson | None` with pgvector embeddings + LLM fallback + a `human_review_queue` table for ambiguous cases. Replaces `identity_decisions.jsonl` and `identity_overrides.csv`.

### Multi-tenancy & ops

11. **Tenant-scoped crawling.** Every job, row, and API call carries `org_id`. Watchlists per org. Shared targets crawled once but allocated by who asked first. Postgres RLS as backstop.
12. **Observability primitives baked in.** Structured logs, per-VC cost tracking, scraper health dashboards from day one.

---

## 5. Sub-project breakdown (the milestones)

Each is independently deliverable, has its own design spec, plan, and execution. Dependencies are explicit.

### Dependency graph

```
                   ┌──────────────────────────────────┐
                   │ #1 Event log + Postgres          │ ← FOUNDATION
                   └────────────┬─────────────────────┘
                                │
           ┌────────────────────┼────────────────┬─────────────────┐
           ▼                    ▼                ▼                 ▼
  ┌─────────────────┐  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐
  │ #2 Source proto │  │ #3 Convergence│  │ #6 Identity  │  │ #7 Multi-    │
  │   + job queue   │  │    math v2    │  │    res v2    │  │   tenancy    │
  │   + workers     │  │  (parallel)   │  │  (parallel)  │  │  (parallel)  │
  └────────┬────────┘  └──────────────┘  └──────────────┘  └──────────────┘
           │
           ▼
  ┌─────────────────┐
  │ #4 Scrape infra │
  │   (accounts,    │
  │   browsers,     │
  │   proxies)      │
  └────────┬────────┘
           │
           ▼
  ┌─────────────────┐
  │ #5 LinkedIn     │ ← THE DIFFERENTIATOR
  │   source impl   │
  └─────────────────┘
```

### #1 — Event log + Postgres migration *(Foundation, ~2 weeks)*

- Define schema: `person`, `edge_event`, `repository`, `convergence_event`, `watchlist`, `org`, `scraper_account`, `human_review_queue`.
- Migrate Neo4j data → Postgres (one-shot ETL script).
- Update FastAPI read paths to use Postgres; keep Neo4j read-only fallback for one week of parity testing.
- Cutover, drop Neo4j.

**Blocks:** #2, #3 (cleanly), #6, #7.

### #2 — Source abstraction + job queue + worker split *(Spine, ~2 weeks)*

- Define `Source` Protocol with the methods listed in §4.1.
- Port GitHub and Twitter clients to implement it.
- Install Procrastinate; define typed jobs (`crawl_following`, `fetch_profile`, `score_target`).
- Split FastAPI process into `api` / `worker` / `scheduler`.
- Kill `APScheduler` from `backend/app.py:lifespan`.

**Depends on:** #1. **Blocks:** #4.

### #3 — Convergence math rewrite *(Moat, ~1–2 weeks, parallel-safe)*

- Watcher-tier weighting (T1 angel = 3.0, mid-VC = 1.0, etc.).
- Base-rate calibration: `signal_strength = observed / expected` with Bayesian smoothing per watcher.
- Time decay (~14d half-life Twitter, ~30d LinkedIn).
- Independence check — cluster signals from the same originating event (e.g., a viral retweet cascade counts once).
- Founder-prior multiplier (GitHub stars, role keywords, follower count).
- Final score: `Σ (watcher_weight × time_decay × independence) × founder_prior`.

**Depends on:** #1 (for the new `edge_event` schema). Can be drafted on the old Neo4j data and ported on cutover.

### #4 — Scrape infrastructure *(LinkedIn enabler, ~2 weeks)*

- `scraper_account` table + checkout/checkin service.
- `browserless` Docker integration; session lease API.
- Proxy router with geo + health tracking.
- Per-source budget enforcement in the job queue.

**Depends on:** #2. **Blocks:** #5.

### #5 — LinkedIn source implementation *(Differentiator, ~3–4 weeks, biggest single piece)*

- LinkedIn `Source` impl: profile fetch, connections, recent activity, posts.
- Account warming guide + automation hooks.
- Behavioral mimicry: dwell time, scroll patterns, session pauses.
- Ban recovery playbook + automated quarantine.

**Depends on:** #4.

### #6 — Identity resolution v2 *(Correctness, ~1–2 weeks, parallel)*

- pgvector embedding store for identity candidates.
- Replace JSONL caches with Postgres tables.
- Human review queue UI surface (lives in the existing frontend).
- Promote LLM arbitration from one-shot Gemini calls to a structured DSPy/Pydantic-AI pipeline.

**Depends on:** #1. Parallel-safe with everything else.

### #7 — Multi-tenancy + auth + billing *(SaaS shell, ~1–2 weeks, parallel)*

- Add `org_id` to every table; add Postgres RLS policies.
- Clerk integration (Vercel Marketplace).
- Frontend org switcher + watchlist scoping.
- Stripe billing primitives (usage-based: per VC seat + per profile crawled).

**Depends on:** #1. Parallel-safe with everything else.

---

## 6. Migration order and timeline

Realistic with 1–2 engineers:

| Weeks | Sub-projects | Output |
|---|---|---|
| 1–2 | #1 | Event log live, Neo4j drained, Postgres is source of truth |
| 3–4 | #2 + #3 in parallel | Workers split out; convergence math v2 deployed |
| 5–6 | #4 + #7 in parallel | Browser/proxy/account fleet live; auth and orgs live |
| 7–10 | #5 | LinkedIn pipeline scraping at design-partner scale |
| 11–12 | #6 + polish | Identity resolution v2 + design-partner onboarding |

Roughly 3 calendar months from "hackathon prototype" to "VC-ready beta."

---

## 7. What stays unchanged

To prevent scope drift during the rebuild:

- **Frontend (Vite SPA):** stays. May revisit later but is not in scope for this document.
- **Resend** for email digests.
- **Gemini** as one of the LLM providers (now invoked through a structured pipeline rather than ad-hoc).
- **Lovable-generated UI components:** keep them. shadcn/Radix is framework-portable; nothing to redo.
- **GitHub PyGithub client:** stays as a working backend; only the orchestration around it changes.

---

## 8. What we are deliberately not doing now

YAGNI list — call out so we can resist scope creep:

- **Frontend rewrite to Next.js.** Vite works; revisit when marketing-site SEO becomes a priority.
- **Kubernetes.** Fly.io machines / Hetzner / Railway are sufficient until we exceed ~30 worker replicas.
- **Real-time streaming pipeline (Kafka / Redpanda).** Unnecessary at 250 VCs and ~500K profiles. Procrastinate handles this scale.
- **Custom ML training.** Use off-the-shelf embedding models + Bayesian scoring until accuracy data justifies anything fancier.
- **Scope-creep features from `PROBLEM_STATEMENT.md`'s "out of scope" list:** hackathon-winner detection, vesting monitoring, geographic coverage analysis, etc.
- **Building our own proxy infra.** Buy from IPRoyal/Bright Data forever.
- **Slack / WhatsApp / SMS notifications.** Email + web app dashboard only, per the validated requirements.

---

## 9. Open questions for the user

- **Where will the company host?** Fly.io is the assumption; Hetzner / AWS / Railway alternatives change a few details (object storage, secrets, networking).
- **Clerk vs Supabase Auth?** Recommended Clerk; Supabase Auth is acceptable if we keep Supabase elsewhere. **Decision needed before #7 spec.**
- **Neon vs Supabase Postgres?** Recommended Neon (branching is a real productivity multiplier for a small team); Supabase if we want everything in one vendor. **Decision needed before #1 spec.**
- **Pricing model.** Per-seat? Per-watcher? Usage-based per crawl? Affects #7's billing schema.
- **Design-partner pipeline.** How many VCs (besides Omar) are committed for the beta? Determines whether #5's LinkedIn timeline can slip without losing customers.

---

## 10. Done criteria

This architecture document is "done" when:

- The user has reviewed each section and approved it (or requested edits and we've revised).
- Open questions in §9 are answered or explicitly deferred.
- The next per-milestone spec (sub-project #1: event log + Postgres migration) can be written referencing this document with no remaining ambiguity.

The next deliverable after sign-off is `docs/superpowers/specs/2026-05-XX-anytrace-v2-event-log-postgres-migration-design.md`.
