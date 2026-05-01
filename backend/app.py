"""FastAPI backend for the signal-convergence frontend.

Run locally:
    uvicorn backend.app:app --reload --host 0.0.0.0 --port 8000

Expose via ngrok with the permanent domain:
    ngrok http 8000 --domain=mispackaged-linn-prepoetic.ngrok-free.dev

Endpoints (response shapes match /mnt/d/signal-convergence/src/data/types.ts):
    GET /api/health             -> { ok, neo4j, generatedAt }
    GET /api/investors          -> Investor[]
    GET /api/founders           -> Founder[]
    GET /api/alerts             -> ConvergenceAlert[]
    GET /api/graph              -> { nodes, edges, topPickFounderId, generatedAt }
    GET /api/person/{id}        -> Investor | Founder
    GET /api/founder/{id}       -> Founder & { alerts: ConvergenceAlert[] }
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from neo4j import GraphDatabase, Driver
from neo4j.exceptions import ServiceUnavailable

from backend import queries
from backend.mappers import (
    avatar_color_for,
    initials_of,
    map_alert,
    map_founder,
    map_investor,
    map_signal,
    tier_for,
    title_for,
    group_for,
)

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

DEMO_USER_ID = "demo"
MIN_WATCHERS = int(os.getenv("CONVERGENCE_MIN_WATCHERS", "2"))
GRAPH_EDGE_LIMIT = int(os.getenv("GRAPH_EDGE_LIMIT", "500"))
FOUNDER_LIMIT = int(os.getenv("FOUNDER_LIMIT", "100"))


# --- Driver lifecycle -------------------------------------------------------

class Neo4jState:
    driver: Driver | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    uri = os.environ.get("NEO4J_URI")
    user = os.environ.get("NEO4J_USER")
    pw = os.environ.get("NEO4J_PASSWORD")
    if not all([uri, user, pw]):
        raise RuntimeError("NEO4J_URI / NEO4J_USER / NEO4J_PASSWORD must be set in .env")
    Neo4jState.driver = GraphDatabase.driver(uri, auth=(user, pw))
    yield
    if Neo4jState.driver is not None:
        Neo4jState.driver.close()


app = FastAPI(
    title="signal-convergence backend",
    version="0.1.0",
    lifespan=lifespan,
)


# --- CORS -------------------------------------------------------------------
# Permissive — Lovable preview origins are not stable, and ngrok handles auth.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "OPTIONS"],
    allow_headers=["*"],
)


# --- Helpers ----------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _session():
    if Neo4jState.driver is None:
        raise HTTPException(status_code=503, detail="Neo4j driver not ready")
    return Neo4jState.driver.session()


# --- Endpoints --------------------------------------------------------------

@app.get("/api/health")
def health() -> dict[str, Any]:
    neo4j_ok = False
    try:
        with _session() as s:
            r = s.run(queries.HEALTH).single()
            neo4j_ok = r is not None and r.get("ok") == 1
    except (ServiceUnavailable, Exception):  # pragma: no cover
        neo4j_ok = False
    return {"ok": neo4j_ok, "neo4j": neo4j_ok, "generatedAt": _now_iso()}


@app.get("/api/investors")
def list_investors() -> list[dict[str, Any]]:
    with _session() as s:
        rows = list(s.run(queries.LIST_INVESTORS, user_id=DEMO_USER_ID))
    return [map_investor(dict(r)) for r in rows]


@app.get("/api/founders")
def list_founders() -> list[dict[str, Any]]:
    with _session() as s:
        rows = list(s.run(
            queries.LIST_FOUNDER_CANDIDATES,
            user_id=DEMO_USER_ID,
            min_watchers=MIN_WATCHERS,
            limit=FOUNDER_LIMIT,
        ))
    return [map_founder(dict(r)) for r in rows]


@app.get("/api/alerts")
def list_alerts() -> list[dict[str, Any]]:
    with _session() as s:
        rows = list(s.run(
            queries.LIST_CONVERGENCE_SIGNALS,
            user_id=DEMO_USER_ID,
            min_watchers=MIN_WATCHERS,
            limit=FOUNDER_LIMIT,
        ))
    return [map_alert(dict(r)) for r in rows]


@app.get("/api/graph")
def graph_snapshot() -> dict[str, Any]:
    with _session() as s:
        nodes_rows = list(s.run(queries.GRAPH_NODES, user_id=DEMO_USER_ID))
        edge_rows = list(s.run(
            queries.GRAPH_EDGES,
            user_id=DEMO_USER_ID,
            min_watchers=MIN_WATCHERS,
            edge_limit=GRAPH_EDGE_LIMIT,
        ))

    # Investor nodes (active watchlist members)
    nodes: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for r in nodes_rows:
        d = dict(r)
        nodes.append({
            "id": d["id"],
            "kind": "investor",
            "data": map_investor(d),
        })
        seen_ids.add(d["id"])

    # Founder nodes (deduped by founder_id)
    founder_signal_count: dict[str, int] = {}
    founder_meta: dict[str, dict[str, Any]] = {}
    edges: list[dict[str, Any]] = []
    for r in edge_rows:
        d = dict(r)
        fid = d["founder_id"]
        founder_signal_count[fid] = founder_signal_count.get(fid, 0) + 1
        founder_meta[fid] = {
            "id":             fid,
            "name":           d["founder_name"] or "Unknown",
            "github_handle":  d.get("founder_github"),
        }
        # edge → frontend GraphEdgeDTO
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
                    "first_seen_at": d.get("first_seen_at"),
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

    top_pick = max(founder_signal_count.items(), key=lambda kv: kv[1])[0] if founder_signal_count else None
    return {
        "nodes":            nodes,
        "edges":            edges,
        "topPickFounderId": top_pick,
        "generatedAt":      _now_iso(),
    }


@app.get("/api/person/{person_id}")
def person_detail(person_id: str) -> dict[str, Any]:
    with _session() as s:
        head = s.run(queries.PERSON_DETAIL, id=person_id, user_id=DEMO_USER_ID).single()
        if head is None:
            raise HTTPException(status_code=404, detail="Person not found")
        head_d = dict(head)
        is_active = head_d.get("watch_tier") == "active"

    if is_active:
        return map_investor({
            "id":              head_d["id"],
            "name":            head_d["name"],
            "investor_type":   head_d.get("investor_type"),
            "archetype":       head_d.get("archetype"),
            "country":         head_d.get("country"),
            "linkedin_url":    head_d.get("linkedin_url"),
            "twitter_handle":  head_d.get("twitter_handle"),
            "github_handle":   head_d.get("github_handle"),
        })

    # Otherwise treat as founder candidate
    return map_founder({
        "id":              head_d["id"],
        "name":            head_d["name"],
        "country":         head_d.get("country"),
        "github_handle":   head_d.get("github_handle"),
        "linkedin_url":    head_d.get("linkedin_url"),
        "twitter_handle":  head_d.get("twitter_handle"),
        "watcher_count":   None,
    })


@app.get("/api/founder/{founder_id}")
def founder_detail(founder_id: str) -> dict[str, Any]:
    """Founder + the convergence alerts that fired for them."""
    with _session() as s:
        head = s.run(queries.PERSON_DETAIL, id=founder_id, user_id=DEMO_USER_ID).single()
        if head is None:
            raise HTTPException(status_code=404, detail="Founder not found")
        head_d = dict(head)
        founder = map_founder({
            "id":             head_d["id"],
            "name":           head_d["name"],
            "country":        head_d.get("country"),
            "github_handle":  head_d.get("github_handle"),
            "linkedin_url":   head_d.get("linkedin_url"),
            "twitter_handle": head_d.get("twitter_handle"),
            "watcher_count":  None,
        })
        # Alerts for THIS founder only
        alert_rows = list(s.run(
            queries.LIST_CONVERGENCE_SIGNALS,
            user_id=DEMO_USER_ID,
            min_watchers=MIN_WATCHERS,
            limit=200,
        ))

    alerts = [map_alert(dict(r)) for r in alert_rows if r["founder_id"] == founder_id]
    return {**founder, "alerts": alerts}


# Also expose /api/explore as an alias if the frontend looks for it.
@app.get("/")
def root() -> dict[str, Any]:
    return {
        "service": "signal-convergence-backend",
        "endpoints": [
            "/api/health",
            "/api/investors",
            "/api/founders",
            "/api/alerts",
            "/api/graph",
            "/api/person/{id}",
            "/api/founder/{id}",
            "/docs",
        ],
    }
