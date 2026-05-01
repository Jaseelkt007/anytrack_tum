# `/backend` — FastAPI bridge between Neo4j and the Lovable frontend

Read-only API. Serves data shaped exactly like `/mnt/d/signal-convergence/src/data/types.ts` so the frontend deserializes without an adapter layer.

## Run

```bash
# from /mnt/d/tum_ai with .venv activated
uvicorn backend.app:app --reload --host 127.0.0.1 --port 8000
```

Confirm it's up:
```bash
curl -s http://127.0.0.1:8000/api/health | python -m json.tool
# {"ok": true, "neo4j": true, "generatedAt": "..."}
```

Open the auto-generated Swagger UI in a browser:
```
http://127.0.0.1:8000/docs
```

## Expose via ngrok (permanent domain)

```bash
ngrok http 8000 --domain=mispackaged-linn-prepoetic.ngrok-free.dev
```

Verify the tunnel works (the `ngrok-skip-browser-warning` header is mandatory or you'll get HTML back):

```bash
curl -H "ngrok-skip-browser-warning: true" \
     https://mispackaged-linn-prepoetic.ngrok-free.dev/api/health
```

The Lovable client always sends that header — see `LOVABLE_INTEGRATION_PROMPT.md`.

## Endpoints

| Path | Returns | Frontend page that uses it |
|---|---|---|
| `GET /api/health` | `{ ok, neo4j, generatedAt }` | LiveStatus pill |
| `GET /api/investors` | `Investor[]` (active watchlist) | Watchlist, Explore |
| `GET /api/founders` | `Founder[]` (≥2 inbound watcher follows) | Index, Explore |
| `GET /api/alerts` | `ConvergenceAlert[]` | Index dashboard |
| `GET /api/graph` | `{ nodes, edges, topPickFounderId, generatedAt }` | Explore |
| `GET /api/person/{id}` | `Investor \| Founder` | Side panel |
| `GET /api/founder/{id}` | `Founder & { alerts: ConvergenceAlert[] }` | FounderDetail |

## Configuration

Set in `.env` (project root). Defaults shown.

| Variable | Default | Notes |
|---|---|---|
| `NEO4J_URI` | required | e.g. `bolt://localhost:7687` |
| `NEO4J_USER` | required | usually `neo4j` |
| `NEO4J_PASSWORD` | required | local Docker default `phase1devpassword` |
| `CONVERGENCE_MIN_WATCHERS` | `2` | Minimum distinct active watchers to fire convergence |
| `GRAPH_EDGE_LIMIT` | `500` | Cap edges in `/api/graph` for React Flow performance |
| `FOUNDER_LIMIT` | `100` | Cap founder candidates returned |

## Mapping notes (Neo4j → frontend types)

- **`investor.tier`**: derived from `Person.investor_type`. `Angel` → `'angel'`. `VC - Big fund` → `'vc'`. `VC - Small fund` / `VC - Medium-Sized Fund` → `'microvc'`. M2.5 augmentations have `investor_type='angel_operator'` → `'angel'`.
- **`Investor.firm`**: not tracked in Phase 1 (CSV had no firm column for individuals).
- **`Founder` candidacy** in Phase 1: any Person who is the target of `FOLLOWS_ON_GITHUB` from ≥2 distinct active watchers AND is not themselves an active watcher. This is the live convergence signal — the M4 work formalizes it; this endpoint computes it on every request.
- **`Signal.occurredAt`**: GitHub does not expose follow timestamps, so for `FOLLOWS_ON_GITHUB` edges the timestamp is the pipeline poll time. For `STARRED_REPO` edges (not yet wired into convergence), the real historical `starred_at` is preserved.
- **`avatarColor`**: deterministic HSL string in the format the frontend expects (`"243 84% 60%"`), derived from `canonical_id` so colors are stable across requests.

## Limitations to call out before the demo

1. **Convergence is FOLLOWS_ON_GITHUB only.** STARRED_REPO convergence (via repo owner → founder) is the next step. M4 formalizes both.
2. **No persisted `ConvergenceEvent` rows yet.** Alerts are computed on each request. Not a problem at our data scale; if it becomes one, M4's persisted events fix it.
3. **`location`, `company`, `firm` are mostly null.** Phase 2 (Twitter / LinkedIn ingestion) populates these.
4. **`/api/founder/{id}` only exposes alerts that fire under the current MIN_WATCHERS threshold.** A founder who currently has 1 watcher won't appear; lower the threshold via env var if you want the long tail.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `HTTP 503` from any endpoint | Neo4j unreachable | `docker start tum-ai-neo4j` |
| `/api/health` returns `{ok:false, neo4j:false}` | Wrong NEO4J_URI/PASSWORD | Re-check `.env` |
| Empty `/api/investors` | Active watchlist not loaded | `python scripts/promote_active_watchlist.py` |
| Empty `/api/founders` and `/api/alerts` | Active watchers have no scraped follows | `python -m scrapers.pipeline` |
| ngrok returns HTML interstitial | Missing `ngrok-skip-browser-warning` header | Lovable client already sends it; verify your curl test sends it too |
| CORS errors in browser | Lovable preview origin blocked | `app.py` allows `*` already; only an issue if the user added a custom CORS proxy |
