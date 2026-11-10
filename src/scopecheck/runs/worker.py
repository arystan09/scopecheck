"""Sequential fake execution with short transactions and generation-fenced writes.

Expired leases are intentionally not reclaimed here. Crash recovery belongs to stage 6.
"""

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from scopecheck.datasets.canonicalize import content_hash
from scopecheck.datasets.schemas import Dataset
from scopecheck.db import create_engine
from scopecheck.evaluation.scorer import CaseScore, run_case
from scopecheck.providers.fake import FakeProvider
from scopecheck.runs.models import (
    AssistantConfig,
    CaseAttempt,
    CaseResult,
    DatasetVersion,
    Run,
    RunJob,
)
from scopecheck.runs.service import ConfigInput, digest
from scopecheck.settings import Settings
from scopecheck.version import PACKAGE_VERSION


class LeaseLost(Exception):
    """The caller must stop without persisting or completing its former job."""


@dataclass(frozen=True)
class Claim:
    job_id: UUID
    run_id: UUID
    workspace_id: UUID
    owner: UUID
    generation: int
    dataset: dict[str, Any]
    config: dict[str, Any]
    snapshot: dict[str, Any]


async def claim_job(
    engine: AsyncEngine, owner: UUID, lease_seconds: int = 60
) -> Claim | None:
    if lease_seconds < 1:
        raise ValueError("Lease must be positive")
    async with AsyncSession(engine) as session, session.begin():
        job = await session.scalar(
            select(RunJob)
            .where(RunJob.status == "queued")
            .order_by(RunJob.created_at, RunJob.id)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if job is None:
            return None
        run = await session.get(Run, job.run_id)
        assert run is not None
        dataset = await session.get(DatasetVersion, run.dataset_id)
        config = await session.get(AssistantConfig, run.config_id)
        assert dataset is not None and config is not None
        now = await session.scalar(select(func.clock_timestamp()))
        assert isinstance(now, datetime)
        job.status = run.status = "running"
        run.started_at = now
        job.lease_owner = owner
        job.lease_generation += 1
        job.lease_expires_at = now + timedelta(seconds=lease_seconds)
        return Claim(
            job.id,
            run.id,
            run.workspace_id,
            owner,
            job.lease_generation,
            dataset.content,
            config.content,
            run.snapshot,
        )


async def owned_job(session: AsyncSession, claim: Claim, lease_seconds: int) -> RunJob:
    # Fence each evidence/final write by locking and checking its lease here.
    job = await session.scalar(
        select(RunJob)
        .where(
            RunJob.id == claim.job_id,
            RunJob.run_id == claim.run_id,
            RunJob.workspace_id == claim.workspace_id,
            RunJob.lease_owner == claim.owner,
            RunJob.lease_generation == claim.generation,
            RunJob.status == "running",
            RunJob.lease_expires_at > func.clock_timestamp(),
        )
        .with_for_update()
    )
    if job is None:
        raise LeaseLost
    now = await session.scalar(select(func.clock_timestamp()))
    assert isinstance(now, datetime)
    job.lease_expires_at = now + timedelta(seconds=lease_seconds)
    return job


async def start_attempt(
    engine: AsyncEngine, claim: Claim, case_id: str, repetition: int, lease_seconds: int
) -> None:
    async with AsyncSession(engine) as session, session.begin():
        await owned_job(session, claim, lease_seconds)
        now = await session.scalar(select(func.clock_timestamp()))
        session.add(
            CaseAttempt(
                workspace_id=claim.workspace_id,
                run_id=claim.run_id,
                case_id=case_id,
                repetition=repetition,
                lease_generation=claim.generation,
                worker_version=PACKAGE_VERSION,
                status="running",
                started_at=now,
            )
        )


async def finish_attempt(
    engine: AsyncEngine,
    claim: Claim,
    case_id: str,
    repetition: int,
    score: CaseScore | None,
    status: str,
    error_code: str | None,
    lease_seconds: int = 60,
) -> None:
    async with AsyncSession(engine) as session, session.begin():
        await owned_job(session, claim, lease_seconds)
        attempt = await session.scalar(
            select(CaseAttempt).where(
                CaseAttempt.workspace_id == claim.workspace_id,
                CaseAttempt.run_id == claim.run_id,
                CaseAttempt.case_id == case_id,
                CaseAttempt.repetition == repetition,
                CaseAttempt.lease_generation == claim.generation,
                CaseAttempt.status == "running",
            )
        )
        if attempt is None:
            raise LeaseLost
        attempt.status = status
        attempt.finished_at = await session.scalar(select(func.clock_timestamp()))
        session.add(
            CaseResult(
                workspace_id=claim.workspace_id,
                run_id=claim.run_id,
                case_id=case_id,
                repetition=repetition,
                status=status,
                score=score.model_dump(mode="json") if score is not None else None,
                error_code=error_code,
            )
        )
        run = await session.get(Run, claim.run_id)
        assert run is not None
        run.completed_cases += 1


async def finish_job(
    engine: AsyncEngine, claim: Claim, error_code: str | None, lease_seconds: int = 60
) -> None:
    async with AsyncSession(engine) as session, session.begin():
        job = await owned_job(session, claim, lease_seconds)
        run = await session.get(Run, claim.run_id)
        assert run is not None
        if error_code is None and run.completed_cases != run.total_cases:
            error_code = "incomplete_execution"
        job.status = run.status = "failed" if error_code else "completed"
        run.error_code = error_code
        run.finished_at = await session.scalar(select(func.clock_timestamp()))


async def execute_claim(
    engine: AsyncEngine, claim: Claim, lease_seconds: int = 60
) -> None:
    try:
        dataset = Dataset.model_validate(claim.dataset)
        config = ConfigInput.model_validate(claim.config)
        if (
            claim.snapshot["mode"] != "fake"
            or claim.snapshot["dataset_schema_version"] != dataset.schema_version
            or any(
                claim.snapshot[key] != "1"
                for key in (
                    "policy_version",
                    "evaluator_version",
                    "tool_schema_version",
                )
            )
            or claim.snapshot["dataset_hash"] != content_hash(dataset)
            or claim.snapshot["config_hash"] != digest(config.model_dump(mode="json"))
        ):
            await finish_job(engine, claim, "incompatible_snapshot", lease_seconds)
            return
        limit = claim.snapshot["limits"]["max_tool_calls"]
        failed = False
        for repetition in range(1, claim.snapshot["repetitions"] + 1):
            for case in dataset.cases:
                await start_attempt(
                    engine, claim, case.case_id, repetition, lease_seconds
                )
                trace = case.traces[config.fixture]
                status, error = (
                    ("limited", "tool_call_limit")
                    if len(trace) > limit
                    else ("completed", None)
                )
                try:
                    # No session or transaction is alive while the sandbox executes.
                    score = await asyncio.to_thread(
                        run_case, case, FakeProvider(trace[:limit])
                    )
                except Exception:
                    score, status, error = None, "failed", "execution_failed"
                failed |= status != "completed"
                await finish_attempt(
                    engine,
                    claim,
                    case.case_id,
                    repetition,
                    score,
                    status,
                    error,
                    lease_seconds,
                )
        await finish_job(
            engine, claim, "incomplete_execution" if failed else None, lease_seconds
        )
    except LeaseLost:
        return
    except Exception:
        # Do not persist exception text: it may contain scenario content or credentials.
        try:
            await finish_job(engine, claim, "execution_failed", lease_seconds)
        except LeaseLost:
            return


async def run_one(
    engine: AsyncEngine, owner: UUID | None = None, lease_seconds: int = 60
) -> bool:
    claim = await claim_job(engine, owner or uuid4(), lease_seconds)
    if claim is None:
        return False
    await execute_claim(engine, claim, lease_seconds)
    return True


async def serve(*, once: bool = False, poll_seconds: float = 1) -> None:
    engine = create_engine(Settings())
    owner = uuid4()
    try:
        while True:
            worked = await run_one(engine, owner)
            if once:
                return
            if not worked:
                await asyncio.sleep(poll_seconds)
    finally:
        await engine.dispose()
