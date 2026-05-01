"""Twitter follow-tracker CLI (per CLAUDE_SCRAPEBADGER_IMPLEMENTATION_BRIEF.md).

  python scripts/track_scrapebadger_twitter_follows.py \\
      --watchlist data/twitter_vc_watchlist.txt \\
      --targets   data/twitter_interesting_people.txt \\
      --snapshot-dir data/scrapebadger_twitter_snapshots \\
      --signals-file data/scrapebadger_twitter_follow_signals.jsonl \\
      --max-pages 1

The CLI is purely file-in/file-out: it does not touch Neo4j. The graph load step
is `scrapers.jobs.load_twitter_signals_to_neo4j` (run via the orchestrator).

Wording rule: signals say "newly observed" — never "X followed Y at 10:03".
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scrapers.clients.raw_artifact_store import RawArtifactStore  # noqa: E402
from scrapers.clients.scrapebadger import ScrapebadgerClient, ScrapebadgerError  # noqa: E402
from scrapers.jobs.fetch_twitter_followings import (  # noqa: E402
    JobConfig,
    fetch_one_account,
    load_handles,
    parse_handle,
)

logger = logging.getLogger("track_scrapebadger")


def _load_target_set(path: Path | None) -> set[str] | None:
    if not path:
        return None
    if not path.exists():
        print(f"WARN: targets file {path} not found — switching to ALL mode",
              file=sys.stderr)
        return None
    handles = load_handles(path)
    return {h.lower() for h in handles}


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--watchlist", type=Path, required=True)
    parser.add_argument("--targets", type=Path, default=None)
    parser.add_argument("--snapshot-dir", type=Path,
                        default=ROOT / "data" / "scrapebadger_twitter_snapshots")
    parser.add_argument("--signals-file", type=Path,
                        default=ROOT / "data" / "scrapebadger_twitter_follow_signals.jsonl")
    parser.add_argument("--include-existing", action="store_true",
                        help="On first run, emit current follows as baseline signals.")
    parser.add_argument("--all-following", action="store_true",
                        help="Ignore --targets, emit any newly observed follow.")
    parser.add_argument("--max-pages", type=int, default=1)
    parser.add_argument("--no-write-snapshots", action="store_true",
                        help="Dry-run/diff without persisting new snapshots.")
    parser.add_argument("--limit", type=int, default=None,
                        help="Process only the first N watchers (testing).")
    parser.add_argument("--only-handles", type=str, default=None,
                        help="Comma-separated handles to restrict the run.")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")

    if not args.watchlist.exists():
        print(f"ERROR: watchlist file {args.watchlist} not found", file=sys.stderr)
        return 2

    watched = load_handles(args.watchlist)
    if args.only_handles:
        wanted = {h.strip().lower() for h in args.only_handles.split(",") if h.strip()}
        wanted = {h for h in (parse_handle(w) or "" for w in wanted) if h}
        watched = [h for h in watched if h in wanted]
    if args.limit:
        watched = watched[: args.limit]

    if not watched:
        print("ERROR: watchlist resolved to zero handles after filters", file=sys.stderr)
        return 2

    target_set: set[str] | None = None
    if not args.all_following:
        target_set = _load_target_set(args.targets)

    try:
        artifact_store = RawArtifactStore(ROOT / "data" / "raw_artifacts")
        client = ScrapebadgerClient(artifact_store=artifact_store)
    except ScrapebadgerError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    config = JobConfig(
        snapshot_dir=args.snapshot_dir,
        signals_file=args.signals_file,
        target_set=target_set,
        include_existing=args.include_existing,
        max_pages=args.max_pages,
        write_snapshots=not args.no_write_snapshots,
    )

    snapshots = signals_emitted = errors = 0
    print(f"Watching {len(watched)} handle(s); "
          f"targets={'ALL' if target_set is None else len(target_set)}; "
          f"max_pages={args.max_pages}; "
          f"include_existing={args.include_existing}")

    for i, handle in enumerate(watched, 1):
        result = fetch_one_account(client, handle, config)
        if result.error:
            errors += 1
            print(f"[{i}/{len(watched)}] {handle}  ERROR: {result.error}")
            continue
        if result.snapshot_written:
            snapshots += 1
        signals_emitted += result.signals_emitted
        for target in result.new_targets:
            print(f"  {handle} -> {target} (https://x.com/{handle}/following)")
        if not result.new_targets:
            print(f"[{i}/{len(watched)}] {handle}  no new follows")

    print()
    print(f"watched_accounts={len(watched)}")
    print(f"interesting_targets={'ALL' if target_set is None else len(target_set)}")
    print(f"snapshots={snapshots}")
    print(f"signals_emitted={signals_emitted}")
    print(f"errors={errors}")
    print(f"signals_file={args.signals_file}")
    return 0 if errors == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
