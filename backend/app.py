"""FastAPI backend for the signal-convergence frontend.

Run locally:
    uvicorn backend.app:app --reload --host 0.0.0.0 --port 8000

Endpoints (response shapes match /mnt/d/signal-convergence/src/data/types.ts):
    GET /api/health             -> { ok, postgres, generatedAt }
    GET /api/investors          -> Investor[]
    GET /api/founders           -> Founder[]
    GET /api/alerts             -> ConvergenceAlert[]
    GET /api/graph              -> { nodes, edges, topPickFounderId, generatedAt }
    GET /api/person/{id}        -> Investor | Founder
    GET /api/founder/{id}       -> Founder & { alerts: ConvergenceAlert[] }

v0.2: read/write paths now talk to Postgres via SQLAlchemy 2.0 async. Pipeline,
notifier, and dossier endpoints temporarily return 501 until sub-projects
#11 (scraper write path) and #12 (JSONL persistence) re-enable them.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastapi import Body, Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.mappers import (
    avatar_color_for,
    initials_of,
    map_alert,
    map_founder,
    map_investor,
    map_signal,
)
from db.engine import dispose_engine, get_engine, get_session
from intelligence import convergence as conv
from intelligence.rule import (
    KNOWN_SIGNAL_TYPES,
    KNOWN_SORT_KEYS,
    AlertRule,
    get_rule,
    save_rule,
    update_rule_partial,
)
from queries import (
    convergence as q_convergence,
    founders as q_founders,
    graph as q_graph,
    investors as q_investors,
    persons as q_persons,
)

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

DEMO_ORG_ID = "demo"
DEMO_USER_ID = "demo"
MIN_WATCHERS = int(os.getenv("CONVERGENCE_MIN_WATCHERS", "2"))
GRAPH_EDGE_LIMIT = int(os.getenv("GRAPH_EDGE_LIMIT", "500"))
FOUNDER_LIMIT = int(os.getenv("FOUNDER_LIMIT", "100"))


# --- Lifecycle --------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Eagerly initialise the engine so connection problems surface at startup.
    get_engine()

    # APScheduler stays disabled during the v2 migration — scheduler still
    # references Neo4j in its scrape stages. Sub-project #2 (Procrastinate)
    # replaces it; until then, run pipeline jobs manually.
    if os.environ.get("PIPELINE_SCHEDULER_ENABLED", "").lower() in ("1", "true", "yes"):
        from backend import scheduler as _sched
        _sched.start_scheduler()

    yield

    try:
        from backend import scheduler as _sched
        _sched.stop_scheduler()
    except Exception:
        pass
    await dispose_engine()


app = FastAPI(
    title="signal-convergence backend",
    version="0.2.0",
    lifespan=lifespan,
)


# --- CORS -------------------------------------------------------------------

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_origin_regex=r"https://.*\.(lovableproject\.com|lovable\.app)",
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
    max_age=600,
)


# --- Helpers ----------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _dedupe_by_id(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for item in items:
        key = item.get("id")
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


# --- Endpoints --------------------------------------------------------------

@app.get("/api/health")
async def health(session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    pg_ok = False
    try:
        result = await session.execute(text("SELECT 1"))
        pg_ok = result.scalar() == 1
    except Exception:
        pg_ok = False

    return {
        "ok":          pg_ok,
        "postgres":    pg_ok,
        "neo4j":       False,  # legacy field — frontend may still read it
        "generatedAt": _now_iso(),
    }


@app.get("/api/investors")
async def list_investors(session: AsyncSession = Depends(get_session)) -> list[dict[str, Any]]:
    rows = await q_investors.list_investors(session, user_id=DEMO_USER_ID)
    return _dedupe_by_id([map_investor(r) for r in rows])


@app.get("/api/founders")
async def list_founders(session: AsyncSession = Depends(get_session)) -> list[dict[str, Any]]:
    rows = await q_founders.list_founder_candidates(
        session,
        org_id=DEMO_ORG_ID,
        user_id=DEMO_USER_ID,
        min_watchers=MIN_WATCHERS,
        limit=FOUNDER_LIMIT,
    )
    return _dedupe_by_id([map_founder(r) for r in rows])


@app.get("/api/alerts")
async def list_alerts(session: AsyncSession = Depends(get_session)) -> list[dict[str, Any]]:
    rule = get_rule(DEMO_USER_ID)
    rows = await q_convergence.list_convergence_signals(
        session,
        org_id=DEMO_ORG_ID,
        user_id=DEMO_USER_ID,
        min_watchers=rule.min_distinct_watchers,
        limit=rule.limit,
    )
    out: list[dict[str, Any]] = []
    for i, r in enumerate(rows, start=1):
        # mappers expect *_json fields as JSON strings; we serialise here so the
        # mapper's existing parsing path keeps working unchanged.
        import json as _json
        pre = dict(r)
        for k in ("evidence_json", "score_breakdown_json", "signal_type_counts_json"):
            v = pre.get(k)
            if v is not None and not isinstance(v, str):
                pre[k] = _json.dumps(v, default=str)
        out.append(map_alert(pre, window_days=rule.window_days, rank=i))
    return out


@app.get("/api/alert-rule")
async def get_alert_rule() -> dict[str, Any]:
    rule = get_rule(DEMO_USER_ID)
    return {
        "userId": DEMO_USER_ID,
        "rule": rule.to_dict(),
        "allowed": {
            "signal_types": list(KNOWN_SIGNAL_TYPES),
            "sort_by":      list(KNOWN_SORT_KEYS),
        },
    }


@app.put("/api/alert-rule")
async def update_alert_rule(patch: dict[str, Any] = Body(...)) -> dict[str, Any]:
    try:
        new = update_rule_partial(DEMO_USER_ID, patch)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"userId": DEMO_USER_ID, "rule": new.to_dict()}


@app.post("/api/alerts/recompute")
async def recompute_alerts(session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    rule = get_rule(DEMO_USER_ID)
    events = await conv.find_convergences(
        session, user_id=DEMO_USER_ID, org_id=DEMO_ORG_ID, rule=rule,
    )
    end_iso = events[0].window_end if events else _now_iso()
    await conv.persist_events(
        session, events, user_id=DEMO_USER_ID, org_id=DEMO_ORG_ID,
        window_end_iso=end_iso,
    )
    return {
        "userId":      DEMO_USER_ID,
        "rule":        rule.to_dict(),
        "fired":       len(events),
        "topTargets":  [
            {"name": e.target_name, "score": e.score, "n": e.distinct_member_count}
            for e in events[:5]
        ],
        "generatedAt": _now_iso(),
    }


@app.get("/api/graph")
async def graph_snapshot(session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    nodes_rows = await q_graph.list_graph_nodes(session, user_id=DEMO_USER_ID)
    edge_rows = await q_graph.list_graph_edges(
        session,
        org_id=DEMO_ORG_ID,
        user_id=DEMO_USER_ID,
        min_watchers=MIN_WATCHERS,
        edge_limit=GRAPH_EDGE_LIMIT,
    )

    nodes: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for d in nodes_rows:
        nodes.append({"id": d["id"], "kind": "investor", "data": map_investor(d)})
        seen_ids.add(d["id"])

    founder_signal_count: dict[str, int] = {}
    founder_meta: dict[str, dict[str, Any]] = {}
    edges: list[dict[str, Any]] = []
    for d in edge_rows:
        fid = d["founder_id"]
        founder_signal_count[fid] = founder_signal_count.get(fid, 0) + 1
        founder_meta[fid] = {
            "id":            fid,
            "name":          d["founder_name"] or "Unknown",
            "github_handle": d.get("founder_github"),
        }
        first_seen = d.get("first_seen_at")
        if first_seen is not None and not isinstance(first_seen, str):
            first_seen = first_seen.isoformat()
        edges.append({
            "id":       f"e-{d['watcher_id'][:8]}-{fid[:8]}",
            "sourceId": d["watcher_id"],
            "targetId": fid,
            "signal":   map_signal(
                founder_id=fid,
                founder_handle=d.get("founder_github"),
                signal={
                    "edge_type":     "FOLLOWS_ON_GITHUB",
                    "watcher_id":    d["watcher_id"],
                    "watcher_name":  d["watcher_name"],
                    "first_seen_at": first_seen,
                },
            ),
        })

    for fid, meta in founder_meta.items():
        if fid in seen_ids:
            continue
        seen_ids.add(fid)
        nodes.append({
            "id":   fid,
            "kind": "founder",
            "data": {
                "id":             fid,
                "name":           meta["name"],
                "headline":       f"Followed by {founder_signal_count.get(fid, 0)} watchlist members",
                "location":       "Unknown",
                "company":        None,
                "companyUrl":     None,
                "linkedinUrl":    None,
                "twitterHandle":  None,
                "githubUsername": meta["github_handle"],
                "initials":       initials_of(meta["name"]),
                "avatarColor":    avatar_color_for(fid),
            },
        })

    top_pick = (
        max(founder_signal_count.items(), key=lambda kv: kv[1])[0]
        if founder_signal_count else None
    )
    return {
        "nodes":            nodes,
        "edges":            edges,
        "topPickFounderId": top_pick,
        "generatedAt":      _now_iso(),
    }


@app.get("/api/person/{person_id}")
async def person_detail(person_id: str,
                         session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    head = await q_persons.get_person(session, person_id=person_id, user_id=DEMO_USER_ID)
    if head is None:
        raise HTTPException(status_code=404, detail="Person not found")
    is_investor = head.get("watch_tier") in ("active", "vip")

    if is_investor:
        return map_investor({
            "id":             head["id"],
            "name":           head["name"],
            "investor_type":  head.get("investor_type"),
            "archetype":      head.get("archetype"),
            "country":        head.get("country"),
            "linkedin_url":   head.get("linkedin_url"),
            "twitter_handle": head.get("twitter_handle"),
            "github_handle":  head.get("github_handle"),
        })

    return map_founder({
        "id":             head["id"],
        "name":           head["name"],
        "country":        head.get("country"),
        "github_handle":  head.get("github_handle"),
        "linkedin_url":   head.get("linkedin_url"),
        "twitter_handle": head.get("twitter_handle"),
        "watcher_count":  None,
    })


@app.get("/api/founder/{founder_id}")
async def founder_detail(founder_id: str,
                          session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    head = await q_persons.get_person(session, person_id=founder_id, user_id=DEMO_USER_ID)
    if head is None:
        raise HTTPException(status_code=404, detail="Founder not found")

    founder = map_founder({
        "id":             head["id"],
        "name":           head["name"],
        "country":        head.get("country"),
        "github_handle":  head.get("github_handle"),
        "linkedin_url":   head.get("linkedin_url"),
        "twitter_handle": head.get("twitter_handle"),
        "watcher_count":  None,
    })

    rule = get_rule(DEMO_USER_ID)
    alert_rows = await q_convergence.list_convergence_signals(
        session,
        org_id=DEMO_ORG_ID,
        user_id=DEMO_USER_ID,
        min_watchers=rule.min_distinct_watchers,
        limit=200,
    )

    import json as _json

    def _ensure_json_strings(row: dict[str, Any]) -> dict[str, Any]:
        out = dict(row)
        for k in ("evidence_json", "score_breakdown_json", "signal_type_counts_json"):
            v = out.get(k)
            if v is not None and not isinstance(v, str):
                out[k] = _json.dumps(v, default=str)
        return out

    alerts = [
        map_alert(_ensure_json_strings(r))
        for r in alert_rows if r["founder_id"] == founder_id
    ]
    return {**founder, "alerts": alerts}


# --- Stubbed endpoints (re-enabled in sub-project #11/#12) -----------------
# Pipeline + notifier + dossier writes still reference Neo4j-only modules. They
# return 501 during the migration so the frontend gets a clean error instead of
# a 500 from an import failure.

_NOT_YET = {"detail": "Endpoint disabled during v2 migration; coming back in sub-project #11/#12"}


@app.post("/api/pipeline/run")
async def pipeline_run_now(body: dict[str, Any] = Body(default_factory=dict)):
    raise HTTPException(status_code=501, detail=_NOT_YET["detail"])


@app.get("/api/pipeline/status")
async def pipeline_status():
    raise HTTPException(status_code=501, detail=_NOT_YET["detail"])


@app.post("/api/notifier/send-now")
async def notifier_send_now(body: dict[str, Any] = Body(default_factory=dict)):
    raise HTTPException(status_code=501, detail=_NOT_YET["detail"])


@app.get("/api/notifier/status")
async def notifier_status():
    raise HTTPException(status_code=501, detail=_NOT_YET["detail"])


@app.get("/api/dossiers")
async def list_dossiers(status: str | None = None, limit: int = 100):
    raise HTTPException(status_code=501, detail=_NOT_YET["detail"])


@app.get("/api/dossier/{dossier_id}")
async def get_dossier(dossier_id: str):
    raise HTTPException(status_code=501, detail=_NOT_YET["detail"])


@app.post("/api/dossiers/regenerate")
async def regenerate_dossier(body: dict[str, Any] = Body(default_factory=dict)):
    raise HTTPException(status_code=501, detail=_NOT_YET["detail"])


@app.post("/api/dossier/{dossier_id}/feedback")
async def submit_dossier_feedback(dossier_id: str, body: dict[str, Any] = Body(...)):
    raise HTTPException(status_code=501, detail=_NOT_YET["detail"])


@app.get("/api/dossier/{dossier_id}/feedback")
async def list_dossier_feedback(dossier_id: str):
    raise HTTPException(status_code=501, detail=_NOT_YET["detail"])


@app.get("/api/feedback")
async def list_all_feedback(since: str | None = None, limit: int = 100):
    raise HTTPException(status_code=501, detail=_NOT_YET["detail"])


@app.get("/")
def root() -> dict[str, Any]:
    return {
        "service": "signal-convergence-backend",
        "version": "0.2.0",
        "endpoints": [
            "/api/health",
            "/api/investors",
            "/api/founders",
            "/api/alerts",
            "/api/graph",
            "/api/person/{id}",
            "/api/founder/{id}",
            "/api/alert-rule",
            "/api/alerts/recompute",
            "/docs",
        ],
    }
