"""M7 backfill: walk every (Person, PlatformIdentity) in the graph and run them
through the resolver. Report any *proposed* merges. By default this is a
dry run — no graph mutations.

Acceptance criterion (PHASE_2_PLAN.md M7): zero unintended merges. Inspect every
proposed merge before re-running with --execute.

Usage:
    python scripts/backfill_identity_resolver.py             # dry-run, prints report
    python scripts/backfill_identity_resolver.py --no-llm    # skip Tier 3 entirely
    python scripts/backfill_identity_resolver.py --limit 50  # only first 50 identities
    python scripts/backfill_identity_resolver.py --execute   # apply merges (NOT YET IMPLEMENTED — see notes below)

The --execute path is intentionally a no-op for now: the safe pattern is to land
the resolver, run dry-runs across the existing graph, and only then add a
merge-execution path once we've reviewed the proposed merges in identity_decisions.jsonl.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from identity.resolver import build_default_resolver  # noqa: E402


WALK_QUERY = """
MATCH (p:Person)-[:HAS_IDENTITY]->(i:PlatformIdentity)
RETURN p.canonical_id AS canonical_id,
       p.display_name AS display_name,
       i.platform     AS platform,
       i.handle       AS handle
ORDER BY p.display_name, i.platform
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-llm", action="store_true",
                        help="Skip Tier 3 (LLM arbitration). Useful for cheap diagnostic runs.")
    parser.add_argument("--limit", type=int, default=None,
                        help="Only process the first N identities.")
    parser.add_argument("--execute", action="store_true",
                        help="Apply merges (currently disabled — see script docstring).")
    args = parser.parse_args()

    if args.execute:
        print("ERROR: --execute is intentionally disabled in this version.", file=sys.stderr)
        print("Run dry-run, inspect data/identity_decisions.jsonl, then enable in code "
              "after manual review.", file=sys.stderr)
        return 2

    from dotenv import load_dotenv
    from neo4j import GraphDatabase
    load_dotenv(ROOT / ".env")

    driver = GraphDatabase.driver(
        os.environ["NEO4J_URI"],
        auth=(os.environ["NEO4J_USER"], os.environ["NEO4J_PASSWORD"]),
    )

    tier_counts: Counter[str] = Counter()
    proposed_merges: list[dict] = []          # tier in (override, bio_link, llm_match)
    backfill_blind_spots: list[dict] = []     # tier='fresh' but id differs (backfill data too thin)
    consistent: int = 0
    rows_processed = 0

    with driver.session() as session:
        resolver = build_default_resolver(session, with_llm=not args.no_llm)

        rows = session.run(WALK_QUERY).data()
        if args.limit:
            rows = rows[: args.limit]

        for row in rows:
            current_cid = row["canonical_id"]
            platform = row["platform"]
            handle = row["handle"]

            # Backfill has no live profile fetch — only display_name. Tier 2
            # (bio-link) cannot fire here. This is expected; at ingest time the
            # profile blob will include bio and the resolver will fire Tier 2.
            profile_blob = {"display_name": row["display_name"]}

            result = resolver.resolve(platform, handle, profile_blob)
            tier_counts[result.tier] += 1
            rows_processed += 1

            if result.canonical_id == current_cid:
                consistent += 1
                continue

            entry = {
                "platform": platform,
                "handle": handle,
                "display_name": row["display_name"],
                "current_id": current_cid,
                "proposed_id": result.canonical_id,
                "tier": result.tier,
                "confidence": result.confidence,
                "reasoning": result.reasoning,
            }
            if result.tier in ("override", "bio_link", "llm_match"):
                proposed_merges.append(entry)
            else:
                backfill_blind_spots.append(entry)

    driver.close()

    print(f"\n--- M7 backfill report (dry run) ---")
    print(f"identities walked        : {rows_processed}")
    for tier, n in sorted(tier_counts.items()):
        print(f"  tier={tier:<10}  : {n}")
    print(f"consistent w/ graph      : {consistent}")
    print(f"PROPOSED MERGES          : {len(proposed_merges)}  (override / bio_link / llm_match)")
    print(f"backfill-blind 'fresh'   : {len(backfill_blind_spots)}  (resolver would recreate; benign — bio data not available at backfill)")

    if proposed_merges:
        print("\nFIRST 20 PROPOSED MERGES (review carefully — these are real merges):")
        for m in proposed_merges[:20]:
            print(f"  {m['platform']:<8} {m['handle']:<25} "
                  f"{m['current_id'][:8]}.. -> {m['proposed_id'][:8]}.. "
                  f"({m['tier']}, conf={m['confidence']:.2f}) "
                  f"[{m['display_name']}]")
            print(f"      reason: {m['reasoning']}")

    if args.no_llm:
        print("\n(LLM arbitration was disabled via --no-llm)")
    print(f"\nLLM decisions (when invoked) are logged to data/identity_decisions.jsonl.")

    if proposed_merges:
        print("\nNext step: review every proposed merge above. If any are wrong, add "
              "a guard in data/identity_overrides.csv or tighten the resolver "
              "before enabling --execute.")
    else:
        print("\nNo real merges proposed by Tier 1/2/3 — resolver is consistent with "
              "the existing graph for the data available at backfill time. ✓")
        print("Tier 2 (bio-link) cannot fire during backfill (no bio data); it will "
              "fire at ingest time in M8.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
