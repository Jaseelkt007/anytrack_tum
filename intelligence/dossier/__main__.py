"""Dossier-builder CLI (M9.5.4).

Walks every persisted ConvergenceEvent for a user above a configurable score
threshold, enriches each target, classifies via Gemini, and persists a Dossier.

Usage:
    python -m intelligence.dossier --user demo                           # all events above threshold
    python -m intelligence.dossier --user demo --target <canonical_id>   # one specific target
    python -m intelligence.dossier --user demo --score-threshold 4.0     # only stronger leads
    python -m intelligence.dossier --user demo --force-reclassify        # ignore bundle hash, re-call Gemini
    python -m intelligence.dossier --user demo --dry-run                 # don't persist or call Gemini
    python -m intelligence.dossier --user demo --skip-twitter            # skip recent_tweets fetch (cheap mode)
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from intelligence.dossier.classifier import GeminiClassifier
from intelligence.dossier.dossier import build_or_update, compute_bundle_hash
from intelligence.dossier.enrichment import EnrichmentBundle, enrich, TargetNotFoundError

logger = logging.getLogger("intelligence.dossier")


QUERY_EVENTS_FOR_USER = """
MATCH (c:ConvergenceEvent {user_id: $user_id})-[:ABOUT]->(p:Person)
WHERE c.score >= $score_threshold
RETURN p.canonical_id AS target_id,
       p.display_name AS target_name,
       collect(c.id)  AS event_ids,
       max(c.score)   AS max_score,
       max(c.distinct_member_count) AS n
ORDER BY max_score DESC
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--user", default="demo")
    parser.add_argument("--target", default=None,
                        help="Process only this canonical_id (skip the event walk).")
    parser.add_argument("--score-threshold", type=float, default=0.0,
                        help="Skip events whose max score is below this.")
    parser.add_argument("--force-reclassify", action="store_true")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print what would happen; do not call Gemini or persist.")
    parser.add_argument("--skip-twitter", action="store_true",
                        help="Skip Scrapebadger recent_tweets fetch (saves API calls).")
    parser.add_argument("--limit", type=int, default=None,
                        help="Process at most N targets.")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    from dotenv import load_dotenv
    from neo4j import GraphDatabase
    from scrapers.github_client import GitHubClient
    load_dotenv(ROOT / ".env")

    driver = GraphDatabase.driver(
        os.environ["NEO4J_URI"],
        auth=(os.environ["NEO4J_USER"], os.environ["NEO4J_PASSWORD"]),
    )

    gh_token = os.environ.get("GITHUB_TOKEN")
    github_client = GitHubClient(tokens=[gh_token]) if gh_token else None

    twitter_client = None
    if not args.skip_twitter:
        try:
            from scrapers.clients.scrapebadger import ScrapebadgerClient, ScrapebadgerError
            from scrapers.clients.raw_artifact_store import RawArtifactStore
            twitter_client = ScrapebadgerClient(
                artifact_store=RawArtifactStore(ROOT / "data" / "raw_artifacts"),
            )
        except (ScrapebadgerError, RuntimeError) as e:
            print(f"WARN: twitter client unavailable ({e}); proceeding without recent_tweets",
                  file=sys.stderr)

    llm = None
    if not args.dry_run:
        try:
            llm = GeminiClassifier()
        except RuntimeError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 2

    # Silence Neo4j "schema not yet populated" warnings on first run.
    logging.getLogger("neo4j.notifications").setLevel(logging.ERROR)

    # --- gather targets ---
    with driver.session() as session:
        if args.target:
            # Fetch event_ids for this single target so BUILT_FROM edges are created.
            ev_rows = session.run("""
                MATCH (c:ConvergenceEvent {user_id: $u})-[:ABOUT]->(p:Person {canonical_id: $tid})
                RETURN collect(c.id) AS ids,
                       coalesce(max(c.score), 0) AS sc
            """, u=args.user, tid=args.target).single()
            targets = [{"target_id": args.target,
                        "target_name": args.target,
                        "event_ids": (ev_rows["ids"] if ev_rows else []) or [],
                        "max_score": (ev_rows["sc"] if ev_rows else 0) or 0,
                        "n": 0}]
        else:
            rows = session.run(QUERY_EVENTS_FOR_USER,
                               user_id=args.user,
                               score_threshold=args.score_threshold).data()
            targets = rows
            if args.limit:
                targets = targets[: args.limit]

    if not targets:
        print(f"No targets found for user={args.user} score>={args.score_threshold}")
        return 0

    print(f"Building dossiers for {len(targets)} target(s) (user={args.user}, "
          f"score>={args.score_threshold}, dry_run={args.dry_run})\n")

    stats = {"new": 0, "cached": 0, "regenerated": 0, "failed": 0,
             "errors": 0, "ready_to_send": 0}

    for i, t in enumerate(targets, 1):
        target_id = t["target_id"]
        name = t.get("target_name") or target_id
        try:
            with driver.session() as session:
                bundle = enrich(session, target_id,
                                user_id=args.user,
                                github_client=github_client,
                                twitter_client=twitter_client)
                bundle_hash = compute_bundle_hash(bundle)
                if args.dry_run:
                    print(f"[{i}/{len(targets)}] {name:<40}  [dry-run] bundle_hash={bundle_hash[:12]}, "
                          f"score={t.get('max_score', 0)}")
                    continue
                res = build_or_update(
                    session, user_id=args.user, bundle=bundle,
                    triggering_event_ids=t.get("event_ids") or [],
                    llm=llm,
                    force_reclassify=args.force_reclassify,
                )
            tag = ("NEW" if res.is_new_dossier else
                   "CACHED" if res.cached else "OVERWRITE")
            print(f"[{i}/{len(targets)}] {name:<40}  {tag:<9} "
                  f"role={res.classification:<12} conf={res.confidence:.2f} "
                  f"status={res.status}")
            if res.cached:
                stats["cached"] += 1
            elif res.regenerated:
                stats["regenerated"] += 1
            if res.is_new_dossier:
                stats["new"] += 1
            if res.status == "failed":
                stats["failed"] += 1
            if res.status == "ready_to_send":
                stats["ready_to_send"] += 1
            if res.grounding_issues:
                for issue in res.grounding_issues:
                    print(f"      WARN: {issue}")
        except TargetNotFoundError as e:
            stats["errors"] += 1
            print(f"[{i}/{len(targets)}] {name:<40}  ERROR: {e}")
        except Exception as e:
            stats["errors"] += 1
            print(f"[{i}/{len(targets)}] {name:<40}  ERROR: {type(e).__name__}: {e}")

    print()
    print(f"--- Summary ---")
    for k, v in stats.items():
        print(f"  {k:<14} {v}")
    driver.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
