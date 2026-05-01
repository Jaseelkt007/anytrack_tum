# Phase 1 Implementation Plan — First Working MVP

*Companion to `BUILD_PLAN_V2.md`. That document is the vision and architecture (mutation rate: low). This document is the contract the team executes against in Phase 1 (mutation rate: medium — edit freely, log changes at the bottom).*

**Status tags used in this doc:** `STABLE` = locked, change only with team sign-off. `DRAFT` = being built, edit freely. `EXPLORATORY` = rough hypothesis, may be thrown away.

---

## Phase 1 in one paragraph

Build a working end-to-end slice on **GitHub data only** that demonstrates the convergence signal, with the **Lovable / GPT Engineer case as the anchor backtest**. The user can upload a watchlist, the system flags founders whose repos a configured set of investors has starred or who they follow, and the UI shows the converging members with verifiable click-through links to GitHub. Twitter, LinkedIn, the browser extension, the identity resolver, the Cox model, and the Bayesian precision layer are all **explicitly out of Phase 1** — they belong to Phases 2 and 3.

**Why GitHub-first:** real API, real timestamps on stars (unlike Twitter follows), high signal density for technical founders, and the strongest demo case (Lovable with 30K stars on GPT Engineer) is GitHub-native.

---

## Definition of Done for Phase 1

A judge or teammate sitting at a laptop can:

1. Open the web app, log in, and see an empty watchlist.
2. Upload a CSV / paste a list of ~30 GitHub usernames (the seed watchlist).
3. Wait while the backend ingests their public activity (or hit "Run now" if data is pre-loaded).
4. See a **convergence inbox** showing flagged founder candidates ranked by how many watchlist members converged on them.
5. Click a flagged candidate (e.g. Anton Osika / GPT Engineer) and see:
   - The list of converging watchlist members with timestamps
   - A click-through link to each GitHub event (the actual star, the actual follow)
   - A small graph visualization of the immediate neighborhood
6. Drag a time slider back to mid-2023 and see the convergence form historically — the Lovable demo moment.

If those six steps work on real data, Phase 1 is done.

---

## Scope boundaries (what is and isn't in Phase 1)

| Area | In Phase 1 | Deferred to later phases |
|---|---|---|
| Data sources | GitHub (stars + follows) | Twitter, LinkedIn, Crunchbase |
| Identity resolution | Hand-curated mapping table for demo set (~50 people) | LLM-arbitrated probabilistic resolver |
| Convergence detection | Threshold rule (N≥2 members, 90-day window) | Temporal point process, intensity ratios |
| Scoring | Distinct-member count + recency weight | Bayesian precision, Cox model, thesis match |
| Profile timeseries | Static snapshot per person | Periodic re-snapshotting, `ProfileChange` events |
| Frontend | Watchlist upload, convergence inbox, founder dossier, simple graph view | Time slider polish, full Graph Explorer, extension overlays |
| Notifications | None (web app only) | Email via Resend |
| Browser extension | None | Phase 3 |
| Auth | Single hardcoded demo user | Real auth |
| Deployment | Local dev + one staging URL | Production-grade |

Resist scope creep. If something in the right column starts looking attractive, write it on a sticky note and revisit at the Phase 1 retro.

---

## Team & ownership

Four people, mapping to the roles in `BUILD_PLAN_V2.md`:

- **P1 — Data**: GitHub scrapers, ingestion pipeline into Neo4j
- **P2 — Intelligence**: convergence query, scoring, the `ConvergenceEvent` table
- **P3 — Backtest**: Lovable case validation, demo data preparation, identity mapping
- **P4 — Frontend + Backend API**: Next.js app, GraphQL/REST endpoints, graph visualization

These boundaries are `DRAFT` — adjust based on actual skill distribution, but each milestone below has a primary owner.

---

## Data model for Phase 1 (`STABLE` — do not change without team sign-off)

Subset of the full `BUILD_PLAN_V2` model. Lock these now; everything else can grow on top.

### Nodes

```
Person
  - canonical_id (UUID)         # internal, generated
  - display_name (string)
  - role_tags (list)            # ['investor', 'angel', 'founder_candidate', ...]
  - investor_type (string)      # 'Angel' | 'VC - Small fund' | 'VC - Medium-Sized Fund' | 'VC - Big fund' | null
  - country (string, nullable)
  - sector_tags (list)          # ['AI/ML', 'Fintech', ...] — populated from CSV for investors, empty for founder_candidates in Phase 1
  - stage_tags (list)           # ['Seed', 'Series A', ...]
  - bio_text (string, nullable)
  - confidence_score (float)    # default 1.0 in Phase 1
  - first_observed_at (datetime)
  - last_observed_at (datetime)

PlatformIdentity
  - platform (enum: 'github' | 'linkedin' | 'twitter')   # all three loaded in Phase 1 from CSV; only 'github' actively polled
  - handle (string)             # github username, twitter handle, or linkedin slug
  - profile_url (string)
  - verified_via (enum: 'manual' | 'deterministic' | 'csv_import')
  - confidence (float)

Repository
  - github_id (string)          # owner/name, unique
  - owner_handle (string)
  - name (string)
  - description (string)
  - language (string)
  - star_count_observed (int)   # at last fetch
  - created_at (datetime)
  - last_fetched_at (datetime)

WatchlistMembership
  - user_id (string)            # demo user only in Phase 1
  - person_id (UUID)
  - archetype (string)          # free-form tag in Phase 1
  - tier (enum: 'active' | 'reference')   # 'active' = used by convergence detector; 'reference' = recognized but not driving signals
  - notes (string)
  - added_at (datetime)
```

**Reference vs. active tier (`STABLE`)**: every `Person` with `role_tags` containing `'investor'` is loaded from the 1000-row CSV as `tier='reference'`. The curated demo subset (~30 names, mostly angels) is additionally tagged `tier='active'`. **Convergence detection only counts `tier='active'` members.** This is the design choice that keeps the signal/noise ratio sane — see "Why a curated active watchlist" section below.

### Edges (all temporal, all append-only)

```
HAS_IDENTITY        Person -> PlatformIdentity
STARRED_REPO        Person -> Repository    {first_seen_at, last_seen_at, removed_at?}
FOLLOWS_ON_GITHUB   Person -> Person        {first_seen_at, last_seen_at, removed_at?}
OWNS_REPO           Person -> Repository    {first_seen_at}
WATCHED_BY          Person -> User          {added_at}
```

### Derived / event tables

```
ConvergenceEvent
  - id (UUID)
  - target_person_id (UUID)
  - user_id (string)
  - fired_at (datetime)
  - window_start (datetime)
  - window_end (datetime)
  - distinct_member_count (int)
  - member_ids (list of UUID)
  - score (float)
  - evidence_json (json)        # links to underlying edges with timestamps
```

**Append-only rule (`STABLE`)**: never destructively update an edge. Re-observation updates `last_seen_at`. Disappearance sets `removed_at` and keeps the row.

---

## Why a curated active watchlist (and not the full 1000) (`STABLE` rationale)

We have `investor_list_1000.csv` (1000 investors with LinkedIn + Twitter, ~80 of them angels). The temptation is to use all of them as the active watchlist. Resist it. Here's the math:

If 1000 investors each follow ~500 unique people, that's 500K outbound edges. A target needs only 2 of those edges within 90 days to fire convergence — under that threshold, *thousands* of moderately-followed tech people would fire daily. The detector becomes meaningless.

This is exactly Specter's failure mode at a different layer: a wide-net definition of "engagement" produces high false-positive rates and the user stops trusting the tool. Omar told us in the interview: *"10 angels that I really like."* That's the design.

**Solution: tiered watchlist.**
- **Reference tier** (all 1000 from CSV): loaded as `Person` nodes. Used to (a) recognize people we encounter, (b) enrich context, (c) seed Phase 2 Twitter ingestion for free.
- **Active tier** (curated ~30, mostly angels with verified GitHub presence): drives the convergence detector. Threshold N=2 is meaningful here — two of *thirty hand-picked angels* converging is genuinely rare.

The CSV has zero GitHub handles, so building the active tier requires a one-time enrichment step (M2 below).

---

## The four ingestion flows for Phase 1 (`STABLE` boundaries)

Only flow A is fully active in Phase 1; the others are stubbed.

| Flow | Active in Phase 1? | What it does |
|---|---|---|
| **A. Watchlist polling** | YES | For each watchlist member, fetch their starred repos and following list, diff against last snapshot, write new edges |
| **B. Target profile polling** | Stub | Fetch profile snapshot for any person flagged as a `founder_candidate`. Phase 2 expands this. |
| **C. Extension passive capture** | Not built | Phase 3 |
| **D. Backtest reconstruction** | YES (one-shot) | For the Lovable case, reconstruct historical state by querying the GitHub Stargazers API with timestamps |

---

## Milestones

Nine milestones (M1, M2, M2.5, M3 through M8). Each has goal, deliverables, owner, acceptance criteria, dependencies. M2 (CSV reference load) is fast and unblocks M2.5; M3 can begin against a stub watchlist while M2.5 is finalized. M4 onwards has a clearer dependency chain.

### M1 — Foundations (Day 1 morning)

**Goal**: every team member can read/write to the same Neo4j instance, the schema is loaded, the repo is structured, and secrets are in place.

**Deliverables**:
- Monorepo created with subdirectories: `/backend`, `/frontend`, `/scrapers`, `/intelligence`, `/backtest`, `/data` (for seed CSVs and demo fixtures), `/docs`
- Neo4j AuraDB Free instance provisioned, connection string in `.env.example`
- Each person has a working local connection (test query returns a node)
- Cypher schema constraints loaded (uniqueness on `Person.canonical_id`, `PlatformIdentity.handle+platform`, `Repository.github_id`)
- API key inventory file: GitHub PATs (×4 — one per team member to multiply rate limit), Anthropic, Vercel
- `.env.example`, `.gitignore`, `README.md` with quickstart for new team member

**Owner**: P1 leads, everyone present

**Acceptance criteria**:
- All four laptops run `python scripts/healthcheck.py` and see "Neo4j OK, GitHub PAT OK"
- Schema constraints visible via `SHOW CONSTRAINTS` in Neo4j browser

**Dependencies**: none

**Status**: `DRAFT`

---

### M2 — Investor reference load (Day 1 afternoon)

**Goal**: all 1000 rows of `investor_list_1000.csv` are loaded into Neo4j as reference `Person` records with LinkedIn + Twitter identities. This becomes the lookup substrate for everything downstream.

**Deliverables**:
- `scripts/clean_investor_csv.py`: reads `investor_list_1000.csv`, fixes the malformed quote-escaped rows (the four with `PhD"`, `LP"`, `LLC"`, `Inc."` artifacts), normalizes country/sector/stage strings into clean lists, writes `data/investors_clean.csv`.
- `scripts/load_investor_reference.py`: reads the cleaned CSV and for each row creates:
  - `Person` node with `display_name`, `investor_type`, `country`, `sector_tags`, `stage_tags`, `role_tags=['investor']` (plus `'angel'` if `investor_type=='Angel'`)
  - `PlatformIdentity(platform='linkedin')` if a LinkedIn URL is present, with `verified_via='csv_import'`
  - `PlatformIdentity(platform='twitter')` if a Twitter handle is present (extract handle from URL like `https://twitter.com/foo` → `foo`), with `verified_via='csv_import'`
  - `WatchlistMembership` row with `tier='reference'` for the demo user
- Idempotent: re-running the script does not create duplicates (uses the uniqueness constraints from M1).
- Sanity query results documented in `data/README.md`: counts by `investor_type`, by country, by sector.

**Owner**: P3

**Acceptance criteria** (corrected against the real CSV — 2026-05-01):
- `MATCH (p:Person {investor_type:'Angel'}) RETURN count(p)` returns 79
- `MATCH (p:Person)-[:HAS_IDENTITY]->(i:PlatformIdentity {platform:'linkedin'}) RETURN count(p)` returns 133
- `MATCH (p:Person)-[:HAS_IDENTITY]->(i:PlatformIdentity {platform:'twitter'}) RETURN count(p)` returns 129
- `MATCH (p:Person {investor_type:'Angel'})-[:HAS_IDENTITY]->(:PlatformIdentity {platform:'twitter'}) RETURN count(p)` returns 76 (96% of angels have Twitter — they are the Phase 2 watchlist seed)
- `MATCH (p:Person {investor_type:'VC - Big fund'})-[:HAS_IDENTITY]->(i:PlatformIdentity) RETURN count(p)` returns 0 (big/medium VCs in this CSV are name-only)
- `MATCH (p:Person)-[r:WATCHED_BY {tier:'reference'}]->(:User {id:'demo'}) RETURN count(p)` returns 1000
- Re-running the loader produces no duplicates.

**Dependencies**: M1

**Status**: `DRAFT` for the cleanup script; the load is `STABLE` once it ships — downstream code depends on these node properties.

---

### M2.5 — Active watchlist curation (Day 1 evening / Day 2 morning)

**Goal**: a curated subset of ~30 reference investors has GitHub handles attached and is promoted to `tier='active'`. The convergence detector will run against this set.

**Deliverables**:
- `data/active_watchlist.csv` with columns: `display_name, source_csv_row_or_external, github_handle, rationale`. ~30 rows.
  - Start from the 79 angels in the reference set; pick those with publicly findable GitHub handles (look at their LinkedIn websites, personal sites, or Twitter bios).
  - Augment with ~10 known technical investors who *should* be in the reference set but might not be: Naval Ravikant, Elad Gil, Patrick Collison, Sahil Lavingia, Daniel Gross, Sarah Guo, Soma Somasegar, Lachy Groom, Guillermo Rauch, Soumith Chintala. If any are missing from the 1000-row CSV, add them as new `Person` nodes too.
  - Bias the selection toward European / Swedish presence to make the Lovable case plausible (Hampus Jakobsson, Sophia Bendz, Pär-Jörgen Pärson if they have GitHub activity, etc.)
- `scripts/promote_active_watchlist.py`:
  - For each row, locates the `Person` (by display_name match against the reference set; if not found, creates a new one)
  - Adds a `PlatformIdentity(platform='github')` with `verified_via='manual'`
  - Updates the existing `WatchlistMembership` row to `tier='active'` (or creates one)
- A "GitHub activity health check" query: for each active watchlist member, count their stars and follows. Any member with <5 stars and <5 follows in the last 12 months gets flagged in the output and considered for replacement.
- `data/identity_overrides.csv`: hand-curated identity links for the demo target founders (Anton Osika → `github.com/AntonOsika`, plus 2-3 backup founders) so the demo doesn't trip on resolver issues.

**Owner**: P3 (curation), P1 (script)

**Acceptance criteria**:
- `MATCH (p:Person)-[:WATCHED_BY]->(:User {id:'demo'}) WHERE EXISTS { (p)-[:HAS_IDENTITY]->(:PlatformIdentity {platform:'github'}) } RETURN count(p)` returns ≥25
- The active watchlist health check shows ≥80% of members have meaningful GitHub activity in the last 12 months
- Anton Osika's `Person` exists with `github_handle='AntonOsika'`, `role_tags` contains `'founder_candidate'`, and is **not** in the active watchlist

**Dependencies**: M2

**Status**: `DRAFT`. Composition will change after M5 / M7 reveal which members actually fire on the demo cases.

---

### M3 — GitHub ingestion pipeline (Days 1-2)

**Goal**: a job that, given a watchlist, fetches each member's stars and follows from GitHub and writes edges to Neo4j with timestamps.

**Deliverables**:
- `scrapers/github_client.py`: thin wrapper around PyGithub or direct REST. Handles auth (token rotation across 4 PATs), rate-limit backoff, pagination.
- `scrapers/jobs/fetch_starred_repos.py`: for one user handle, returns list of `(repo_full_name, starred_at)`. Uses the Stargazers API which *does* expose timestamps with the `Accept: application/vnd.github.star+json` header.
- `scrapers/jobs/fetch_following.py`: for one user handle, returns list of followed handles. (No timestamps available — we approximate with poll-time.)
- `scrapers/pipeline.py`: orchestrates a full watchlist sweep. For each member, fetches stars + follows, upserts `Repository` + `Person` nodes, creates/updates `STARRED_REPO` and `FOLLOWS_ON_GITHUB` edges with `first_seen_at`/`last_seen_at`.
- Append-only behavior implemented and tested: re-running the pipeline does not duplicate edges; new edges get `first_seen_at = now()`; re-observed edges get `last_seen_at = now()`.
- A small CLI: `python -m scrapers.pipeline --watchlist demo --limit 5` for fast iteration.
- The pipeline filters to `tier='active'` watchlist members only — we do *not* poll all 1000 reference investors. Reference tier exists for lookup, not for active polling.

**Owner**: P1

**Acceptance criteria**:
- Running the pipeline on a 5-member subset populates Neo4j with hundreds of `Repository` nodes and thousands of `STARRED_REPO` edges
- Running it twice in a row produces no duplicate edges; `last_seen_at` updates on the second run
- The Stargazers API timestamp ends up in `STARRED_REPO.first_seen_at` for *historical* stars (this is what makes the Lovable backtest work)

**Dependencies**: M1, M2

**Status**: `DRAFT`. The Stargazers timestamp behavior needs to be verified day one — if it's not retrievable historically for users who already starred, the backtest strategy changes.

---

### M4 — Convergence query + scoring (Day 2)

**Goal**: a query that, given a user, returns ranked founder candidates whose recent inbound edges from watchlist members exceed a threshold.

**Deliverables**:
- `intelligence/convergence.py`:
  - `find_convergences(user_id, window_days=90, min_members=2) -> list[ConvergenceEvent]`
  - Cypher query that finds targets with ≥`min_members` distinct watchlist edges in the window
  - Score = `distinct_member_count + recency_bonus + member_quality_placeholder` (the placeholder is a constant in Phase 1; Bayesian comes in Phase 2)
- Writes `ConvergenceEvent` rows to Neo4j with full evidence JSON (member ids, edge ids, timestamps)
- A test fixture: synthetic mini-graph with 5 watchlist members and 3 targets, two of which should fire and one shouldn't
- CLI: `python -m intelligence.convergence --user demo --window 90`

**Owner**: P2

**Acceptance criteria**:
- On the synthetic fixture, the 2 expected targets fire and the 1 expected non-target doesn't
- On the real loaded data after M3 + M5, at least 5 candidate `ConvergenceEvent` rows exist
- Each `ConvergenceEvent.evidence_json` contains direct GitHub URLs that load in a browser

**Dependencies**: M3 (needs real data to be meaningful, but can be developed against the fixture in parallel)

**Status**: `DRAFT`. Threshold and window are the most-likely-to-change values in the entire system.

---

### M5 — Lovable backtest data (Day 2)

**Goal**: the GPT Engineer / Lovable case is fully loaded into Neo4j with historical fidelity, so the convergence query fires on it as if we had been running in 2023.

**Deliverables**:
- `backtest/cases/lovable.py`:
  - Pulls the full Stargazers list of `AntonOsika/gpt-engineer` (the GitHub repo) with `starred_at` timestamps
  - Cross-references against the watchlist + a broader "high-signal investor" supplementary list
  - For any matches, ensures the `Person` exists and creates `STARRED_REPO` edges with the *historical* `first_seen_at` (not the current poll time)
  - Pulls Anton Osika's GitHub follower list and similarly back-dates `FOLLOWS_ON_GITHUB` edges where timestamps are inferable
- `backtest/cases/README.md` documenting the data sources and any approximations
- A second case stubbed out (e.g. another YC founder with public GitHub activity) for redundancy

**Owner**: P3

**Acceptance criteria**:
- `MATCH (p:Person {github_handle: 'AntonOsika'})<-[r]-(w:Person)-[:WATCHED_BY]->(:User {id: 'demo'}) RETURN w, r ORDER BY r.first_seen_at` returns ≥3 watchlist members with edges dated before November 2023
- Running the convergence query with `window_days=90` and `as_of_date='2023-10-15'` flags Anton

**Dependencies**: M3 (uses the same scraper), M4 (needs the convergence query to validate)

**Status**: `DRAFT`. If the Lovable backtest doesn't produce a clean signal even with manual curation, P3 swaps to the backup case immediately.

---

### M6 — Backend API (Day 2-3)

**Goal**: the frontend has a stable API to call. Read-only is enough for Phase 1.

**Deliverables**:
- FastAPI app in `/backend` with endpoints (Phase 1 sticks to REST for simplicity; GraphQL is `EXPLORATORY` and can be added later):
  - `GET /api/watchlist` — list current watchlist with archetype + notes
  - `POST /api/watchlist/upload` — accept CSV upload, replace or merge based on flag
  - `GET /api/convergences?user=demo&since=...` — list `ConvergenceEvent` rows ranked by score
  - `GET /api/person/{canonical_id}` — full dossier: identities, recent inbound edges, repos owned
  - `GET /api/graph/neighborhood?person={id}&hops=2` — returns nodes + edges JSON for the graph viz
- Pydantic response models — these are the contract the frontend depends on; once frozen, treat as `STABLE` for the rest of Phase 1
- Dockerfile + a single `make dev` that boots backend + connects to Neo4j

**Owner**: P4

**Acceptance criteria**:
- All endpoints return valid JSON for the demo dataset
- `/api/convergences` returns at least Anton Osika after M5 is loaded
- `/api/graph/neighborhood?person=<anton_id>` returns ~20 nodes including the converging watchlist members

**Dependencies**: M2 (watchlist), M4 (convergence query), M5 (real demo data)

**Status**: `DRAFT` for implementation; response shapes graduate to `STABLE` once the frontend starts depending on them.

---

### M7 — Frontend MVP (Days 2-3)

**Goal**: the Definition of Done flow works end-to-end in a browser.

**Deliverables**:
- Next.js 14+ App Router project in `/frontend`, deployed to Vercel
- Pages:
  - `/watchlist` — table view of current watchlist, upload CSV button, archetype filter
  - `/inbox` — ranked list of `ConvergenceEvent`s; each row shows target name, member count, score, fired_at, click to dossier
  - `/person/[id]` — founder dossier: bio, identities, list of inbound watchlist edges (each with click-through to the GitHub URL), 2-hop graph viz
  - `/graph` — full-window graph using `react-force-graph-2d`, basic time slider on the bottom (day-granularity is fine for Phase 1)
- Tailwind + shadcn/ui for chrome
- Loading states, empty states, basic error states
- One demo user hardcoded; no auth flow

**Owner**: P4 (with M8 below this is the heaviest lift — pull P3 in for component work after M5 is done)

**Acceptance criteria**:
- All six steps in "Definition of Done for Phase 1" pass
- Lighthouse perf is not embarrassing (>70 on the dossier page)
- Each click-through link on the dossier opens the correct GitHub event

**Dependencies**: M6

**Status**: `DRAFT`

---

### M8 — Demo dry-run + Phase 1 retro (Day 3 evening)

**Goal**: the team rehearses the demo end-to-end at least twice on real data, captures issues, and writes the Phase 2 brief.

**Deliverables**:
- A 5-minute demo script following the six-step DoD flow, with explicit transitions and the Lovable moment marked
- One full dry-run with the full team watching
- A second dry-run with bug fixes applied
- A `PHASE_2_BRIEF.md` capturing: what worked, what was hand-wavy, where the Twitter/LinkedIn additions slot in, what the identity resolver actually needs to do
- Decision-log update at the bottom of *this* doc

**Owner**: whole team

**Acceptance criteria**:
- The demo runs to completion without anyone touching the keyboard mid-flow
- Every signal shown in the demo has a clickable evidence link
- The team agrees on the top 3 fragility points and what to harden in Phase 2

**Dependencies**: all prior

**Status**: `DRAFT`

---

## Concrete API contracts (frontend ↔ backend)

`STABLE` once M6 ships. The frontend should not change shape without coordinating with backend.

```jsonc
// GET /api/convergences?user=demo
{
  "events": [
    {
      "id": "evt_01HX...",
      "target": {
        "canonical_id": "p_anton",
        "display_name": "Anton Osika",
        "github_handle": "AntonOsika"
      },
      "score": 4.2,
      "distinct_member_count": 4,
      "fired_at": "2023-10-15T00:00:00Z",
      "window_start": "2023-07-17T00:00:00Z",
      "window_end": "2023-10-15T00:00:00Z",
      "members": [
        {
          "canonical_id": "p_naval",
          "display_name": "Naval Ravikant",
          "edge_type": "STARRED_REPO",
          "first_seen_at": "2023-08-04T12:00:00Z",
          "evidence_url": "https://github.com/AntonOsika/gpt-engineer/stargazers"
        }
        // ...
      ]
    }
  ]
}
```

```jsonc
// GET /api/person/{id}
{
  "canonical_id": "p_anton",
  "display_name": "Anton Osika",
  "bio_text": "Building Lovable. Previously GPT Engineer, Sana.",
  "identities": [
    { "platform": "github", "handle": "AntonOsika", "url": "https://github.com/AntonOsika" }
  ],
  "owned_repos": [
    { "github_id": "AntonOsika/gpt-engineer", "stars": 30421, "language": "Python" }
  ],
  "inbound_edges": [
    /* same shape as members above */
  ]
}
```

```jsonc
// GET /api/graph/neighborhood?person=<id>&hops=2
{
  "nodes": [
    { "id": "p_anton", "label": "Anton Osika", "type": "founder_candidate" },
    { "id": "p_naval", "label": "Naval Ravikant", "type": "investor" }
    // ...
  ],
  "edges": [
    { "source": "p_naval", "target": "p_anton", "type": "STARRED_REPO", "first_seen_at": "2023-08-04T12:00:00Z" }
    // ...
  ]
}
```

---

## Risks specific to Phase 1

| Risk | Severity | Mitigation |
|---|---|---|
| Stargazers timestamp API is missing/stale for some users | Critical (kills Lovable backtest) | Verify on day one with a sample call; if broken, switch demo case to one where we still have timestamps (any repo's stars are fetchable with `vnd.github.star+json`) |
| GitHub rate limits exhausted mid-demo | High | Token rotation across 4 PATs (5K req/hr each = 20K/hr). Pre-load all demo data into Neo4j by Day 3 morning; demo reads from Neo4j only |
| Active watchlist members don't actually have signal density on GitHub | High | M2.5 health-check query flags low-activity members; swap before M3 finishes. Target: ≥80% of active members have ≥10 stars or ≥5 follows in the last 12 months. |
| Few of the 1000 reference investors are findable on GitHub | Medium | Expected — GitHub-active investors are a small subset. M2.5 augments with the ~10 known technical investors not necessarily in the CSV (Naval, Patrick Collison, Elad Gil, etc.). |
| Twitter handle extraction from CSV URLs is brittle (some are bare handles, some are URLs, some have query strings) | Low | M2 cleanup script normalizes: strip `https://twitter.com/` and `https://x.com/`, strip trailing slashes/query strings, lowercase |
| Convergence threshold needs to be N=1 to fire on Lovable | Medium | Tune empirically in M5 with knowledge that N=2 is the lowest defensible number. If we need N=1 we change the framing to "watchlist activity" rather than "convergence" |
| Frontend graph viz lags with too many nodes | Low | Limit `/api/graph/neighborhood` to 200 nodes; clip aggressively |
| Identity is wrong on a demo node | Medium | M2 ships hand-curated identity overrides for demo entities; freeze them by Day 2 evening |

---

## What "out of scope" looks like for Phase 1 (pre-committed)

If any of these come up mid-build, defer:

- Twitter scraping, LinkedIn scraping, browser extension, ProxyCurl integration
- Bayesian per-angel precision (just count distinct members)
- Cox proportional hazards model (no headline probability — just a rank)
- "Why now" NLG (no Claude API calls in Phase 1)
- Email notifications
- User auth / multi-user / sign-up
- Admin panel for editing the watchlist via UI (CSV upload is enough)
- Confidence intervals or uncertainty quantification anywhere
- `ProfileSnapshot` and `ProfileChange` tables (Phase 2 — when we add LinkedIn)
- Vector embeddings on bios

---

## Phase 1 → Phase 2 hand-off (preview)

So the team knows what's coming and builds toward it without painting into a corner:

- Phase 2 adds Twitter via `twscrape`, requires identity-resolution to fuse Twitter handles to existing GitHub `Person`s. Plan for a `PlatformIdentity` insert flow that runs through a resolver function from day one — even if the function is "manual lookup" in Phase 1, the *call site* should already be in place.
- Phase 2 introduces `ProfileSnapshot` / `ProfileChange`. The `Person` node should not have any attribute that would be better stored as a snapshot. Bio text is the marginal call — keep it on `Person` for Phase 1, plan to migrate to snapshots in Phase 2.
- Phase 2 swaps the threshold rule for Bayesian + intensity scoring. The convergence module's interface (`find_convergences(user_id, window_days, min_members) -> list[ConvergenceEvent]`) should not change; only the internals.
- Phase 3 adds the browser extension, which talks to the same backend API. The API should not assume request origin; treat extension and frontend as equal clients.

---

## Decision log

A new entry every time something `STABLE` changes, or a `DRAFT` decision is made that the team should remember. Date-stamped, one line each.

- **2026-05-01**: Phase 1 plan drafted. Locked: data model node/edge shapes, append-only rule, four-flow ingestion taxonomy. Threshold values N=2, window=90d are `DRAFT`.
- **2026-05-01 (M3)**: M3 GitHub ingestion pipeline shipped + validated against 21 of 28 active watchlist members (the run was stopped early on operator request — 7 names later in the alphabet not yet ingested; trivial to backfill). Result: 14,462 Repository nodes, 15,962 STARRED_REPO edges (13,916 dated before 2024 — historical depth confirmed), 3,271 FOLLOWS_ON_GITHUB edges, 2,853 Persons with github identity. Idempotency verified across 3 successive partial-run cycles. Stargazers `vnd.github.star+json` Accept header returns historical timestamps cleanly (depth back to 2010-04-10 GitHub backfill date for Karpathy's earliest stars). The opportunistic OWNS_REPO edges grow once on the second run as more repo owners' Person nodes come into existence — this is by design, stable from run 3 onward. **All 7 M3 acceptance checks PASS.**
- **2026-05-01 (M2.5)**: Active watchlist curated. 28 members, all hand-verified via GitHub API name-match before promotion. 1 was already in the M2 reference set (Max Stoiber); 27 are augmentations. **Tier exclusivity**: a person has one `WATCHED_BY` edge whose `tier` flips between `'reference'` and `'active'`, so the M2 reference count dropped from 1000 → 999 when Max was promoted. This is correct; total watched persons = 1000 (M2 reference) + 27 (augmentations) = 1027, of which 28 are tier='active'. Convergence detector queries `tier='active'` only.
- **2026-05-01**: CSV profiled with proper quote-aware parsing. **Platform-identifier coverage is MUCH lower than the earlier awk-based estimate**: 133 LinkedIn (13%), 129 Twitter (13%) — not 889/635. Breakdown: all 79 angels have ≥1 platform identifier (76 Twitter, 35 LinkedIn = 96%/44%); only 109/828 small VCs have any (13%); **0 of 62 big VCs and 0 of 31 medium VCs have any platform identifier**. Implications: (a) the angels are the only category usable as Phase 2 active watchlist seeds, (b) big/medium VCs are recognition-only data (still load them, they widen the "we know who this is" surface), (c) M2 acceptance criteria corrected accordingly. Strategic note: this validates Omar's "angels first" priority — the data shape mirrors the user's mental model.
- **2026-05-01**: Adopted tiered watchlist model after `investor_list_1000.csv` arrived. Locked: all 1000 load as `tier='reference'`; convergence detector runs only against curated `tier='active'` (~30). Rationale: 1000-member watchlist makes N≥2 convergence trivially true and floods the signal. Added M2 (reference load) and M2.5 (active curation). Schema additions: `Person.investor_type`, `Person.country`, `Person.sector_tags`, `Person.stage_tags`, `WatchlistMembership.tier`, `PlatformIdentity.platform` extended to `'linkedin'` and `'twitter'` (loaded passively from CSV but not actively polled in Phase 1).

---

## How to use this doc

- If you're about to start a milestone, re-read its acceptance criteria first.
- If you find yourself doing work that's not in this doc, stop and ask: is it actually deferred to Phase 2, or is the doc wrong? Update the doc either way.
- If a `STABLE` decision needs to change, message the team before editing.
- At the end of each work session, add to the decision log if anything material was decided.
