"""Workspace scope comes exclusively from an authenticated bearer token."""

from collections.abc import AsyncIterator
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from scopecheck.api.errors import ApiError
from scopecheck.auth import Principal, authenticate
from scopecheck.datasets.service import MAX_IMPORT_BYTES, import_dataset
from scopecheck.db import SCHEMA_REVISION
from scopecheck.runs.models import (
    AssistantConfig,
    CaseAttempt,
    CaseResult,
    DatasetVersion,
    Run,
)
from scopecheck.runs.reports import compare, owned_run, report
from scopecheck.runs.service import ConfigInput, RunInput, create_config, create_run

router = APIRouter()


async def session_dependency(request: Request) -> AsyncIterator[AsyncSession]:
    engine: AsyncEngine | None = request.app.state.engine
    if engine is None:
        raise ApiError(503, "database_unavailable", "Database is unavailable")
    async with AsyncSession(engine, expire_on_commit=False) as session:
        async with session.begin():
            yield session


Session = Annotated[AsyncSession, Depends(session_dependency, scope="function")]


async def principal_dependency(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> Principal:
    if authorization is None:
        raise ApiError(401, "unauthorized", "Valid bearer token required")
    parts = authorization.split()
    if len(parts) != 2 or parts[0].lower() != "bearer" or len(parts[1]) != 43:
        raise ApiError(401, "unauthorized", "Valid bearer token required")
    engine: AsyncEngine | None = request.app.state.engine
    if engine is None:
        raise ApiError(503, "database_unavailable", "Database is unavailable")
    async with AsyncSession(engine) as session:
        principal = await authenticate(session, parts[1])
    if principal is None:
        raise ApiError(401, "unauthorized", "Valid bearer token required")
    return principal


PrincipalDep = Annotated[Principal, Depends(principal_dependency)]


async def editor_dependency(principal: PrincipalDep) -> Principal:
    if principal.role != "editor":
        raise ApiError(403, "forbidden", "Editor role required")
    return principal


Editor = Annotated[Principal, Depends(editor_dependency)]


def dataset_view(record: DatasetVersion, *, detail: bool = True) -> dict[str, Any]:
    result = {
        "id": record.id,
        "content_hash": record.content_hash,
        "created_at": record.created_at,
        "case_count": len(record.content["cases"]),
    }
    if detail:
        result["content"] = record.content
    return result


def config_view(record: AssistantConfig) -> dict[str, Any]:
    return {
        "id": record.id,
        "content_hash": record.content_hash,
        "content": record.content,
        "created_at": record.created_at,
    }


def run_view(record: Run) -> dict[str, Any]:
    return {
        "id": record.id,
        "dataset_id": record.dataset_id,
        "config_id": record.config_id,
        "status": record.status,
        "total_cases": record.total_cases,
        "completed_cases": record.completed_cases,
        "started_at": record.started_at,
        "finished_at": record.finished_at,
        "error_code": record.error_code,
        "snapshot": record.snapshot,
        "created_at": record.created_at,
    }


@router.get("/health/ready")
async def ready(session: Session) -> dict[str, str]:
    revision = await session.scalar(text("SELECT version_num FROM alembic_version"))
    if revision != SCHEMA_REVISION:
        raise ApiError(503, "schema_not_ready", "Database schema is not ready")
    return {"status": "ok"}


@router.post("/api/v1/datasets")
async def post_dataset(
    request: Request, response: Response, editor: Editor, session: Session
) -> dict[str, Any]:
    raw = bytearray()
    async for chunk in request.stream():
        if len(raw) + len(chunk) > MAX_IMPORT_BYTES:
            raise ApiError(413, "dataset_too_large", "Dataset exceeds 1 MiB")
        raw.extend(chunk)
    record, created = await import_dataset(session, editor.workspace_id, bytes(raw))
    response.status_code = 201 if created else 200
    return dataset_view(record)


@router.get("/api/v1/datasets")
async def list_datasets(
    principal: PrincipalDep,
    session: Session,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: UUID | None = None,
) -> dict[str, Any]:
    statement = select(DatasetVersion).where(
        DatasetVersion.workspace_id == principal.workspace_id
    )
    if cursor is not None:
        anchor = await session.scalar(statement.where(DatasetVersion.id == cursor))
        if anchor is None:
            raise ApiError(404, "not_found", "Resource not found")
        statement = statement.where(DatasetVersion.id > cursor)
    rows = list(
        (
            await session.scalars(
                statement.order_by(DatasetVersion.id).limit(limit + 1)
            )
        ).all()
    )
    return {
        "items": [dataset_view(row, detail=False) for row in rows[:limit]],
        "next_cursor": str(rows[limit - 1].id) if len(rows) > limit else None,
    }


@router.get("/api/v1/datasets/{dataset_id}")
async def get_dataset(
    dataset_id: UUID, principal: PrincipalDep, session: Session
) -> dict[str, Any]:
    record = await session.scalar(
        select(DatasetVersion).where(
            DatasetVersion.id == dataset_id,
            DatasetVersion.workspace_id == principal.workspace_id,
        )
    )
    if record is None:
        raise ApiError(404, "not_found", "Resource not found")
    return dataset_view(record)


@router.post("/api/v1/configs")
async def post_config(
    body: ConfigInput, response: Response, editor: Editor, session: Session
) -> dict[str, Any]:
    record, created = await create_config(session, editor.workspace_id, body)
    response.status_code = 201 if created else 200
    return config_view(record)


@router.post("/api/v1/runs")
async def post_run(
    body: RunInput,
    request: Request,
    response: Response,
    editor: Editor,
    session: Session,
    idempotency_key: Annotated[
        str, Header(min_length=1, max_length=128, pattern=r"^[!-~]+$")
    ],
) -> dict[str, Any]:
    record, created = await create_run(
        session,
        editor.workspace_id,
        body,
        idempotency_key,
        request.app.state.settings.code_revision,
    )
    response.status_code = 201 if created else 200
    return run_view(record)


@router.get("/api/v1/runs")
async def list_runs(
    principal: PrincipalDep,
    session: Session,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: UUID | None = None,
) -> dict[str, Any]:
    statement = select(Run).where(Run.workspace_id == principal.workspace_id)
    if cursor is not None:
        anchor = await session.scalar(statement.where(Run.id == cursor))
        if anchor is None:
            raise ApiError(404, "not_found", "Resource not found")
        statement = statement.where(Run.id > cursor)
    rows = list(
        (await session.scalars(statement.order_by(Run.id).limit(limit + 1))).all()
    )
    return {
        "items": [run_view(row) for row in rows[:limit]],
        "next_cursor": str(rows[limit - 1].id) if len(rows) > limit else None,
    }


@router.get("/api/v1/runs/{run_id}")
async def get_run(
    run_id: UUID, principal: PrincipalDep, session: Session
) -> dict[str, Any]:
    record = await session.scalar(
        select(Run).where(
            Run.id == run_id,
            Run.workspace_id == principal.workspace_id,
        )
    )
    if record is None:
        raise ApiError(404, "not_found", "Resource not found")
    return run_view(record)


@router.get("/api/v1/runs/{run_id}/report")
async def get_report(
    run_id: UUID, principal: PrincipalDep, session: Session
) -> dict[str, Any]:
    return await report(
        session, await owned_run(session, principal.workspace_id, run_id)
    )


@router.get("/api/v1/runs/{run_id}/cases")
async def get_cases(
    run_id: UUID,
    principal: PrincipalDep,
    session: Session,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: UUID | None = None,
) -> dict[str, Any]:
    await owned_run(session, principal.workspace_id, run_id)
    statement = select(CaseAttempt).where(
        CaseAttempt.workspace_id == principal.workspace_id, CaseAttempt.run_id == run_id
    )
    if cursor is not None:
        if await session.scalar(statement.where(CaseAttempt.id == cursor)) is None:
            raise ApiError(404, "not_found", "Resource not found")
        statement = statement.where(CaseAttempt.id > cursor)
    attempts = list(
        (
            await session.scalars(statement.order_by(CaseAttempt.id).limit(limit + 1))
        ).all()
    )
    items = []
    for attempt in attempts[:limit]:
        result = await session.scalar(
            select(CaseResult).where(
                CaseResult.workspace_id == principal.workspace_id,
                CaseResult.run_id == run_id,
                CaseResult.case_id == attempt.case_id,
                CaseResult.repetition == attempt.repetition,
            )
        )
        items.append(
            {
                "id": attempt.id,
                "case_id": attempt.case_id,
                "repetition": attempt.repetition,
                "status": attempt.status,
                "started_at": attempt.started_at,
                "finished_at": attempt.finished_at,
                "worker_version": attempt.worker_version,
                "attempt_number": attempt.attempt_number,
                "attempt_history": attempt.history,
                "score": result.score if result is not None else None,
                "provenance": result.provenance if result is not None else None,
                "error_code": result.error_code if result is not None else None,
            }
        )
    return {
        "items": items,
        "next_cursor": str(attempts[limit - 1].id) if len(attempts) > limit else None,
    }


@router.get("/api/v1/comparisons")
async def get_comparison(
    baseline: UUID, candidate: UUID, principal: PrincipalDep, session: Session
) -> dict[str, Any]:
    return await compare(session, principal.workspace_id, baseline, candidate)
