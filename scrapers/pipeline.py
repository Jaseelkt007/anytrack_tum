"""GitHub ingestion pipeline orchestrator.

Reads tier='active' watchlist members from Neo4j (each must have a github
PlatformIdentity), fetches their stars and follows from GitHub, writes
append-only edges into Neo4j.

Usage:
    python -m scrapers.pipeline                       # full sweep, default user 'demo'
    python -m scrapers.pipeline --limit 3             # cap to N members for fast iteration
    python -m scrapers.pipeline --skip-stars          # only fetch follows
    python -m scrapers.pipeline --skip-follows        # only fetch stars
    python -m scrapers.pipeline --user-id demo --max-pages 2
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Ensure project root is importable when run as `python -m scrapers.pipeline` from /
sys.path.insert(0, str(ROOT))

from scrapers import cypher
from scrapers.github_client import GitHubClient
from scrapers.jobs.fetch_following import fetch_following
from scrapers.jobs.fetch_starred_repos import fetch_starred

logger = logging.getLogger("scrapers.pipeline")


def _gather_tokens() -> list[str]:
    """Collect any present GITHUB_TOKEN[_2|_3|_4] env vars."""
    tokens: list[str] = []
    primary = os.environ.get("GITHUB_TOKEN")
    if primary:
        tokens.append(primary)
    for suffix in ("_2", "_3", "_4"):
        extra = os.environ.get(f"GITHUB_TOKEN{suffix}")
        if extra:
            tokens.append(extra)
    return tokens


def fetch_active_watchlist(session, user_id: str) -> list[dict]:
    result = session.run(cypher.QUERY_ACTIVE_WATCHLIST_WITH_GITHUB, user_id=user_id)
    return [dict(r) for r in result]


def ingest_stars(session, client: GitHubClient, watcher: dict, now_iso: str,
                 max_pages: int | None) -> tuple[int, int]:
    """Returns (repos_observed, stars_observed)."""
    handle = watcher["github_handle"]
    repos = 0
    stars = 0
    for event in fetch_starred(client, handle, max_pages=max_pages):
        # Repository upsert
        session.run(
            cypher.UPSERT_REPOSITORY,
            github_id=str(event.repo_github_id),
            owner_handle=event.repo_owner,
            name=event.repo_name,
            full_name=event.repo_full_name,
            description=event.repo_description,
            language=event.repo_language,
            star_count=event.repo_star_count,
            html_url=event.repo_html_url,
            now_iso=now_iso,
        )
        repos += 1
        # Watcher --STARRED_REPO--> Repository (with historical starred_at)
        session.run(
            cypher.MERGE_STARRED_REPO,
            watcher_id=watcher["canonical_id"],
            repo_github_id=str(event.repo_github_id),
            starred_at=event.starred_at,
            now_iso=now_iso,
        )
        stars += 1
        # Opportunistic: if the repo owner is a known Person via github identity,
        # tag OWNS_REPO. Cheap — runs once per star.
        session.run(
            cypher.MERGE_OWNS_REPO,
            owner_handle=event.repo_owner,
            repo_github_id=str(event.repo_github_id),
            now_iso=now_iso,
        )
    return repos, stars


def ingest_follows(session, client: GitHubClient, watcher: dict, now_iso: str,
                   max_pages: int | None) -> int:
    handle = watcher["github_handle"]
    follows = 0
    for entry in fetch_following(client, handle, max_pages=max_pages):
        followed_id = cypher.github_person_id(entry.handle)
        # Upsert the followed Person + their github identity.
        session.run(
            cypher.UPSERT_PERSON_BY_GITHUB,
            canonical_id=followed_id,
            handle=entry.handle,
            profile_url=entry.profile_url,
            display_name=entry.handle,  # bare handle until enriched
            now_iso=now_iso,
        )
        # Watcher -> followed edge.
        session.run(
            cypher.MERGE_FOLLOWS_GITHUB,
            watcher_id=watcher["canonical_id"],
            followed_id=followed_id,
            now_iso=now_iso,
        )
        follows += 1
    return follows


def run(user_id: str, limit: int | None, max_pages: int | None,
        skip_stars: bool, skip_follows: bool,
        only_handles: set[str] | None = None) -> int:
    try:
        from dotenv import load_dotenv
        from neo4j import GraphDatabase
    except ImportError as exc:
        print(f"ERROR: missing dependency ({exc.name}). Run: pip install -r requirements.txt", file=sys.stderr)
        return 2

    load_dotenv(ROOT / ".env")
    tokens = _gather_tokens()
    if not tokens:
        print("ERROR: GITHUB_TOKEN not set in .env", file=sys.stderr)
        return 2

    neo4j_uri = os.environ.get("NEO4J_URI")
    neo4j_user = os.environ.get("NEO4J_USER")
    neo4j_pw = os.environ.get("NEO4J_PASSWORD")
    if not all([neo4j_uri, neo4j_user, neo4j_pw]):
        print("ERROR: Neo4j env vars missing", file=sys.stderr)
        return 2

    client = GitHubClient(tokens=tokens)
    driver = GraphDatabase.driver(neo4j_uri, auth=(neo4j_user, neo4j_pw))
    now_iso = datetime.now(timezone.utc).isoformat()

    total_repos = total_stars = total_follows = 0

    with driver.session() as session:
        members = fetch_active_watchlist(session, user_id)
        if not members:
            print("WARN: no tier='active' watchlist members with a GitHub identity found.", file=sys.stderr)
            print("      Run scripts/promote_active_watchlist.py first.", file=sys.stderr)
            return 1

        if only_handles:
            members = [m for m in members if (m.get("github_handle") or "").lower() in only_handles]
        if limit:
            members = members[:limit]

        print(f"Active watchlist members to process: {len(members)}")
        for i, m in enumerate(members, start=1):
            label = f"{m['display_name']} ({m['github_handle']})"
            print(f"[{i}/{len(members)}] {label}")
            try:
                if not skip_stars:
                    repos, stars = ingest_stars(session, client, m, now_iso, max_pages)
                    total_repos += repos
                    total_stars += stars
                    print(f"    stars: {stars}")
                if not skip_follows:
                    f = ingest_follows(session, client, m, now_iso, max_pages)
                    total_follows += f
                    print(f"    follows: {f}")
            except Exception as exc:
                logger.error("failed for %s: %s", label, exc)
                continue

    driver.close()

    print()
    print(f"Pipeline complete.  repos_upserted={total_repos}  stars={total_stars}  follows={total_follows}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user-id", default="demo")
    parser.add_argument("--limit", type=int, default=None,
                        help="cap number of watchlist members processed")
    parser.add_argument("--only-handles", type=str, default=None,
                        help="comma-separated github handles to restrict to")
    parser.add_argument("--max-pages", type=int, default=None,
                        help="cap pages per fetch (useful for fast iteration)")
    parser.add_argument("--skip-stars", action="store_true")
    parser.add_argument("--skip-follows", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    only = None
    if args.only_handles:
        only = {h.strip().lower() for h in args.only_handles.split(",") if h.strip()}
    return run(args.user_id, args.limit, args.max_pages, args.skip_stars, args.skip_follows, only_handles=only)


if __name__ == "__main__":
    sys.exit(main())
