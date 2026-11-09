"""Tenant-owned durable records. Composite foreign keys defend direct inserts too."""

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
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
        CheckConstraint("status = 'queued'", name="run_queued_only"),
    )
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id"))
    dataset_id: Mapped[UUID]
    config_id: Mapped[UUID]
    idempotency_key: Mapped[str] = mapped_column(String(128))
    request_hash: Mapped[str] = mapped_column(String(64))
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(20), default="queued")


class RunJob(Record, Base):
    __tablename__ = "run_jobs"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "run_id"], ["runs.workspace_id", "runs.id"]
        ),
        UniqueConstraint("workspace_id", "run_id"),
        CheckConstraint("status = 'queued'", name="job_queued_only"),
    )
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id"))
    run_id: Mapped[UUID]
    status: Mapped[str] = mapped_column(String(20), default="queued")
