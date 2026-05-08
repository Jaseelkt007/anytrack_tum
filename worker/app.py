"""Procrastinate App — the job queue runtime for AnyTrace v2.

The App lives on the same Postgres database as the rest of the schema
(see DATABASE_URL_DIRECT). Procrastinate adds its own `procrastinate_*`
tables alongside ours; they don't interfere with the application schema.

The async connector is used by the worker process and by API/CLI code that
needs to enqueue jobs. The sync connector is used only for the one-shot
schema apply step.
"""
from __future__ import annotations

import os

from dotenv import load_dotenv
from procrastinate import App, PsycopgConnector, SyncPsycopgConnector

load_dotenv()


def _conninfo() -> str:
    """Plain libpq URL stripped of the SQLAlchemy driver prefix."""
    raw = os.environ["DATABASE_URL_DIRECT"]
    return raw.replace("postgresql+psycopg://", "postgresql://")


# Async App — used by the worker process and by enqueue calls from async code.
app = App(connector=PsycopgConnector(conninfo=_conninfo()))


def sync_app() -> App:
    """Sync app, only for the schema-apply CLI. Re-creates each call so we
    don't hold sync connections during normal runtime."""
    return App(connector=SyncPsycopgConnector(conninfo=_conninfo()))


# Tasks are imported here so `procrastinate --app=worker.app.app worker`
# discovers them. Late import avoids a circular import on enqueue helpers.
from worker import jobs  # noqa: E402,F401
