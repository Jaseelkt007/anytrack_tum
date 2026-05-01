# Build Plan V2 — SOTA Architecture

*Supersedes BUILD_PLAN.md. This is the master architecture document that the team will reference as the foundation for all detailed sub-plans (frontend deep-dive, ML deep-dive, scraping deep-dive, infrastructure deep-dive).*

---

## Strategic frame

**SOTA in framing, conceptual rigor, and architectural choices. Pragmatic in implementation.**

The judges, the VCs evaluating us, and any future investor reading this should see "these people thought about the problem like real engineers building a real product." The actual code can be 30% of what is described here — but it is built against the right scaffolding so it can grow into the full vision without being rewritten.

This is a deliberate choice. Going full SOTA on every component with a four-person team is not delivery; it is hubris. But shipping the *bones* of a SOTA architecture, with clean abstractions where complexity will eventually live, is what separates a hackathon project from a defensible product.

---

## The five architectural shifts from V1

### Shift 1 — Identity resolution as a first-class subsystem

**V1 assumption:** "Joe Hill on LinkedIn" and "@joehill on Twitter" can be matched by handle similarity or shared websites.

**Reality:** This is the single hardest data problem in the space. Specter has hundreds of engineers working on entity resolution and it is still their largest source of error. Without a dedicated identity layer, every downstream signal is noisy because we constantly fail to recognize the same human across platforms.

**V2 approach:** A dedicated identity resolution service that:

- Maintains canonical Person records, each with multiple platform identifiers and per-attribute confidence scores
- Uses a hybrid matching pipeline: deterministic (exact email, exact website, mutual high-confidence connections) → probabilistic (Fellegi-Sunter record linkage on bio similarity, location, work history) → LLM-arbitrated for ambiguous cases (Claude given two profiles with the question "is this the same person? answer with confidence + reasoning")
- Tracks provenance per fact: every attribute records which source said so, when, and with what confidence
- Surfaces conflicts to the UI: when two sources disagree, the user can resolve it (and the system learns)
- Supports backwards reconciliation: if we discover a new identity link, all historical edges from each platform get unified

This is the unsexy work that determines whether the system is a toy or a product.

### Shift 2 — Storage is a graph database, not a relational table

**V1:** SQLite with edge tables.

**V2:** Neo4j AuraDB Free tier (200K nodes, 400K relationships, more than enough for the demo and well beyond).

**Why this matters:**
- The user's mental model is a knowledge graph. The storage should match.
- Cypher queries express business logic clearly:

  ```cypher
  MATCH (target:Person)<-[r:CONNECTED_TO]-(w:WatchlistMember)
  WHERE r.first_seen_at > datetime() - duration({days: 14})
  WITH target, count(DISTINCT w) AS convergence, collect(w.name) AS members
  WHERE convergence >= 3
  RETURN target, convergence, members
  ORDER BY convergence DESC LIMIT 25
  ```

- Multi-hop traversals (the user's "intro path" feature) become trivial; in SQL they require recursive CTEs that are slow and fragile.
- The frontend's force-directed graph view consumes Neo4j queries directly — no impedance mismatch between DB and visualization.
- Native indexing on relationship types and timestamps makes the temporal queries fast.

### Shift 3 — Browser extension as universal capture, not LinkedIn workaround

**V1 framing:** "We can't scrape LinkedIn, so use an extension."

**V2 framing:** The extension *is* the data acquisition layer. It runs on Twitter, LinkedIn, GitHub, and Crunchbase. The user's natural browsing becomes a passive data stream. Every page they view contributes structured observations to the canonical graph.

**Why this is a much better architecture:**

- Zero scraping. Zero ToS violations. Zero proxy infrastructure to maintain.
- Three platforms unified through one client.
- The user's daily browsing *is* the system's freshness — they care about what they look at, so what they look at is intrinsically high-signal.
- Convergence highlights overlay back onto every platform: when the user hovers a name on Twitter, the extension shows "★4 watchlist matches" right there, with a click-through to the full evidence trail in the knowledge graph UI.
- Backend scrapers still exist for active monitoring of the watchlist (these are the targeted, predictable, high-signal entities) — but for the long tail, ambient capture from the user's browsing handles it.

**Hybrid capture model:**
- Active backend: scrapes ~200-500 watchlist members on a 4-6 hour cadence
- Passive extension: opportunistically captures everyone the user views, plus their connection graphs as visible to the user
- Both feed the same identity resolver and graph store

### Shift 4 — Intelligence layer reframed as temporal graph + Bayesian inference

**V1:** Z-score on edge velocity, hard threshold on convergence count.

**V2:** A proper statistical framing.

- **Marked temporal point processes** for edge arrivals. Each (watchlist_member → target) edge is an arrival in a marked Poisson process. Compute the conditional intensity λ(t | history) — the expected rate of new edges given the past. When recent intensity exceeds K × baseline, fire. This is rigorous and robust to varying baseline rates per founder.

- **Bayesian per-angel precision** with Beta(α, β) distributions. Each angel starts at a sensible prior (e.g., Beta(2, 8) — slightly skeptical default). After each historical case where they followed someone who later raised, update α += 1; for each "they followed someone who didn't raise," update β += 1. This gives credible intervals on each angel's predictive precision, not just point estimates. We can rank by posterior mean *or* by lower confidence bound (more conservative).

- **Survival model (Cox proportional hazards)** for the headline prediction: probability that a founder will raise in [t, t+90 days]. Time-varying covariates include each new investor edge. The hazard ratio for "3 thesis-aligned angels followed in last 14 days" *is* the magnitude of our signal, and it has a confidence interval.

- **Heterogeneous temporal graph embedding (framed as future direction).** We don't ship a full GNN in the hackathon. We do reference Temporal Graph Networks (Twitter, 2020), JODIE (KDD 2019), and TGAT in the architecture document, and structure the codebase so swapping in a learned embedding model later is a clean substitution. This makes the project look like it has a research roadmap, not just a one-shot demo.

### Shift 5 — Frontend is a knowledge graph product

**V1:** Ranked feed dashboard, graph viz as a footnote.

**V2:** The interactive knowledge graph IS the product. The ranked feed is just one saved query view over the graph.

This is your insight. Get it right and the demo is unforgettable. Get it wrong and you're building yet another sourcing dashboard.

**Frontend stack:**
- Next.js 14+ App Router
- `react-force-graph-2d` for the main graph viz (GPU-friendly, beautiful, easy)
- `Sigma.js + Graphology` as fallback if the graph grows beyond what react-force-graph handles cleanly
- Tailwind + shadcn/ui for dashboard chrome
- Apollo Client + GraphQL for queries (GraphQL is a natural fit for graph traversal)

See "Knowledge Graph UI Sub-plan" placeholder below for the full UX spec. The high-level views are:
1. **Graph Explorer** (centerpiece) — full-screen interactive force-directed graph with time slider, type filters, search, side panel
2. **Founder dossier** — drill into one person, see their incoming edges + 2-hop network
3. **Convergence inbox** — the daily "what fired today" ranked feed
4. **Watchlist manager** — add/remove investors, see per-angel precision scores
5. **Backtest theater** — scrub time backwards, watch historical convergences form

---

## The data model

### Node types

```
Person
  - canonical_id (UUID, internal)
  - display_name
  - bio_text
  - bio_embedding (384-dim vector)
  - role_tags (founder | investor | operator | researcher)
  - location_tags
  - sector_tags
  - last_seen_at
  - confidence_score

PlatformIdentity (attached to Person)
  - platform (twitter | linkedin | github | crunchbase | etc.)
  - handle / url / id
  - verified_via (deterministic | probabilistic | llm | manual)
  - confidence (0..1)
  - first_observed_at

Company
  - name
  - founded_at
  - registry_id (optional)
  - sector_tags

Repository (GitHub)
  - owner / name
  - language_tags
  - star_count_at_observation

WatchlistMembership (attached to Person and User)
  - user_id
  - archetype (mega_vc | micro_vc | scout | angel_operator | solo_gp)
  - sector_tags
  - notes
  - bayesian_alpha
  - bayesian_beta
```

### Edge types (all temporal — every edge has first_seen_at)

```
FOLLOWS_ON_TWITTER
CONNECTED_ON_LINKEDIN
STARRED_REPO
FOLLOWS_ON_GITHUB
REPLIED_TO
MENTIONED
WORKED_AT (Person → Company)
INVESTED_IN (Person → Company)
FOUNDED (Person → Company)
CO_FOUNDER_OF (Person ↔ Person)
CONVERGENCE_FIRED (Person → Person, with metadata about which members)
```

### Property graph node example (Cypher)

```cypher
CREATE (p:Person {
  canonical_id: 'p_47291',
  display_name: 'Joe Hill',
  bio_text: 'Building something new in fintech. ex-N26.',
  bio_embedding: [0.123, -0.045, ...],
  role_tags: ['founder_candidate'],
  sector_tags: ['fintech', 'ai'],
  confidence_score: 0.94
})

CREATE (linked:PlatformIdentity {
  platform: 'linkedin',
  url: 'linkedin.com/in/joehill',
  verified_via: 'deterministic',
  confidence: 1.0
})

CREATE (p)-[:HAS_IDENTITY]->(linked)
```

Lock this model in week one. Every later change costs.

---

## The intelligence layer in detail

### Convergence detection (refined)

Pseudocode for the core fire condition:

```python
def evaluate_convergence(target_id: str, user_id: str, now: datetime) -> Optional[ConvergenceEvent]:
    # Pull recent edges from watchlist members to this target
    recent_edges = neo4j.query("""
        MATCH (w:Person)-[r]->(target:Person {canonical_id: $target})
        WHERE r.first_seen_at > $cutoff
          AND (w)-[:WATCHED_BY]->(:User {id: $user})
        RETURN w, r, type(r) as edge_type
    """, target=target_id, user=user_id, cutoff=now - timedelta(days=14))
    
    if not recent_edges: return None
    
    distinct_members = {e['w'].canonical_id for e in recent_edges}
    if len(distinct_members) < THRESHOLD: return None
    
    # Compute the temporal intensity ratio
    historical_baseline = neo4j.query("""
        MATCH (w:Person)-[r]->(target:Person {canonical_id: $target})
        WHERE r.first_seen_at < $window_start
          AND (w)-[:WATCHED_BY]->(:User {id: $user})
        RETURN count(r) as count
    """, target=target_id, user=user_id, window_start=now - timedelta(days=14))
    
    days_observed = max(1, (now - earliest_edge_date(target_id)).days)
    baseline_per_day = historical_baseline / days_observed
    recent_per_day = len(recent_edges) / 14
    intensity_ratio = recent_per_day / max(baseline_per_day, 0.01)
    
    if intensity_ratio < INTENSITY_THRESHOLD: return None
    
    # Score it using all components
    score = compute_combined_score(
        distinct_member_count=len(distinct_members),
        intensity_ratio=intensity_ratio,
        thesis_match=compute_thesis_match(target_id, recent_edges),
        member_quality=compute_avg_bayesian_precision(distinct_members),
        archetype_weighting=apply_archetype_weights(recent_edges),
    )
    
    why_now = generate_why_now(target_id, recent_edges, score)
    
    return ConvergenceEvent(
        target_id=target_id,
        user_id=user_id,
        member_ids=list(distinct_members),
        score=score,
        intensity_ratio=intensity_ratio,
        why_now=why_now,
        fired_at=now,
    )
```

### Per-angel Bayesian precision

```python
def update_angel_precision(angel_id: str, founder_id: str, raised_within_90d: bool):
    angel = db.get_watchlist_member(angel_id)
    if raised_within_90d:
        angel.bayesian_alpha += 1
    else:
        angel.bayesian_beta += 1
    db.save(angel)

def angel_precision_estimate(angel_id: str) -> Tuple[float, Tuple[float, float]]:
    """Returns (posterior_mean, 95% credible interval)."""
    angel = db.get_watchlist_member(angel_id)
    a, b = angel.bayesian_alpha, angel.bayesian_beta
    mean = a / (a + b)
    lower = scipy.stats.beta.ppf(0.025, a, b)
    upper = scipy.stats.beta.ppf(0.975, a, b)
    return mean, (lower, upper)
```

This gives you the demo line: *"Naval Ravikant has a posterior mean precision of 0.71 with a 95% credible interval of [0.62, 0.79] over 47 observed cases."* That sentence sounds like a real product.

### Survival model (P3 owns this)

```python
from lifelines import CoxPHFitter

# Train on historical fundraise dataset
df = build_survival_dataset()  # rows: (founder, time_at_risk, raised_or_censored, covariates)
cph = CoxPHFitter()
cph.fit(df, duration_col='time_at_risk', event_col='raised_within_window',
        formula='angel_count_14d + thesis_match_score + intensity_ratio + ex_faang')

# Predict hazard for a new founder
hazard = cph.predict_partial_hazard(new_founder_features)
prob_raise_in_90d = 1 - cph.predict_survival_function(new_founder_features, times=[90])
```

Output: a calibrated probability that the founder raises in the next 90 days, with confidence intervals. This is what the headline "score" on a founder card means — not an arbitrary 0-100, but a real probability.

---

## Backtest methodology (research-grade)

P3 runs this with proper rigor:

1. **Build the dataset.** ~500 publicly announced funding rounds in the last 18 months, with announcement timestamps.

2. **Reconstruct historical state.** For each round at announcement time T, query our scrapers for the founder's social-graph state at time T-14, T-30, T-60, T-90 days. *Critical*: only use information that was observable at that time. No look-ahead bias.

3. **Match controls.** For each raised founder, find a matched control founder (similar bio embedding, same sector, same general era) who did *not* raise. Compare their pre-T graphs.

4. **Statistical tests.**
   - Difference-in-differences on edge counts: are the raised founders accumulating watchlist edges at significantly higher rates pre-raise?
   - Cox model on the full dataset with raised/not-raised as the event.
   - Bonferroni-corrected p-values when ranking individual angels by predictive precision.

5. **Out-of-time validation.** Train on rounds before date X, test on rounds after. Report calibration (Brier score) and discrimination (concordance index) separately.

6. **Document the limitations.**
   - Survivorship bias (only public raises)
   - Selection bias (the watchlist is non-random)
   - Twitter follow timestamps are sometimes unreliable
   - Identity resolution errors propagate through

The demo line that comes out of this:
> *"On a held-out validation set of 200 rounds, our top-tier convergence signal had a positive predictive value of 0.68 with 95% CI [0.61, 0.74]. Concordance index 0.79. We control for survivorship bias by including known stealth-fizzles where data is available."*

That sentence wins a technical-judge room.

---

## Frontend deep dive (the centerpiece)

### Graph Explorer view (the demo's wow moment)

Built on `react-force-graph-2d`. Nodes colored by type (founders red, investors blue, etc.). Edges colored by recency (gradient from gray for old to bright for new in the last 7 days).

**Interactions:**
- Click a node → side panel opens with full dossier
- Double-click → expand the node's 2-hop neighborhood
- Drag the time slider at the bottom → graph rewinds; old edges fade out, watch convergences form historically
- Search box → highlight matching nodes
- Filter chips for edge types: [Twitter] [LinkedIn] [GitHub] [All]
- Filter chips for archetypes: [Mega VCs] [Micro VCs] [Angels] [Scouts] [All]
- "Spotlight" mode: select 2 nodes, system shows shortest paths between them

**The Lovable demo moment:**
- Time slider at June 2023. Show: Anton Osika (Lovable founder) has minimal watchlist activity.
- Drag forward to August 2023. Watch: 3 Swedish VC nodes light up edges to him.
- Drag to October 2023. Watch: 2 more angels added. Convergence event fires (visible as a halo around his node).
- Drag to November 2023. Show: actual Lovable round announcement.
- Pause. Look at the room. Continue.

### Other views

See the planned "Frontend Sub-Plan" doc for full specs of:
- Founder dossier view
- Convergence inbox / ranked feed
- Watchlist manager (with per-angel Bayesian precision visualization)
- Backtest theater
- Settings + integrations

---

## Browser extension deep dive

### Manifest v3 + content scripts on three domains

```
extension/
├── manifest.json                    # MV3, host permissions for x.com, linkedin.com, github.com
├── content/
│   ├── twitter.js                   # captures profile data, follow lists, timeline interactions
│   ├── linkedin.js                  # captures connections, profile views, sales nav
│   └── github.js                    # captures stars, follows, profile info
├── shared/
│   ├── identity.js                  # extracts canonical identifiers from each platform
│   ├── overlay.css                  # the highlight badges
│   └── api-client.js                # talks to backend
├── background/
│   └── service-worker.js            # batches, dedupes, handles auth
└── popup/
    ├── popup.html                   # quick "today's convergence" view
    └── popup.js
```

### What it captures (passively)

- Twitter: when user views a profile, the following count, follower count, bio, recent tweets are visible — capture them. When user views their own following list, capture which accounts are on it.
- LinkedIn: when user views a profile, capture mutual connections (the "Mutual connections" pane), headline, current company, About section. When user views Sales Navigator results, capture the lead list metadata.
- GitHub: when user views a repo, capture star count, language tags. When user views a profile, capture follower/following counts.

### What it overlays (actively)

- Inline badge next to every visible name: **★4 watchlist matches** (clickable)
- Hover card with the 4 specific watchlist members and timestamps
- "Add to watchlist" one-click button visible on every profile

### The unified search (the killer feature)

Inside the extension popup: type a name → see *everything* the system knows about them across Twitter, LinkedIn, GitHub, in one unified card. This replaces 3 tabs of manual cross-referencing with one keystroke. *This single feature is potentially worth more than the rest of the product combined.*

See "Extension Sub-Plan" for full DOM selector specifications and platform-specific details.

---

## Toolchain — concrete choices

| Layer | Choice | Why |
|---|---|---|
| Knowledge graph DB | Neo4j AuraDB Free | Native graph queries, free tier sufficient |
| Vector store | Inline in Neo4j (vectors as node properties) or pgvector | Avoid extra infrastructure |
| Identity resolver | Custom Python service + Claude API arbitration | Hybrid deterministic + LLM is the best you can do at hackathon scale |
| Active scrapers | twscrape (Twitter), PyGithub (GitHub), ProxyCurl (LinkedIn fallback) | Standard choices |
| Browser extension | Manifest V3, vanilla JS or Preact for popup | Keep extension lightweight |
| Backend API | FastAPI + Strawberry GraphQL | GraphQL fits the graph data model |
| Embeddings | sentence-transformers all-MiniLM-L6-v2 | Free, local, fast |
| NLG (why-now) | Claude Haiku via Anthropic API | Cheap, fast, high quality |
| Survival modeling | lifelines (Python) | Standard, well-documented |
| Frontend | Next.js 14 + react-force-graph-2d + Tailwind + shadcn/ui | Modern, fast, beautiful |
| Hosting | Vercel (frontend) + Railway (backend + Neo4j) + Upstash (Redis) | Free tiers cover the demo |
| Email notif | Resend API | Three-line integration |

Total infrastructure cost for the hackathon: ~$30 (mostly Apify fallback for Twitter scraping if twscrape breaks).

---

## Phased implementation plan

Treating "no time constraint" loosely but with checkpoint discipline.

### Phase 0 — Foundations (Day 1)

All four people in one room, agreeing on:
- This document, the data model, and the Cypher schema — frozen
- API contracts: backend ↔ frontend (GraphQL schema), backend ↔ extension (REST endpoints)
- Repo monorepo layout: `/extension`, `/backend`, `/frontend`, `/scrapers`, `/intelligence`, `/backtest`, `/identity-resolver`
- API keys provisioned: GitHub PATs (×4), Anthropic, Apify, ProxyCurl, Resend, Neo4j AuraDB
- Watchlist seeded for demo: ~50 well-known angels in AI/fintech (Naval, Elad Gil, Sahil Lavingia, Sarah Guo, Daniel Gross, Soma Somasegar, etc.) — Omar's actual list ideal but a curated proxy works

**Deliverable**: this document marked "v1.0, agreed by team," in the repo, plus a working Neo4j connection from each person's laptop.

### Phase 1 — GitHub spine + Lovable backtest (Days 2-3)

**Goal**: a working end-to-end signal on the simplest data source, validating the Lovable case.

- P1: GitHub scraper populating `Person`, `Repository`, `STARRED_REPO` edges in Neo4j
- P3: Lovable case fully validated. Pull GPT Engineer's stargazer list with timestamps; identify which were investors before the Lovable raise. *This is the demo's anchor moment.*
- P2: Convergence detector running on GitHub-only data with simple rules (threshold ≥ 3, window 14 days)
- P4: Watchlist upload UI + minimal "convergence inbox" view + skeleton Graph Explorer rendering the GitHub subgraph

**Milestone**: judges could log in, upload a watchlist, and see GPT Engineer flagged with the specific star events shown chronologically.

### Phase 2 — Identity resolver + Twitter (Days 4-5)

- P1: twscrape running with snapshot + diff loop. Identity resolver matching Twitter handles to existing GitHub Persons via deterministic rules + LLM arbitration for ambiguous cases.
- P2: Convergence detector now spans GitHub + Twitter edges. Add Bayesian per-angel precision with priors.
- P3: Backtest expanded to include Legora and one more case.
- P4: Founder dossier view — clicking a node opens full panel. Time-slider on Graph Explorer.

**Milestone**: a real founder catches via Twitter convergence, with verifiable links visible in the UI.

### Phase 3 — Browser extension + LinkedIn (Days 6-7)

- P1: Browser extension MV3 scaffold; content scripts on Twitter and LinkedIn capturing data. ProxyCurl fallback for active LinkedIn enrichment.
- P2: Cox proportional hazards model trained on the backtest dataset. "Why now" NLG via Claude API.
- P3: Out-of-time validation report. Calibration plot. Brier score. Concordance index.
- P4: Extension overlay badges working live on LinkedIn. Convergence Theater (backtest replay) view.

**Milestone**: live demo flow ready end-to-end. Open LinkedIn, see badges. Open the dashboard, scrub time, see convergences form.

### Phase 4 — Polish + Hardening (Days 8+)

- Email notifications via Resend
- Edge case handling: missing data, identity conflicts, scraper failures
- Demo dry-runs (5+, with backup videos for each live segment)
- Pitch script
- One-pager handout for judges

---

## Risk register (revised)

| Risk | Severity | Mitigation |
|---|---|---|
| Twitter scraping breaks mid-demo | High | twscrape primary + Apify fallback, plus pre-recorded video segment |
| Identity resolver mismatches confuse the demo | High | Pre-validate the demo's specific founders (Lovable, Legora, etc.) by hand; freeze their IDs |
| Neo4j AuraDB free tier limit hit | Medium | Monitor node count; have local Memgraph/Neo4j as fallback |
| Cox model overfits to small N | Medium | Use cross-validation; report wide CIs honestly; don't oversell |
| Chrome extension blocked by LinkedIn anti-bot | Medium | Extension only reads DOM, no automation; should be safe but test early |
| Lovable backtest doesn't show clean convergence | Critical | Have 3-4 backup case studies validated by Day 4 |
| Identity resolution is too immature for the demo | Medium | Start with hand-curated mappings for the demo's 50 watchlist members; resolver runs only on new data |
| Schema changes mid-build | High | Lock Day 1, treat changes as team-blocking decisions |

---

## Sub-plans to develop next (placeholders)

This document is the master architecture. Each section below should expand into a dedicated detailed plan:

### Sub-plan 1 — Knowledge Graph UI Spec
- Full UX flows for all five views
- Component-level breakdown (which shadcn/ui components, custom components)
- Animation and interaction details
- GraphQL schema for the frontend
- Performance targets (60 FPS at 1000 visible nodes)

### Sub-plan 2 — Browser Extension Spec
- DOM selectors per platform with version pinning
- Auth flow (extension ↔ backend)
- Privacy model (what's captured, what's not)
- Conflict resolution UI when a captured fact contradicts what the backend has

### Sub-plan 3 — Identity Resolver Design
- Matching pipeline stages (deterministic → probabilistic → LLM)
- Confidence calibration
- LLM prompt design for the arbitrator
- Provenance and conflict storage model
- Backwards reconciliation algorithm

### Sub-plan 4 — Intelligence Layer Detail
- Full mathematical specification of the convergence detector
- Bayesian update rules and priors
- Cox model feature engineering
- Future GNN swap-in interface
- Calibration and evaluation metrics

### Sub-plan 5 — Backtest Methodology
- Dataset construction process
- Bias controls and assumptions documented
- Statistical test specifications
- Validation reports template

### Sub-plan 6 — Infrastructure & DevOps
- Deployment topology
- Secret management
- Monitoring and alerting
- Backup and recovery for Neo4j

Each of these will become its own `.md` file as the team starts building.

---

## What success looks like

A judge walks away from the demo with three thoughts in their head:

1. *"They modeled this as a knowledge graph problem and built a real graph database backing it."* (Architectural seriousness.)
2. *"Their Lovable case is a real, verifiable, retroactive proof point. Every signal they showed was clickable and led to a real public event."* (Data integrity.)
3. *"The extension overlay on LinkedIn is something I would install on Monday."* (Product-market fit signal.)

If those three thoughts land, you win.

The architecture exists to support those three thoughts. Don't let scope creep distract from them.
