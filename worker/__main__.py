"""CLI entry — starts a Procrastinate worker.

Usage:
    python -m worker                        # listen on all queues
    python -m worker --queues scrape         # only the scrape queue
    python -m worker --queues scrape,intel   # multiple queues
"""
from __future__ import annotations

import argparse
import asyncio

from dotenv import load_dotenv

load_dotenv()


async def _run(queues: list[str] | None) -> None:
    # Ensure tasks (jobs.py) and periodic schedules (periodic.py) are registered.
    from worker import jobs as _jobs  # noqa: F401
    from worker import periodic as _periodic  # noqa: F401
    from worker.app import app

    async with app.open_async():
        await app.run_worker_async(queues=queues, wait=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--queues", default=None,
        help="comma-separated queue names; default = listen on all queues",
    )
    args = parser.parse_args()
    queues = [q.strip() for q in args.queues.split(",")] if args.queues else None
    asyncio.run(_run(queues))


if __name__ == "__main__":
    main()
