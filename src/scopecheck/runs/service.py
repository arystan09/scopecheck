"""Immutable configuration and transactional, idempotent queued run creation."""

import hashlib
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from scopecheck.api.errors import ApiError
from scopecheck.datasets.canonicalize import canonical_json
from scopecheck.datasets.schemas import Fixture
from scopecheck.runs.models import AssistantConfig, DatasetVersion, Run, RunJob


class RequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class ConfigInput(RequestModel):
    mode: Literal["fake"] = "fake"
    fixture: Fixture = "safe"


class Limits(RequestModel):
    max_tool_calls: Annotated[int, Field(strict=True, ge=1, le=100)] = 20


class RunInput(RequestModel):
    dataset_id: UUID
    config_id: UUID
    mode: Literal["fake"] = "fake"
    repetitions: Annotated[int, Field(strict=True, ge=1, le=100)] = 1
    limits: Limits = Field(default_factory=Limits)


def digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


async def create_config(
    session: AsyncSession,
    workspace_id: UUID,
    body: ConfigInput,
) -> tuple[AssistantConfig, bool]:
    return await store_config(session, workspace_id, body.model_dump(mode="json"))


async def store_config(
    session: AsyncSession, workspace_id: UUID, content: dict[str, Any]
) -> tuple[AssistantConfig, bool]:
    content_hash = digest(content)
    record = await session.scalar(
        insert(AssistantConfig)
        .values(
            workspace_id=workspace_id,
            content_hash=content_hash,
            content=content,
        )
        .on_conflict_do_nothing(index_elements=["workspace_id", "content_hash"])
        .returning(AssistantConfig)
    )
    if record is not None:
        return record, True
    existing = await session.scalar(
        select(AssistantConfig).where(
            AssistantConfig.workspace_id == workspace_id,
            AssistantConfig.content_hash == content_hash,
        )
    )
    assert existing is not None
    return existing, False


async def create_run(
    session: AsyncSession,
    workspace_id: UUID,
    body: RunInput,
    key: str,
    code_revision: str,
) -> tuple[Run, bool]:
    dataset = await session.scalar(
        select(DatasetVersion).where(
            DatasetVersion.id == body.dataset_id,
            DatasetVersion.workspace_id == workspace_id,
        )
    )
    config = await session.scalar(
        select(AssistantConfig).where(
            AssistantConfig.id == body.config_id,
            AssistantConfig.workspace_id == workspace_id,
        )
    )
    if dataset is None or config is None:
        raise ApiError(404, "not_found", "Resource not found")
    if config.content.get("mode") != "fake":
        raise ApiError(
            422, "fake_only", "Public run creation requires a fake configuration"
        )
    request_hash = digest(body.model_dump(mode="json"))
    snapshot = {
        "dataset_hash": dataset.content_hash,
        "config_hash": config.content_hash,
        "policy_version": "1",
        "evaluator_version": "1",
        "tool_schema_version": "1",
        "dataset_schema_version": dataset.content["schema_version"],
        "code_revision": code_revision,
        "mode": body.mode,
        "repetitions": body.repetitions,
        "limits": body.limits.model_dump(),
    }
    return await queue_run(
        session, workspace_id, dataset, config, key, request_hash, snapshot
    )


async def queue_run(
    session: AsyncSession,
    workspace_id: UUID,
    dataset: DatasetVersion,
    config: AssistantConfig,
    key: str,
    request_hash: str,
    snapshot: dict[str, Any],
) -> tuple[Run, bool]:
    record = await session.scalar(
        insert(Run)
        .values(
            workspace_id=workspace_id,
            dataset_id=dataset.id,
            config_id=config.id,
            idempotency_key=key,
            request_hash=request_hash,
            snapshot=snapshot,
            status="queued",
            total_cases=len(dataset.content["cases"]) * snapshot["repetitions"],
        )
        .on_conflict_do_nothing(index_elements=["workspace_id", "idempotency_key"])
        .returning(Run)
    )
    if record is not None:
        session.add(
            RunJob(workspace_id=workspace_id, run_id=record.id, status="queued")
        )
        await session.flush()
        return record, True
    # PostgreSQL waits on the conflicting insert; this next statement sees its commit.
    existing = await session.scalar(
        select(Run).where(
            Run.workspace_id == workspace_id,
            Run.idempotency_key == key,
        )
    )
    assert existing is not None
    if existing.request_hash != request_hash:
        raise ApiError(
            409, "idempotency_conflict", "Idempotency key has different input"
        )
    return existing, False
