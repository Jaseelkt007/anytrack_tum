"""Async SQLAlchemy engine + session factory.

The app uses the pooled URL (DATABASE_URL). Migrations use the direct URL.
"""
from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

_engine: AsyncEngine | None = None
_Session: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    global _engine, _Session
    if _engine is None:
        url = os.environ["DATABASE_URL"]
        _engine = create_async_engine(url, pool_pre_ping=True)
        _Session = async_sessionmaker(_engine, expire_on_commit=False)
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    get_engine()
    assert _Session is not None
    return _Session


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Use in scripts/CLIs. FastAPI uses Depends(get_session) instead."""
    Session = get_sessionmaker()
    async with Session() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency."""
    Session = get_sessionmaker()
    async with Session() as session:
        yield session


async def dispose_engine() -> None:
    global _engine, _Session
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _Session = None
