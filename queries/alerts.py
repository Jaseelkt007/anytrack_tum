"""alert_rule CRUD for the FastAPI alert-rule endpoints."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import AlertRule


async def get_rule(session: AsyncSession, *, org_id: str, user_id: str) -> dict[str, Any] | None:
    row = await session.execute(
        select(AlertRule).where(AlertRule.org_id == org_id, AlertRule.user_id == user_id)
    )
    obj = row.scalar_one_or_none()
    return dict(obj.payload) if obj else None


async def save_rule(session: AsyncSession, *, org_id: str, user_id: str,
                     payload: dict[str, Any]) -> None:
    now = datetime.now(timezone.utc)
    stmt = (
        pg_insert(AlertRule)
        .values(org_id=org_id, user_id=user_id, payload=payload, updated_at=now)
        .on_conflict_do_update(
            index_elements=["org_id", "user_id"],
            set_={"payload": payload, "updated_at": now},
        )
    )
    await session.execute(stmt)
    await session.commit()
