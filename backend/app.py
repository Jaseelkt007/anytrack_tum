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
from fastapi import FastAPI, HTTPException, Body
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
from intelligence import convergence as conv
from intelligence.rule import AlertRule, get_rule, save_rule, update_rule_partial, KNOWN_SIGNAL_TYPES, KNOWN_SORT_KEYS

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

    # M12: pipeline scheduler. Idempotent — disabled cleanly when apscheduler
    # is unavailable or PIPELINE_SCHEDULER_DISABLED=1.
    if os.environ.get("PIPELINE_SCHEDULER_DISABLED", "").lower() not in ("1", "true", "yes"):
        from backend import scheduler as _sched
        _sched.start_scheduler()

    yield

    try:
        from backend import scheduler as _sched
        _sched.stop_scheduler()
    except Exception:
        pass
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

    pipeline_block: dict[str, Any] = {}
    try:
        from backend import scheduler as _sched
        info = _sched.get_last_run()
        pipeline_block = {
            "currently_running": info.get("currently_running", False),
            "last_started_at":   info.get("last_started_at"),
            "last_finished_at":  info.get("last_finished_at"),
            "last_status":       info.get("last_status"),
            "stages":            {k: v.get("status")
                                  for k, v in (info.get("stages") or {}).items()},
        }
    except Exception:
        pipeline_block = {"available": False}

    return {
        "ok": neo4j_ok,
        "neo4j": neo4j_ok,
        "pipeline": pipeline_block,
        "generatedAt": _now_iso(),
    }


@app.post("/api/pipeline/run")
def pipeline_run_now(body: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    """Trigger an immediate pipeline run in the background.

    Body (all optional):
      skip_twitter: bool — when true, skips the slow Twitter ingest stage.

    Returns 202-style payload immediately. Poll /api/health for completion.
    """
    from backend import scheduler as _sched
    skip_twitter = bool(body.get("skip_twitter", False))
    res = _sched.trigger_pipeline_now(skip_twitter=skip_twitter)
    if not res.get("started"):
        # Already running — return 409 to be RESTful about it.
        raise HTTPException(status_code=409, detail=res)
    return res


@app.get("/api/pipeline/status")
def pipeline_status() -> dict[str, Any]:
    """Full status of the most recent pipeline run, including per-stage error messages."""
    from backend import scheduler as _sched
    return _sched.get_last_run()


def _dedupe_by_id(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Defense-in-depth: drop rows that share an id. Order-preserving."""
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for item in items:
        key = item.get("id")
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


@app.get("/api/investors")
def list_investors() -> list[dict[str, Any]]:
    with _session() as s:
        rows = list(s.run(queries.LIST_INVESTORS, user_id=DEMO_USER_ID))
    return _dedupe_by_id([map_investor(dict(r)) for r in rows])


@app.get("/api/founders")
def list_founders() -> list[dict[str, Any]]:
    with _session() as s:
        rows = list(s.run(
            queries.LIST_FOUNDER_CANDIDATES,
            user_id=DEMO_USER_ID,
            min_watchers=MIN_WATCHERS,
            limit=FOUNDER_LIMIT,
        ))
    return _dedupe_by_id([map_founder(dict(r)) for r in rows])


@app.get("/api/alerts")
def list_alerts() -> list[dict[str, Any]]:
    rule = get_rule(DEMO_USER_ID)
    with _session() as s:
        rows = list(s.run(
            queries.LIST_CONVERGENCE_SIGNALS,
            user_id=DEMO_USER_ID,
            min_watchers=rule.min_distinct_watchers,
            limit=rule.limit,
        ))
    out: list[dict[str, Any]] = []
    for i, r in enumerate(rows, start=1):
        out.append(map_alert(dict(r), window_days=rule.window_days, rank=i))
    return out


@app.get("/api/alert-rule")
def get_alert_rule() -> dict[str, Any]:
    """Return the current alert rule for the demo user, plus the allowed
    enum values so the frontend knows what choices to render."""
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
def update_alert_rule(patch: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Partial-update of the alert rule. Validates and persists. Returns the
    new full rule. Does NOT auto-recompute — call POST /api/alerts/recompute.
    """
    try:
        new = update_rule_partial(DEMO_USER_ID, patch)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"userId": DEMO_USER_ID, "rule": new.to_dict()}


@app.post("/api/alerts/recompute")
def recompute_alerts() -> dict[str, Any]:
    """Re-run the convergence detector with the current rule and persist events.

    Synchronous — returns when done, with a summary. Cheap on Phase 1 data.
    """
    if Neo4jState.driver is None:
        raise HTTPException(status_code=503, detail="Neo4j driver not ready")
    rule = get_rule(DEMO_USER_ID)
    events = conv.find_convergences(Neo4jState.driver, user_id=DEMO_USER_ID, rule=rule)
    end_iso = events[0].window_end if events else (datetime.now(timezone.utc).isoformat())
    conv.persist_events(Neo4jState.driver, events, user_id=DEMO_USER_ID, window_end_iso=end_iso)
    return {
        "userId":     DEMO_USER_ID,
        "rule":       rule.to_dict(),
        "fired":      len(events),
        "topTargets": [{"name": e.target_name, "score": e.score, "n": e.distinct_member_count} for e in events[:5]],
        "generatedAt": _now_iso(),
    }


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
    """Founder + the convergence alerts that fired for them.
    Additive (M9.5): includes latest_dossier_id when a Dossier exists."""
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
        latest_dossier = s.run(
            _LATEST_DOSSIER_FOR_TARGET,
            user_id=DEMO_USER_ID, target_id=founder_id,
        ).single()

    alerts = [map_alert(dict(r)) for r in alert_rows if r["founder_id"] == founder_id]
    out: dict[str, Any] = {**founder, "alerts": alerts}
    if latest_dossier:
        out["latest_dossier_id"] = latest_dossier["id"]
        out["latest_dossier_classification"] = latest_dossier["classification"]
        out["latest_dossier_status"] = latest_dossier["status"]
    return out


# --- M9.5 Dossier endpoints ------------------------------------------------

_LATEST_DOSSIER_FOR_TARGET = """
MATCH (d:Dossier {user_id: $user_id, target_person_id: $target_id})
RETURN d.id AS id,
       d.classification AS classification,
       d.status AS status,
       toString(d.generated_at) AS generated_at
ORDER BY d.generated_at DESC
LIMIT 1
"""

_LIST_DOSSIERS = """
MATCH (d:Dossier {user_id: $user_id})
WHERE $status IS NULL OR d.status = $status
OPTIONAL MATCH (d)-[:DOSSIER_FOR]->(p:Person)
RETURN d.id                  AS id,
       d.target_person_id    AS target_person_id,
       coalesce(p.display_name, '') AS target_name,
       d.classification      AS classification,
       d.confidence           AS confidence,
       d.status               AS status,
       coalesce(d.recommended_action, '') AS recommended_action,
       toString(d.generated_at) AS generated_at,
       coalesce(d.kb_cross_match_kind, 'unknown') AS kb_cross_match_kind
ORDER BY d.generated_at DESC
LIMIT $limit
"""

_GET_DOSSIER = """
MATCH (d:Dossier {id: $id})
OPTIONAL MATCH (d)-[:DOSSIER_FOR]->(p:Person)
OPTIONAL MATCH (d)-[:BUILT_FROM]->(c:ConvergenceEvent)
RETURN d, coalesce(p.display_name, '') AS target_name,
       collect(c.id) AS triggering_event_ids
"""


@app.get("/api/dossiers")
def list_dossiers(status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    """Summary list. Pass ?status=draft|ready_to_send|sent|rejected|failed to filter."""
    with _session() as s:
        rows = list(s.run(_LIST_DOSSIERS,
                          user_id=DEMO_USER_ID,
                          status=status,
                          limit=limit))
    return [dict(r) for r in rows]


@app.get("/api/dossier/{dossier_id}")
def get_dossier(dossier_id: str) -> dict[str, Any]:
    """Full dossier with parsed evidence_bundle and key_signals."""
    import json as _json
    with _session() as s:
        rec = s.run(_GET_DOSSIER, id=dossier_id).single()
        if rec is None:
            raise HTTPException(status_code=404, detail="Dossier not found")
        d = dict(rec["d"])
        evidence_bundle: Any = None
        if d.get("evidence_bundle_json"):
            try:
                evidence_bundle = _json.loads(d["evidence_bundle_json"])
            except _json.JSONDecodeError:
                evidence_bundle = None
        key_signals: list = []
        if d.get("key_signals_json"):
            try:
                key_signals = _json.loads(d["key_signals_json"])
            except _json.JSONDecodeError:
                key_signals = []
        cross_check_kb: dict = {}
        if d.get("cross_check_kb_json"):
            try:
                cross_check_kb = _json.loads(d["cross_check_kb_json"])
            except _json.JSONDecodeError:
                cross_check_kb = {}
        # Derive score explanation from the bundle's convergence evidence.
        score_explanation: list[dict[str, Any]] = []
        score_components: dict[str, float] = {}
        if isinstance(evidence_bundle, dict):
            ce = evidence_bundle.get("convergence_evidence") or {}
            owned = evidence_bundle.get("owned_repos") or []
            max_stars = max((int(r.get("stars") or 0) for r in owned), default=0)
            distinct = int(ce.get("distinct_member_count") or 0)
            # The bundle stores the total score but not the per-component
            # breakdown — recompute it from the rule + edges. Cheap enough.
            from intelligence.convergence import compute_score, compute_target_prominence
            from intelligence.rule import get_rule
            rule = get_rule(DEMO_USER_ID)
            prom = compute_target_prominence(
                max_stars,
                min_stars=rule.prominence_min_stars,
                max_cap=rule.prominence_max_stars_cap,
            )
            evidence_rows = ce.get("evidence_rows") or []
            newest_iso = max(
                (r.get("edge_at") for r in evidence_rows if r.get("edge_at")),
                default=None,
            )
            _, breakdown = compute_score(
                distinct, newest_iso,
                ce.get("window_end") or _now_iso(),
                rule.window_days,
                weight_distinct_members=rule.weight_distinct_members,
                weight_recency=rule.weight_recency,
                weight_member_quality=rule.weight_member_quality,
                weight_target_prominence=rule.weight_target_prominence,
                target_prominence_value=prom,
            )
            score_components = breakdown
            from backend.mappers import explain_score
            score_explanation = explain_score(
                breakdown,
                distinct_member_count=distinct,
                target_prominence_stars=max_stars,
            )

        # M9.5.5: feedback metadata so the frontend can render a feedback badge
        # and (when rejected) the timestamp the user marked it.
        from intelligence.dossier.feedback import count_feedback_for_dossier
        feedback_count = count_feedback_for_dossier(s, dossier_id)
        rejected_at: str | None = None
        if d.get("status") == "rejected":
            updated = d.get("status_updated_at")
            if updated is not None:
                rejected_at = str(updated)

        return {
            "id": d.get("id"),
            "target_person_id": d.get("target_person_id"),
            "target_name": rec["target_name"],
            "user_id": d.get("user_id"),
            "classification": d.get("classification"),
            "confidence": d.get("confidence"),
            "narrative": d.get("narrative"),
            "key_signals": key_signals,
            "recommended_action": d.get("recommended_action"),
            "cross_check_kb": cross_check_kb,
            "kb_cross_match_kind": d.get("kb_cross_match_kind"),
            "status": d.get("status"),
            "evidence_bundle": evidence_bundle,
            "evidence_bundle_hash": d.get("evidence_bundle_hash"),
            "generated_at": str(d.get("generated_at")),
            "llm_model": d.get("llm_model"),
            "triggering_event_ids": rec["triggering_event_ids"] or [],
            "score_components": score_components,
            "score_explanation": score_explanation,
            "feedback_count": feedback_count,
            "rejected_at": rejected_at,
        }


@app.post("/api/dossiers/regenerate")
def regenerate_dossier(body: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    """Trigger dossier (re)generation for one target or for all eligible events.

    Body:
      target_id          : str, optional. If given, only that target is processed.
      force_reclassify   : bool, default False. Bypasses bundle-hash idempotency.
      score_threshold    : float, default 0.0. Skip events below this score (when target_id is omitted).
    """
    target_id = body.get("target_id")
    force = bool(body.get("force_reclassify", False))
    score_threshold = float(body.get("score_threshold", 0.0))

    from intelligence.dossier.classifier import GeminiClassifier
    from intelligence.dossier.dossier import build_or_update
    from intelligence.dossier.enrichment import enrich, TargetNotFoundError
    from scrapers.github_client import GitHubClient

    gh_token = os.environ.get("GITHUB_TOKEN")
    github_client = GitHubClient(tokens=[gh_token]) if gh_token else None
    try:
        llm = GeminiClassifier()
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=f"Gemini unavailable: {e}")

    results: list[dict[str, Any]] = []
    with _session() as s:
        if target_id:
            ev = s.run("""
                MATCH (c:ConvergenceEvent {user_id: $u})-[:ABOUT]->(p:Person {canonical_id: $tid})
                RETURN collect(c.id) AS ids
            """, u=DEMO_USER_ID, tid=target_id).single()
            targets = [(target_id, (ev["ids"] if ev else []) or [])]
        else:
            rows = s.run("""
                MATCH (c:ConvergenceEvent {user_id: $u})-[:ABOUT]->(p:Person)
                WHERE c.score >= $st
                RETURN p.canonical_id AS tid, collect(c.id) AS ids
            """, u=DEMO_USER_ID, st=score_threshold).data()
            targets = [(r["tid"], r["ids"] or []) for r in rows]

        for tid, ev_ids in targets:
            try:
                bundle = enrich(s, tid, user_id=DEMO_USER_ID, github_client=github_client)
                res = build_or_update(
                    s, user_id=DEMO_USER_ID, bundle=bundle,
                    triggering_event_ids=ev_ids,
                    llm=llm, force_reclassify=force,
                )
                results.append({
                    "target_id": tid,
                    "dossier_id": res.dossier_id,
                    "classification": res.classification,
                    "confidence": res.confidence,
                    "status": res.status,
                    "regenerated": res.regenerated,
                    "cached": res.cached,
                    "grounding_issues": res.grounding_issues,
                })
            except TargetNotFoundError:
                results.append({"target_id": tid, "error": "not_found"})
            except Exception as e:
                results.append({"target_id": tid,
                                "error": f"{type(e).__name__}: {e}"})

    return {
        "user_id": DEMO_USER_ID,
        "processed": len(results),
        "results": results,
        "generatedAt": _now_iso(),
    }


# --- M9.5.5 Dossier feedback endpoints ------------------------------------

@app.post("/api/dossier/{dossier_id}/feedback")
def submit_dossier_feedback(
    dossier_id: str,
    body: dict[str, Any] = Body(...),
) -> dict[str, Any]:
    """Persist a feedback event for one dossier.

    Body:
      verdict                  : 'correct' | 'wrong_classification' | 'wrong_target' | 'spam' | 'low_priority'
      corrected_classification : (required when verdict='wrong_classification') one of the 5 classification roles
      notes                    : (optional) short free-text reviewer note

    Returns 200 with the FeedbackResult shape on success. Negative verdicts
    auto-flip the underlying dossier to status='rejected' (unless it's already
    'sent', in which case status is preserved per the M9.5 immutability rule).
    """
    from intelligence.dossier.feedback import (
        DossierNotFoundError,
        FeedbackValidationError,
        submit_feedback,
    )
    verdict = body.get("verdict")
    corrected = body.get("corrected_classification")
    notes = body.get("notes")
    try:
        with _session() as s:
            res = submit_feedback(
                s, user_id=DEMO_USER_ID, dossier_id=dossier_id,
                verdict=verdict, corrected_classification=corrected, notes=notes,
            )
    except FeedbackValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except DossierNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))

    return {
        "id":                  res.feedback_id,
        "dossier_id":          res.dossier_id,
        "verdict":             res.verdict,
        "submitted_at":        res.submitted_at,
        "side_effect":         res.side_effect,
        "new_dossier_status":  res.new_dossier_status,
    }


@app.get("/api/dossier/{dossier_id}/feedback")
def list_dossier_feedback(dossier_id: str) -> list[dict[str, Any]]:
    """Chronological list of all feedback events for one dossier."""
    from intelligence.dossier.feedback import list_feedback_for_dossier
    with _session() as s:
        return list_feedback_for_dossier(s, dossier_id)


@app.get("/api/feedback")
def list_all_feedback(since: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    """All feedback events for the demo user, optionally since an ISO datetime.
    Used by an admin/audit view and as the labelling source for M11."""
    from intelligence.dossier.feedback import list_feedback_for_user
    with _session() as s:
        return list_feedback_for_user(s, DEMO_USER_ID, since=since, limit=limit)


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
