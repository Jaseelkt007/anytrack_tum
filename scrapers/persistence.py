"""Postgres write helpers for scraper jobs.

Every Cypher MERGE in the deleted scrapers/cypher.py maps to one of these
helpers. They are the only place edge_event / person / platform_identity /
repository / repository_owner rows are written by the ingestion pipeline.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import (
    PlatformIdentity,
    Person,
    Repository,
    RepositoryOwner,
)

# Same UUIDv5 namespace as data/identity_overrides + scripts/bootstrap_demo_data.
NAMESPACE = uuid.UUID("8e1b3f2a-1c5d-4e7f-9a8b-2c3d4e5f6a7b")


def gh_person_id(handle: str) -> uuid.UUID:
    return uuid.uuid5(NAMESPACE, f"gh:{handle.lower()}")


def tw_person_id(handle: str) -> uuid.UUID:
    return uuid.uuid5(NAMESPACE, f"tw:{handle.lower()}")


# --- Person + identity ------------------------------------------------------

async def upsert_person_by_github(
    session: AsyncSession,
    *,
    org_id: str,
    handle: str,
    display_name: str | None = None,
    profile_url: str | None = None,
    kind: str = "User",
) -> uuid.UUID:
    pid = gh_person_id(handle)
    name = display_name or handle
    profile = profile_url or f"https://github.com/{handle}"
    now = datetime.now(timezone.utc)

    await session.execute(
        pg_insert(Person)
        .values(
            id=pid, org_id=org_id, display_name=name,
            entity_type=kind, role_tags=["observed"], confidence_score=0.6,
            first_observed_at=now, last_observed_at=now,
        )
        .on_conflict_do_update(
            index_elements=["id"],
            set_={"last_observed_at": now},
        )
    )
    await session.execute(
        pg_insert(PlatformIdentity)
        .values(
            person_id=pid, platform="github",
            handle=handle.lower(), handle_original=handle,
            profile_url=profile, verified_via="observed", confidence=0.6,
            kind=kind, first_observed_at=now, last_observed_at=now,
        )
        .on_conflict_do_update(
            index_elements=["platform", "handle"],
            set_={"last_observed_at": now, "person_id": pid},
        )
    )
    return pid


async def upsert_person_by_twitter(
    session: AsyncSession,
    *,
    org_id: str,
    handle: str,
    display_name: str | None = None,
    profile_url: str | None = None,
    canonical_id: uuid.UUID | None = None,
) -> uuid.UUID:
    pid = canonical_id or tw_person_id(handle)
    name = display_name or handle
    now = datetime.now(timezone.utc)

    await session.execute(
        pg_insert(Person)
        .values(
            id=pid, org_id=org_id, display_name=name,
            role_tags=["observed"], confidence_score=0.6,
            first_observed_at=now, last_observed_at=now,
        )
        .on_conflict_do_update(
            index_elements=["id"],
            set_={"last_observed_at": now},
        )
    )
    await session.execute(
        pg_insert(PlatformIdentity)
        .values(
            person_id=pid, platform="twitter",
            handle=handle.lower(), handle_original=handle,
            profile_url=profile_url, verified_via="observed", confidence=0.6,
            first_observed_at=now, last_observed_at=now,
        )
        .on_conflict_do_update(
            index_elements=["platform", "handle"],
            set_={"last_observed_at": now, "person_id": pid},
        )
    )
    return pid


# --- Repository -------------------------------------------------------------

async def upsert_repository(
    session: AsyncSession,
    *,
    github_id: str,
    owner_handle: str,
    name: str,
    full_name: str,
    description: str | None,
    language: str | None,
    star_count: int | None,
    html_url: str | None,
) -> None:
    now = datetime.now(timezone.utc)
    await session.execute(
        pg_insert(Repository)
        .values(
            github_id=str(github_id), owner_handle=owner_handle,
            name=name, full_name=full_name, description=description,
            language=language, star_count_observed=star_count,
            html_url=html_url, last_fetched_at=now,
        )
        .on_conflict_do_update(
            index_elements=["github_id"],
            set_={
                "owner_handle": owner_handle,
                "name": name,
                "full_name": full_name,
                "description": description,
                "language": language,
                "star_count_observed": star_count,
                "html_url": html_url,
                "last_fetched_at": now,
            },
        )
    )


async def link_repo_owner(
    session: AsyncSession,
    *,
    org_id: str,
    repo_github_id: str,
    owner_handle: str,
    owner_display_name: str | None = None,
) -> None:
    """Resolve owner_handle to a Person (creating if needed) + link via repository_owner."""
    owner_pid = await upsert_person_by_github(
        session,
        org_id=org_id,
        handle=owner_handle,
        display_name=owner_display_name or owner_handle,
    )
    await session.execute(
        pg_insert(RepositoryOwner)
        .values(repo_id=str(repo_github_id), owner_person_id=owner_pid)
        .on_conflict_do_nothing()
    )


# --- Edge event -------------------------------------------------------------

async def record_edge_event(
    session: AsyncSession,
    *,
    org_id: str,
    source: str,
    action_type: str,
    watcher_person_id: uuid.UUID,
    target_kind: str,
    target_person_id: uuid.UUID | None = None,
    target_repo_id: str | None = None,
    observed_at: datetime,
    evidence_url: str | None = None,
    edge_confidence: float | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Idempotent — re-observation updates last_seen_at; first_seen_at is fixed.

    The dedup index `uq_ee_observation_dedup` (defined in the initial migration)
    has a COALESCE expression that we cannot express through SQLAlchemy
    declaratively; therefore we use raw SQL with the matching ON CONFLICT clause.
    """
    now = datetime.now(timezone.utc)
    metadata_json = json.dumps(metadata, default=str) if metadata else None
    await session.execute(
        text("""
            INSERT INTO edge_event
              (org_id, source, action_type, watcher_person_id, target_kind,
               target_person_id, target_repo_id, observed_at, first_seen_at, last_seen_at,
               evidence_url, edge_confidence, metadata)
            VALUES (:org_id, :source, :action_type, :watcher_id, :target_kind,
                    :target_person_id, :target_repo_id, :observed_at, :first_seen_at, :last_seen_at,
                    :evidence_url, :edge_confidence, CAST(:metadata AS JSONB))
            ON CONFLICT (source, action_type, watcher_person_id,
                         COALESCE(target_person_id::text, target_repo_id))
            DO UPDATE SET last_seen_at = EXCLUDED.last_seen_at,
                          evidence_url = COALESCE(EXCLUDED.evidence_url, edge_event.evidence_url)
        """),
        {
            "org_id": org_id,
            "source": source,
            "action_type": action_type,
            "watcher_id": watcher_person_id,
            "target_kind": target_kind,
            "target_person_id": target_person_id,
            "target_repo_id": target_repo_id,
            "observed_at": observed_at,
            "first_seen_at": now,
            "last_seen_at": now,
            "evidence_url": evidence_url,
            "edge_confidence": edge_confidence,
            "metadata": metadata_json,
        },
    )


# --- Reads ------------------------------------------------------------------

async def fetch_active_watchlist_with_github(
    session: AsyncSession, *, user_id: str
) -> list[dict]:
    """Active watchlist members with a github platform identity."""
    result = await session.execute(
        text("""
            SELECT
                p.id              AS canonical_id,
                p.display_name    AS display_name,
                pi.handle         AS github_handle
            FROM watchlist_member wm
            JOIN person p ON p.id = wm.person_id
            JOIN platform_identity pi
              ON pi.person_id = p.id AND pi.platform = 'github'
            WHERE wm.user_id = :user_id
              AND wm.tier    = 'active'
            ORDER BY p.display_name
        """),
        {"user_id": user_id},
    )
    return [dict(r) for r in result.mappings()]


async def find_person_by_twitter_handle(
    session: AsyncSession, *, handle: str
) -> uuid.UUID | None:
    """Resolve a twitter handle to an existing canonical person id, if any."""
    result = await session.execute(
        text(
            "SELECT person_id FROM platform_identity "
            "WHERE platform = 'twitter' AND handle = :h LIMIT 1"
        ),
        {"h": handle.lower()},
    )
    row = result.first()
    return row[0] if row else None
