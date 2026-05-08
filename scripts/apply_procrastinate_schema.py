"""One-shot script to apply Procrastinate's schema to our Postgres.

Idempotent: re-running is a no-op once tables exist. Run after `alembic upgrade head`
on a fresh database.

    python -m scripts.apply_procrastinate_schema
"""
from __future__ import annotations

from dotenv import load_dotenv

load_dotenv()


def main() -> None:
    from worker.app import sync_app

    a = sync_app()
    with a.open():
        a.schema_manager.apply_schema()
    print("procrastinate schema applied")


if __name__ == "__main__":
    main()
