"""Project-level pytest config — load .env before any test imports."""
import pytest_asyncio
from dotenv import load_dotenv

load_dotenv()


@pytest_asyncio.fixture(autouse=True, loop_scope="function")
async def _dispose_db_engine_after_test():
    """Tear down any global async engine the test created, *inside* the
    test's event loop. Required because asyncpg connections are bound to
    the loop that created them; closing them on a different loop produces
    `RuntimeError: Event loop is closed` at teardown.
    """
    yield
    try:
        from db import engine as engine_mod
    except Exception:
        return
    if engine_mod._engine is None:
        return
    try:
        await engine_mod.dispose_engine()
    except Exception:
        pass
