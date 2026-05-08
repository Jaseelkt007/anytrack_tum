"""Project-level pytest config — load .env before any test imports."""
import asyncio

import pytest
from dotenv import load_dotenv

load_dotenv()


@pytest.fixture(autouse=True)
def _dispose_db_engine_after_test():
    """Tear down any global async engine the test created.

    Without this, an open asyncpg pool collides with pytest-asyncio's
    event-loop teardown — surfacing as `RuntimeError: Event loop is closed`
    while asyncpg tries to cancel a stranded connection.
    """
    yield
    try:
        from db import engine as engine_mod
    except Exception:
        return
    if engine_mod._engine is None:
        return
    try:
        loop = asyncio.new_event_loop()
        loop.run_until_complete(engine_mod.dispose_engine())
        loop.close()
    except RuntimeError:
        pass
