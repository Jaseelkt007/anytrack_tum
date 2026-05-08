"""Bootstrap a fresh Postgres branch with demo data.

Idempotent — re-running yields the same row set.

Run:
    python -m scripts.bootstrap_demo_data
"""
from __future__ import annotations

import asyncio
import csv
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy.dialects.postgresql import insert as pg_insert

load_dotenv()

from db.engine import dispose_engine, session_scope
from db.models import Person, PlatformIdentity, WatchlistMember

logger = logging.getLogger("bootstrap")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
NAMESPACE = uuid.UUID("8e1b3f2a-1c5d-4e7f-9a8b-2c3d4e5f6a7b")
DEMO_ORG = "demo"
DEMO_USER = "demo"


def gh_person_id(handle: str) -> uuid.UUID:
    return uuid.uuid5(NAMESPACE, f"gh:{handle.lower()}")


def li_person_id(slug: str) -> uuid.UUID:
    return uuid.uuid5(NAMESPACE, f"li:{slug.lower()}")


def tw_person_id(handle: str) -> uuid.UUID:
    return uuid.uuid5(NAMESPACE, f"tw:{handle.lower()}")


async def upsert_person(
    session,
    *,
    person_id: uuid.UUID,
    display_name: str,
    investor_type: str | None,
    country: str | None,
    sector_tags: list[str] | None,
    stage_tags: list[str] | None,
    entity_type: str = "Investor",
) -> None:
    now = datetime.now(timezone.utc)
    stmt = (
        pg_insert(Person)
        .values(
            id=person_id,
            org_id=DEMO_ORG,
            display_name=display_name,
            investor_type=investor_type,
            country=country,
            sector_tags=sector_tags,
            stage_tags=stage_tags,
            entity_type=entity_type,
            first_observed_at=now,
            last_observed_at=now,
        )
        .on_conflict_do_update(
            index_elements=["id"],
            set_={
                "display_name": display_name,
                "investor_type": investor_type,
                "country": country,
                "sector_tags": sector_tags,
                "stage_tags": stage_tags,
                "entity_type": entity_type,
                "last_observed_at": now,
            },
        )
    )
    await session.execute(stmt)


async def upsert_identity(
    session,
    *,
    person_id: uuid.UUID,
    platform: str,
    handle: str,
    profile_url: str | None,
) -> None:
    now = datetime.now(timezone.utc)
    stmt = (
        pg_insert(PlatformIdentity)
        .values(
            person_id=person_id,
            platform=platform,
            handle=handle.lower(),
            handle_original=handle,
            profile_url=profile_url,
            verified_via="manual",
            confidence=1.0,
            first_observed_at=now,
            last_observed_at=now,
        )
        .on_conflict_do_update(
            index_elements=["platform", "handle"],
            set_={
                "person_id": person_id,
                "handle_original": handle,
                "profile_url": profile_url,
                "last_observed_at": now,
            },
        )
    )
    await session.execute(stmt)


async def upsert_watchlist(
    session,
    *,
    person_id: uuid.UUID,
    tier: str,
    archetype: str | None = None,
    rationale: str | None = None,
) -> None:
    stmt = (
        pg_insert(WatchlistMember)
        .values(
            org_id=DEMO_ORG,
            user_id=DEMO_USER,
            person_id=person_id,
            tier=tier,
            archetype=archetype,
            rationale=rationale,
        )
        .on_conflict_do_update(
            index_elements=["org_id", "user_id", "person_id"],
            set_={"tier": tier, "archetype": archetype, "rationale": rationale},
        )
    )
    await session.execute(stmt)


async def load_reference_investors(session) -> int:
    count = 0
    with (DATA / "investors_clean.csv").open() as fh:
        for row in csv.DictReader(fh):
            slug = (row.get("linkedin_slug") or "").strip()
            twh = (row.get("twitter_handle") or "").strip()
            if not slug and not twh:
                continue
            pid = li_person_id(slug) if slug else tw_person_id(twh)
            await upsert_person(
                session,
                person_id=pid,
                display_name=row["display_name"],
                investor_type=(row.get("investor_type") or None),
                country=(row.get("country") or None),
                sector_tags=[t for t in (row.get("sector_tags") or "").split("|") if t],
                stage_tags=[t for t in (row.get("stage_tags") or "").split("|") if t],
                entity_type="Investor",
            )
            if slug:
                await upsert_identity(
                    session,
                    person_id=pid,
                    platform="linkedin",
                    handle=slug,
                    profile_url=(row.get("linkedin_url") or None),
                )
            if twh:
                await upsert_identity(
                    session,
                    person_id=pid,
                    platform="twitter",
                    handle=twh,
                    profile_url=None,
                )
            await upsert_watchlist(session, person_id=pid, tier="reference")
            count += 1
    return count


async def load_active_watchlist(session) -> int:
    count = 0
    with (DATA / "active_watchlist.csv").open() as fh:
        for row in csv.DictReader(fh):
            handle = row["github_handle"].strip()
            pid = gh_person_id(handle)
            await upsert_person(
                session,
                person_id=pid,
                display_name=row["display_name"],
                investor_type=None,
                country=None,
                sector_tags=None,
                stage_tags=None,
                entity_type="Investor",
            )
            await upsert_identity(
                session,
                person_id=pid,
                platform="github",
                handle=handle,
                profile_url=f"https://github.com/{handle}",
            )
            await upsert_watchlist(
                session,
                person_id=pid,
                tier="active",
                archetype=row.get("archetype") or None,
                rationale=row.get("rationale") or None,
            )
            count += 1
    return count


async def main() -> None:
    async with session_scope() as session:
        ref = await load_reference_investors(session)
        act = await load_active_watchlist(session)
        logger.info("loaded %d reference investors, %d active watchers", ref, act)
    await dispose_engine()


if __name__ == "__main__":
    asyncio.run(main())
