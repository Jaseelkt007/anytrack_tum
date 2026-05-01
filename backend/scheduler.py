"""Pipeline scheduler (M12).

Runs the full data pipeline on an interval inside the FastAPI process:

  Twitter ingest  ->  Convergence  ->  Dossier sweep

Concurrency model: BackgroundScheduler runs jobs on a thread pool, so they
never block the asyncio event loop. A single-process mutex prevents
overlapping runs (Twitter ingest can take 20+ minutes on free-tier
Scrapebadger). Run state is persisted to data/last_pipeline_run.json so
GET /api/health can surface 'when did we last run, what failed'.

Configuration via env:
  PIPELINE_INTERVAL_HOURS   default 6
  PIPELINE_SKIP_TWITTER     default false ('1'/'true'/'yes' to enable)
  PIPELINE_RUN_ON_STARTUP   default false ('1'/'true'/'yes' to fire one run when backend starts)
  DOSSIER_SCORE_THRESHOLD   default 0.0 — minimum ConvergenceEvent score to dossier-ize
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
STATUS_FILE = ROOT / "data" / "last_pipeline_run.json"

# --- Lock + status persistence -------------------------------------------

_lock = threading.Lock()


def is_running() -> bool:
    return _lock.locked()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_status(status: dict) -> None:
    STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATUS_FILE.write_text(json.dumps(status, indent=2, default=str))


def _read_status() -> dict:
    if not STATUS_FILE.exists():
        return {}
    try:
        return json.loads(STATUS_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def get_last_run() -> dict:
    """Return the last persisted status + a `currently_running` flag for the API."""
    s = _read_status()
    s["currently_running"] = is_running()
    return s


# --- Stage runner --------------------------------------------------------

def _run_stage(name: str, fn: Callable[[], dict], status: dict) -> dict:
    """Run one stage; capture timing + exception. Mutates and persists `status`."""
    stage = {"status": "running", "started_at": _now_iso()}
    status["stages"][name] = stage
    status["last_stage"] = name
    _write_status(status)
    try:
        result = fn() or {}
        stage["status"] = "success"
        stage["stats"] = result if isinstance(result, dict) else {"value": result}
    except Exception as e:
        stage["status"] = "failed"
        stage["error"] = f"{type(e).__name__}: {e}"
        logger.exception("pipeline stage %s failed", name)
    finally:
        stage["finished_at"] = _now_iso()
        _write_status(status)
    return stage


# --- Orchestration -------------------------------------------------------

def run_pipeline(
    *,
    skip_twitter: bool = False,
    stages: list[tuple[str, Callable[[], dict]]] | None = None,
) -> dict:
    """Run the full pipeline. Idempotent. Acquires the mutex; if held, skips.

    `stages` lets tests inject fakes. Default is the live pipeline.
    """
    if not _lock.acquire(blocking=False):
        logger.info("pipeline already running; skipping this tick")
        return {"skipped": True, "reason": "already_running"}

    if stages is None:
        stages = [
            ("twitter_ingest", _stage_twitter_ingest),
            ("convergence",    _stage_convergence),
            ("dossier_sweep",  _stage_dossier_sweep),
        ]
        if skip_twitter:
            stages = [s for s in stages if s[0] != "twitter_ingest"]

    status: dict[str, Any] = {
        "last_started_at": _now_iso(),
        "last_status": "running",
        "stages": {},
    }
    _write_status(status)

    try:
        for name, fn in stages:
            _run_stage(name, fn, status)

        any_failed = any(s.get("status") == "failed" for s in status["stages"].values())
        status["last_finished_at"] = _now_iso()
        status["last_status"] = "partial_failure" if any_failed else "success"
        _write_status(status)
        return status
    finally:
        _lock.release()


def trigger_pipeline_now(*, skip_twitter: bool = False) -> dict:
    """Kick off a pipeline run in a background thread. Returns immediately.

    Returns 'started_at': null + 'reason': 'already_running' when locked.
    """
    if is_running():
        return {"started": False, "reason": "already_running",
                "status": get_last_run()}

    t = threading.Thread(
        target=run_pipeline,
        kwargs={"skip_twitter": skip_twitter},
        daemon=True,
        name="pipeline-manual-trigger",
    )
    t.start()
    return {"started": True, "started_at": _now_iso(),
            "skip_twitter": skip_twitter}


# --- Live stage implementations -----------------------------------------

def _stage_twitter_ingest() -> dict:
    """Run the Scrapebadger CLI as a subprocess + load any new signals into Neo4j.

    Returns a stats dict. Raises only on truly fatal errors; partial-failure
    is encoded in the stats (errors > 0).
    """
    watchlist = ROOT / "data" / "twitter_vc_watchlist.txt"
    targets = ROOT / "data" / "twitter_interesting_people.txt"
    snapshot_dir = ROOT / "data" / "scrapebadger_twitter_snapshots"
    signals_path = ROOT / "data" / "scrapebadger_twitter_follow_signals.jsonl"

    if not watchlist.exists():
        return {"skipped": True, "reason": "no twitter_vc_watchlist.txt"}

    cli = [
        sys.executable,
        str(ROOT / "scripts" / "track_scrapebadger_twitter_follows.py"),
        "--watchlist",     str(watchlist),
        "--snapshot-dir",  str(snapshot_dir),
        "--signals-file",  str(signals_path),
        "--max-pages",     "1",
    ]
    if targets.exists():
        cli += ["--targets", str(targets)]

    proc = subprocess.run(cli, capture_output=True, text=True, timeout=3600)

    # Always load signals after — even if the CLI partially failed, prior signals
    # may be on disk waiting to be persisted.
    from neo4j import GraphDatabase
    from scrapers.jobs.load_twitter_signals_to_neo4j import load_signals
    driver = GraphDatabase.driver(
        os.environ["NEO4J_URI"],
        auth=(os.environ["NEO4J_USER"], os.environ["NEO4J_PASSWORD"]),
    )
    try:
        with driver.session() as session:
            res = load_signals(session, signals_path)
    finally:
        driver.close()

    return {
        "scrapebadger_exit_code": proc.returncode,
        "scrapebadger_tail": (proc.stdout or "").splitlines()[-1:][:1],
        "signals_read": res.signals_read,
        "edges_upserted": res.edges_upserted,
        "skipped_unknown_watcher": res.skipped_unknown_watcher,
        "errors": res.errors,
    }


def _stage_convergence() -> dict:
    from datetime import datetime, timezone as _tz

    from neo4j import GraphDatabase
    from intelligence.convergence import find_convergences, persist_events
    from intelligence.rule import get_rule

    rule = get_rule("demo")
    driver = GraphDatabase.driver(
        os.environ["NEO4J_URI"],
        auth=(os.environ["NEO4J_USER"], os.environ["NEO4J_PASSWORD"]),
    )
    try:
        events = find_convergences(driver, user_id="demo", rule=rule)
        end_iso = datetime.now(_tz.utc).isoformat()
        persist_events(driver, events, user_id="demo", window_end_iso=end_iso)
    finally:
        driver.close()

    return {
        "events_fired": len(events),
        "rule_signal_types": rule.signal_types,
        "min_watchers": rule.min_distinct_watchers,
    }


def _stage_dossier_sweep() -> dict:
    from neo4j import GraphDatabase
    from intelligence.dossier.classifier import GeminiClassifier
    from intelligence.dossier.dossier import build_or_update
    from intelligence.dossier.enrichment import enrich, TargetNotFoundError
    from scrapers.github_client import GitHubClient

    score_threshold = float(os.environ.get("DOSSIER_SCORE_THRESHOLD", "0.0"))

    gh_token = os.environ.get("GITHUB_TOKEN")
    github_client = GitHubClient(tokens=[gh_token]) if gh_token else None

    try:
        llm = GeminiClassifier()
    except RuntimeError as e:
        # No Gemini key — surface as a stage error.
        raise RuntimeError(f"Gemini unavailable: {e}") from e

    driver = GraphDatabase.driver(
        os.environ["NEO4J_URI"],
        auth=(os.environ["NEO4J_USER"], os.environ["NEO4J_PASSWORD"]),
    )
    stats = {"new": 0, "regenerated": 0, "cached": 0,
             "errors": 0, "ready_to_send": 0, "failed": 0,
             "processed": 0}
    try:
        with driver.session() as session:
            rows = session.run(
                """
                MATCH (c:ConvergenceEvent {user_id: $u})-[:ABOUT]->(p:Person)
                WHERE c.score >= $st
                RETURN p.canonical_id AS tid, collect(c.id) AS ids
                """,
                u="demo", st=score_threshold,
            ).data()

        for row in rows:
            stats["processed"] += 1
            try:
                with driver.session() as session:
                    bundle = enrich(session, row["tid"], user_id="demo",
                                    github_client=github_client)
                    res = build_or_update(
                        session, user_id="demo", bundle=bundle,
                        triggering_event_ids=row["ids"] or [], llm=llm,
                    )
                if res.is_new_dossier:
                    stats["new"] += 1
                if res.regenerated:
                    stats["regenerated"] += 1
                if res.cached:
                    stats["cached"] += 1
                if res.status == "ready_to_send":
                    stats["ready_to_send"] += 1
                if res.status == "failed":
                    stats["failed"] += 1
            except TargetNotFoundError:
                stats["errors"] += 1
            except Exception as e:
                stats["errors"] += 1
                logger.warning("dossier failed for %s: %s", row.get("tid"), e)
    finally:
        driver.close()

    return stats


# --- Scheduler lifecycle (called from FastAPI lifespan) ------------------

_scheduler = None  # apscheduler.schedulers.background.BackgroundScheduler | None


def start_scheduler() -> None:
    """Start the background scheduler. Idempotent — calling twice is a no-op."""
    global _scheduler
    if _scheduler is not None:
        return
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
    except ImportError:
        logger.warning("apscheduler not installed; pipeline scheduler disabled")
        return

    interval_hours = float(os.environ.get("PIPELINE_INTERVAL_HOURS", "6"))
    skip_twitter = os.environ.get("PIPELINE_SKIP_TWITTER", "").lower() in ("1", "true", "yes")
    run_on_startup = os.environ.get("PIPELINE_RUN_ON_STARTUP", "").lower() in ("1", "true", "yes")

    sched = BackgroundScheduler(daemon=True, timezone="UTC")
    sched.add_job(
        run_pipeline,
        trigger="interval",
        hours=interval_hours,
        id="pipeline",
        name="full-pipeline",
        max_instances=1,
        coalesce=True,
        kwargs={"skip_twitter": skip_twitter},
    )
    sched.start()
    _scheduler = sched
    logger.info(
        "pipeline scheduler started (interval=%sh, skip_twitter=%s, run_on_startup=%s)",
        interval_hours, skip_twitter, run_on_startup,
    )

    if run_on_startup:
        # Fire one run immediately, off the scheduler thread, so startup isn't blocked.
        threading.Thread(
            target=run_pipeline, kwargs={"skip_twitter": skip_twitter},
            daemon=True, name="pipeline-startup",
        ).start()


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler is None:
        return
    try:
        _scheduler.shutdown(wait=False)
    except Exception:  # pragma: no cover
        logger.exception("scheduler shutdown raised")
    _scheduler = None
