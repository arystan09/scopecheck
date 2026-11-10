"""Tenant-owned durable records. Composite foreign keys defend direct inserts too."""

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Record:
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )


class Workspace(Record, Base):
    __tablename__ = "workspaces"
    name: Mapped[str] = mapped_column(String(100), unique=True)


class ApiToken(Record, Base):
    __tablename__ = "api_tokens"
    __table_args__ = (
        CheckConstraint("role IN ('reader', 'editor')", name="token_role"),
        UniqueConstraint("workspace_id", "id"),
    )
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id"))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    role: Mapped[str] = mapped_column(String(10))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DatasetVersion(Record, Base):
    __tablename__ = "dataset_versions"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id"),
        UniqueConstraint("workspace_id", "content_hash"),
    )
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id"))
    content_hash: Mapped[str] = mapped_column(String(64))
    content: Mapped[dict[str, Any]] = mapped_column(JSONB)


class AssistantConfig(Record, Base):
    __tablename__ = "assistant_configs"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id"),
        UniqueConstraint("workspace_id", "content_hash"),
    )
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id"))
    content_hash: Mapped[str] = mapped_column(String(64))
    content: Mapped[dict[str, Any]] = mapped_column(JSONB)


class Run(Record, Base):
    __tablename__ = "runs"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id"),
        UniqueConstraint("workspace_id", "idempotency_key"),
        ForeignKeyConstraint(
            ["workspace_id", "dataset_id"],
            ["dataset_versions.workspace_id", "dataset_versions.id"],
        ),
        ForeignKeyConstraint(
            ["workspace_id", "config_id"],
            ["assistant_configs.workspace_id", "assistant_configs.id"],
        ),
        CheckConstraint(
            "status IN ('queued', 'running', 'completed', 'failed')", name="run_status"
        ),
    )
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id"))
    dataset_id: Mapped[UUID]
    config_id: Mapped[UUID]
    idempotency_key: Mapped[str] = mapped_column(String(128))
    request_hash: Mapped[str] = mapped_column(String(64))
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(20), default="queued")

    total_cases: Mapped[int] = mapped_column(Integer, server_default="0")
    completed_cases: Mapped[int] = mapped_column(Integer, server_default="0")
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(50))


class RunJob(Record, Base):
    __tablename__ = "run_jobs"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "run_id"], ["runs.workspace_id", "runs.id"]
        ),
        UniqueConstraint("workspace_id", "run_id"),
        CheckConstraint(
            "status IN ('queued', 'running', 'completed', 'failed')", name="job_status"
        ),
        Index("ix_run_jobs_claim", "status", "created_at"),
    )
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id"))
    run_id: Mapped[UUID]
    status: Mapped[str] = mapped_column(String(20), default="queued")

    lease_owner: Mapped[UUID | None]
    lease_generation: Mapped[int] = mapped_column(Integer, server_default="0")
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CaseAttempt(Record, Base):
    __tablename__ = "case_attempts"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "run_id"], ["runs.workspace_id", "runs.id"]
        ),
        UniqueConstraint("workspace_id", "run_id", "case_id", "repetition"),
        CheckConstraint("repetition >= 1", name="attempt_repetition"),
        CheckConstraint(
            "status IN ('running', 'completed', 'limited', 'failed')",
            name="attempt_status",
        ),
    )
    workspace_id: Mapped[UUID]
    run_id: Mapped[UUID]
    case_id: Mapped[str] = mapped_column(String)
    repetition: Mapped[int]
    lease_generation: Mapped[int]
    worker_version: Mapped[str] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(20))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CaseResult(Record, Base):
    __tablename__ = "case_results"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "run_id", "case_id", "repetition"],
            [
                "case_attempts.workspace_id",
                "case_attempts.run_id",
                "case_attempts.case_id",
                "case_attempts.repetition",
            ],
        ),
        UniqueConstraint("workspace_id", "run_id", "case_id", "repetition"),
        CheckConstraint("repetition >= 1", name="result_repetition"),
        CheckConstraint(
            "status IN ('completed', 'limited', 'failed')", name="result_status"
        ),
    )
    workspace_id: Mapped[UUID]
    run_id: Mapped[UUID]
    case_id: Mapped[str] = mapped_column(String)
    repetition: Mapped[int]
    status: Mapped[str] = mapped_column(String(20))
    score: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    provenance: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error_code: Mapped[str | None] = mapped_column(String(50))


class CallReservation(Record, Base):
    __tablename__ = "call_reservations"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "run_id", "case_id", "repetition"],
            [
                "case_attempts.workspace_id",
                "case_attempts.run_id",
                "case_attempts.case_id",
                "case_attempts.repetition",
            ],
        ),
        UniqueConstraint("workspace_id", "run_id", "case_id", "repetition", "sequence"),
        CheckConstraint("sequence BETWEEN 1 AND 4", name="reservation_sequence"),
        CheckConstraint("amount_microusd > 0", name="reservation_positive"),
    )
    workspace_id: Mapped[UUID]
    run_id: Mapped[UUID]
    case_id: Mapped[str] = mapped_column(String)
    repetition: Mapped[int]
    sequence: Mapped[int]
    lease_generation: Mapped[int]
    amount_microusd: Mapped[int] = mapped_column(BigInteger)
    price_hash: Mapped[str] = mapped_column(String(64))
    # Reservations are append-only and never released in stage 5, including
    # successful calls. A crash/timeout cannot silently replenish the budget.
