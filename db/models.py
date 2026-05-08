"""SQLAlchemy 2.0 models for AnyTrace v2.

Schema mirrors docs/superpowers/specs/2026-05-08-anytrace-v2-event-log-postgres-design.md §4.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    ARRAY,
    BigInteger,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    PrimaryKeyConstraint,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


# --- multi-tenancy ----------------------------------------------------------

class Org(Base):
    __tablename__ = "org"
    id: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class AppUser(Base):
    __tablename__ = "app_user"
    id: Mapped[str] = mapped_column(Text, primary_key=True)
    org_id: Mapped[str] = mapped_column(Text, ForeignKey("org.id"), nullable=False, index=True)
    email: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


# --- canonical entities -----------------------------------------------------

class Person(Base):
    __tablename__ = "person"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    org_id: Mapped[str] = mapped_column(Text, ForeignKey("org.id"), nullable=False, index=True)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    investor_type: Mapped[str | None] = mapped_column(Text)
    country: Mapped[str | None] = mapped_column(Text)
    sector_tags: Mapped[list[str] | None] = mapped_column(ARRAY(String))
    stage_tags: Mapped[list[str] | None] = mapped_column(ARRAY(String))
    role_tags: Mapped[list[str] | None] = mapped_column(ARRAY(String))
    confidence_score: Mapped[float | None] = mapped_column(Float)
    entity_type: Mapped[str | None] = mapped_column(Text, index=True)
    first_observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    identities: Mapped[list["PlatformIdentity"]] = relationship(
        back_populates="person", cascade="all, delete-orphan"
    )


class PlatformIdentity(Base):
    __tablename__ = "platform_identity"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    person_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("person.id", ondelete="CASCADE"), nullable=False, index=True
    )
    platform: Mapped[str] = mapped_column(Text, nullable=False)
    handle: Mapped[str] = mapped_column(Text, nullable=False)
    handle_original: Mapped[str | None] = mapped_column(Text)
    profile_url: Mapped[str | None] = mapped_column(Text)
    verified_via: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[float | None] = mapped_column(Float)
    kind: Mapped[str | None] = mapped_column(Text)
    first_observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    person: Mapped[Person] = relationship(back_populates="identities")

    __table_args__ = (
        UniqueConstraint("platform", "handle", name="uq_pi_platform_handle"),
        CheckConstraint("platform IN ('github','twitter','linkedin')", name="ck_pi_platform"),
    )


# --- watchlist --------------------------------------------------------------

class WatchlistMember(Base):
    __tablename__ = "watchlist_member"
    org_id: Mapped[str] = mapped_column(Text, ForeignKey("org.id"), nullable=False)
    user_id: Mapped[str] = mapped_column(Text, ForeignKey("app_user.id"), nullable=False)
    person_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("person.id"), nullable=False
    )
    tier: Mapped[str] = mapped_column(Text, nullable=False)
    archetype: Mapped[str | None] = mapped_column(Text)
    rationale: Mapped[str | None] = mapped_column(Text)
    added_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        PrimaryKeyConstraint("org_id", "user_id", "person_id"),
        CheckConstraint("tier IN ('active','vip','reference')", name="ck_wm_tier"),
        Index("idx_wm_user_tier", "user_id", "tier"),
    )


# --- repositories -----------------------------------------------------------

class Repository(Base):
    __tablename__ = "repository"
    github_id: Mapped[str] = mapped_column(Text, primary_key=True)
    owner_handle: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    full_name: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    description: Mapped[str | None] = mapped_column(Text)
    language: Mapped[str | None] = mapped_column(Text)
    star_count_observed: Mapped[int | None] = mapped_column(Integer)
    html_url: Mapped[str | None] = mapped_column(Text)
    last_fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    first_observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class RepositoryOwner(Base):
    __tablename__ = "repository_owner"
    repo_id: Mapped[str] = mapped_column(Text, ForeignKey("repository.github_id"), nullable=False)
    owner_person_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("person.id"), nullable=False
    )
    __table_args__ = (PrimaryKeyConstraint("repo_id", "owner_person_id"),)


# --- the event log ----------------------------------------------------------

class EdgeEvent(Base):
    __tablename__ = "edge_event"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    org_id: Mapped[str] = mapped_column(Text, ForeignKey("org.id"), nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    action_type: Mapped[str] = mapped_column(Text, nullable=False)
    watcher_person_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("person.id"), nullable=False
    )
    target_kind: Mapped[str] = mapped_column(Text, nullable=False)
    target_person_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("person.id")
    )
    target_repo_id: Mapped[str | None] = mapped_column(Text, ForeignKey("repository.github_id"))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    evidence_url: Mapped[str | None] = mapped_column(Text)
    edge_confidence: Mapped[float | None] = mapped_column(Float)
    raw_artifact_ref: Mapped[str | None] = mapped_column(Text)
    metadata_: Mapped[dict[str, Any] | None] = mapped_column("metadata", JSONB)

    __table_args__ = (
        CheckConstraint("source IN ('github','twitter','linkedin')", name="ck_ee_source"),
        CheckConstraint("target_kind IN ('person','repository')", name="ck_ee_target_kind"),
        CheckConstraint(
            "(target_kind = 'person'     AND target_person_id IS NOT NULL AND target_repo_id IS NULL) OR "
            "(target_kind = 'repository' AND target_repo_id   IS NOT NULL AND target_person_id IS NULL)",
            name="ck_ee_target_oneof",
        ),
        # The actual dedup constraint is a partial unique index added in the migration
        # because COALESCE expressions cannot be expressed in a column-level unique
        # constraint. Index name: uq_ee_observation_dedup.
        Index(
            "idx_ee_target_person_observed",
            "target_person_id", "observed_at",
            postgresql_where="target_kind = 'person'",
        ),
        Index(
            "idx_ee_target_repo_observed",
            "target_repo_id", "observed_at",
            postgresql_where="target_kind = 'repository'",
        ),
        Index("idx_ee_watcher_observed", "watcher_person_id", "observed_at"),
        Index("idx_ee_org_source_observed", "org_id", "source", "observed_at"),
    )


# --- convergence ------------------------------------------------------------

class ConvergenceEventRow(Base):
    __tablename__ = "convergence_event"
    id: Mapped[str] = mapped_column(Text, primary_key=True)
    org_id: Mapped[str] = mapped_column(Text, ForeignKey("org.id"), nullable=False)
    target_person_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("person.id"), nullable=False, index=True
    )
    fired_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    window_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    distinct_member_count: Mapped[int] = mapped_column(Integer, nullable=False)
    member_person_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(UUID(as_uuid=True)), nullable=False
    )
    score: Mapped[float] = mapped_column(Float, nullable=False)
    score_breakdown: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    first_signal_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_signal_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    signal_type_counts: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    evidence: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB)

    __table_args__ = (Index("idx_ce_org_score", "org_id", "score"),)


# --- ported-from-files tables -----------------------------------------------

class AlertRule(Base):
    __tablename__ = "alert_rule"
    org_id: Mapped[str] = mapped_column(Text, ForeignKey("org.id"), nullable=False)
    user_id: Mapped[str] = mapped_column(Text, ForeignKey("app_user.id"), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    __table_args__ = (PrimaryKeyConstraint("org_id", "user_id"),)


class DossierClassification(Base):
    __tablename__ = "dossier_classification"
    convergence_event_id: Mapped[str] = mapped_column(
        Text, ForeignKey("convergence_event.id"), primary_key=True
    )
    classification: Mapped[str] = mapped_column(Text, nullable=False)
    rationale: Mapped[str | None] = mapped_column(Text)
    classified_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class IdentityDecision(Base):
    __tablename__ = "identity_decision"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    person_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("person.id"), nullable=False
    )
    decision_type: Mapped[str] = mapped_column(Text, nullable=False)
    rationale: Mapped[str | None] = mapped_column(Text)
    decided_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    decided_by: Mapped[str | None] = mapped_column(Text)
    __table_args__ = (
        CheckConstraint("decision_type IN ('merge','distinct','override')", name="ck_id_type"),
    )


class FeedbackEvent(Base):
    __tablename__ = "feedback_event"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    org_id: Mapped[str] = mapped_column(Text, ForeignKey("org.id"), nullable=False)
    user_id: Mapped[str] = mapped_column(Text, ForeignKey("app_user.id"), nullable=False)
    target_type: Mapped[str] = mapped_column(Text, nullable=False)
    target_id: Mapped[str] = mapped_column(Text, nullable=False)
    rating: Mapped[str | None] = mapped_column(Text)
    comment: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    __table_args__ = (
        CheckConstraint("target_type IN ('convergence_event','dossier')", name="ck_fe_target_type"),
        Index("idx_fe_target", "target_type", "target_id"),
    )


# --- placeholders for sub-projects #4 / #6 ---------------------------------

class ScraperAccount(Base):
    __tablename__ = "scraper_account"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    credentials: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    daily_quota: Mapped[int | None] = mapped_column(Integer)
    used_today: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    health: Mapped[str] = mapped_column(Text, nullable=False, server_default="healthy")
    ban_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    org_id: Mapped[str | None] = mapped_column(Text, ForeignKey("org.id"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    __table_args__ = (
        CheckConstraint("health IN ('healthy','cooldown','banned')", name="ck_sa_health"),
    )


class HumanReviewQueue(Base):
    __tablename__ = "human_review_queue"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    org_id: Mapped[str] = mapped_column(Text, ForeignKey("org.id"), nullable=False)
    item_type: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="pending")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolution: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    __table_args__ = (
        CheckConstraint("status IN ('pending','resolved','dismissed')", name="ck_hrq_status"),
    )
