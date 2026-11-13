"""Bounded execution with database-time leases, takeover, and fenced writes."""

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from openai import __version__ as OPENAI_SDK_VERSION
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from scopecheck import telemetry
from scopecheck.datasets.canonicalize import content_hash
from scopecheck.datasets.schemas import Case, Dataset
from scopecheck.db import create_engine
from scopecheck.evaluation.live import run_live_case
from scopecheck.evaluation.scorer import CaseScore, run_case
from scopecheck.providers.base import Turn
from scopecheck.providers.config import LiveConfig
from scopecheck.providers.fake import FakeProvider
from scopecheck.providers.openai import (
    TOOL_SCHEMA_HASH,
    OpenAIProvider,
    client_for_worker,
    generation_settings,
)
from scopecheck.retention import maintain
from scopecheck.runs.budget import BudgetExceeded, reserve_call, settle_call
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

LEASE_SECONDS = 30
HEARTBEAT_SECONDS = 5
RUN_SECONDS = 300


class LeaseLost(Exception):
    """No further evidence, final writes, or requests belong to this generation."""


class RunDeadlineExceeded(Exception):
    pass


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
    trace_parent: str | None = None


async def claim_job(
    engine: AsyncEngine, owner: UUID, lease_seconds: int = LEASE_SECONDS
) -> Claim | None:
    if lease_seconds < 1:
        raise ValueError("Lease must be positive")
    async with AsyncSession(engine) as session, session.begin():
        # Serialize concurrent claims by the same worker identity, not all workers.
        await session.execute(select(func.pg_advisory_xact_lock(owner.int % (2**63))))
        active = await session.scalar(
            select(RunJob.id)
            .where(
                RunJob.lease_owner == owner,
                RunJob.status == "running",
                RunJob.lease_expires_at > func.clock_timestamp(),
            )
            .limit(1)
        )
        if active is not None:
            return None
        job = await session.scalar(
            select(RunJob)
            .where(
                or_(
                    RunJob.status == "queued",
                    and_(
                        RunJob.status == "running",
                        or_(
                            RunJob.lease_expires_at <= func.clock_timestamp(),
                            RunJob.lease_expires_at.is_(None),
                        ),
                    ),
                )
            )
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
        run.started_at = run.started_at or now
        job.deadline_at = job.deadline_at or now + timedelta(seconds=RUN_SECONDS)
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
            job.trace_parent,
        )


async def owned_job(
    session: AsyncSession, claim: Claim, lease_seconds: int = LEASE_SECONDS
) -> RunJob:
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


async def remaining_run_seconds(
    engine: AsyncEngine, claim: Claim, lease_seconds: int = LEASE_SECONDS
) -> float:
    async with AsyncSession(engine) as session, session.begin():
        job = await owned_job(session, claim, lease_seconds)
        now = await session.scalar(select(func.clock_timestamp()))
        assert isinstance(now, datetime) and job.deadline_at is not None
        return max(0, (job.deadline_at - now).total_seconds())


async def heartbeat(
    engine: AsyncEngine,
    claim: Claim,
    lease_seconds: int = LEASE_SECONDS,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    while True:
        await sleep(HEARTBEAT_SECONDS)
        async with AsyncSession(engine) as session, session.begin():
            await owned_job(session, claim, lease_seconds)


def case_predicates(claim: Claim, case_id: str, repetition: int) -> tuple[Any, ...]:
    return (
        CaseAttempt.workspace_id == claim.workspace_id,
        CaseAttempt.run_id == claim.run_id,
        CaseAttempt.case_id == case_id,
        CaseAttempt.repetition == repetition,
    )


async def start_attempt(
    engine: AsyncEngine,
    claim: Claim,
    case_id: str,
    repetition: int,
    lease_seconds: int = LEASE_SECONDS,
) -> bool:
    async with AsyncSession(engine) as session, session.begin():
        job = await owned_job(session, claim, lease_seconds)
        now = await session.scalar(select(func.clock_timestamp()))
        assert isinstance(now, datetime)
        if job.deadline_at is not None and job.deadline_at <= now:
            raise RunDeadlineExceeded
        result = await session.scalar(
            select(CaseResult.id).where(
                CaseResult.workspace_id == claim.workspace_id,
                CaseResult.run_id == claim.run_id,
                CaseResult.case_id == case_id,
                CaseResult.repetition == repetition,
            )
        )
        if result is not None:
            return False
        attempt = await session.scalar(
            select(CaseAttempt).where(*case_predicates(claim, case_id, repetition))
        )
        if attempt is not None and attempt.lease_generation == claim.generation:
            raise ValueError("Attempt already started in this generation")
        active = await session.scalar(
            select(func.count())
            .select_from(CaseAttempt)
            .where(
                CaseAttempt.run_id == claim.run_id,
                CaseAttempt.status == "running",
                CaseAttempt.lease_generation == claim.generation,
            )
        )
        if int(active or 0) >= 2:
            raise ValueError("Two case slots already occupied")
        if attempt is None:
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
            return True
        attempt.history = [
            *attempt.history,
            {
                "attempt_number": attempt.attempt_number,
                "lease_generation": attempt.lease_generation,
                "started_at": attempt.started_at.isoformat(),
                "ended_at": now.isoformat(),
                "status": "interrupted",
                "worker_version": attempt.worker_version,
            },
        ]
        attempt.lease_generation = claim.generation
        attempt.worker_version = PACKAGE_VERSION
        if attempt.attempt_number >= 2:
            attempt.status, attempt.finished_at = "failed", now
            session.add(
                CaseResult(
                    workspace_id=claim.workspace_id,
                    run_id=claim.run_id,
                    case_id=case_id,
                    repetition=repetition,
                    status="failed",
                    score=None,
                    error_code="attempt_limit",
                )
            )
            run = await session.get(Run, claim.run_id)
            assert run is not None
            run.completed_cases += 1
            return False
        attempt.attempt_number += 1
        attempt.status, attempt.started_at, attempt.finished_at = "running", now, None
        return True


async def finish_attempt(
    engine: AsyncEngine,
    claim: Claim,
    case_id: str,
    repetition: int,
    score: CaseScore | None,
    status: str,
    error_code: str | None,
    lease_seconds: int = LEASE_SECONDS,
    provenance: dict[str, Any] | None = None,
) -> None:
    async with AsyncSession(engine) as session, session.begin():
        await owned_job(session, claim, lease_seconds)
        attempt = await session.scalar(
            select(CaseAttempt).where(
                *case_predicates(claim, case_id, repetition),
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
                provenance=provenance,
            )
        )
        run = await session.get(Run, claim.run_id)
        assert run is not None
        run.completed_cases += 1


async def finish_job(
    engine: AsyncEngine,
    claim: Claim,
    error_code: str | None,
    lease_seconds: int = LEASE_SECONDS,
) -> None:
    async with AsyncSession(engine) as session, session.begin():
        job = await owned_job(session, claim, lease_seconds)
        run = await session.get(Run, claim.run_id)
        assert run is not None
        if error_code:
            pending = list(
                (
                    await session.scalars(
                        select(CaseAttempt).where(
                            CaseAttempt.run_id == run.id,
                            CaseAttempt.status == "running",
                        )
                    )
                ).all()
            )
            now = await session.scalar(select(func.clock_timestamp()))
            for attempt in pending:
                attempt.status, attempt.finished_at = "failed", now
                session.add(
                    CaseResult(
                        workspace_id=claim.workspace_id,
                        run_id=run.id,
                        case_id=attempt.case_id,
                        repetition=attempt.repetition,
                        status="failed",
                        score=None,
                        error_code=error_code,
                    )
                )
                run.completed_cases += 1
            await session.flush()
        results = list(
            (
                await session.scalars(
                    select(CaseResult).where(CaseResult.run_id == run.id)
                )
            ).all()
        )
        if any(r.error_code == "budget_exhausted" for r in results):
            error_code = "budget_exhausted"
        elif error_code is None and (
            len(results) != run.total_cases
            or any(r.status != "completed" for r in results)
        ):
            error_code = "incomplete_execution"
        status = "completed"
        if error_code == "budget_exhausted":
            status = "budget_exhausted"
        elif error_code:
            status = "completed_with_errors" if results else "failed"
        job.status = run.status = status
        run.error_code = error_code
        run.finished_at = await session.scalar(select(func.clock_timestamp()))


async def execute_claim(
    engine: AsyncEngine, claim: Claim, lease_seconds: int = LEASE_SECONDS
) -> None:
    with telemetry.span(
        "worker.run",
        parent=claim.trace_parent,
        run_id=str(claim.run_id),
        mode=claim.snapshot.get("mode", "fake"),
    ):
        await _execute_claim(engine, claim, lease_seconds)


async def _execute_claim(
    engine: AsyncEngine, claim: Claim, lease_seconds: int = LEASE_SECONDS
) -> None:
    async def bounded() -> None:
        await remaining_run_seconds(engine, claim, lease_seconds)
        # A crash after the last result needs only finalization, even if the
        # execution deadline passed or provider credentials were since removed.
        async with AsyncSession(engine) as session:
            run = await session.get(Run, claim.run_id)
            assert run is not None
            count = await session.scalar(
                select(func.count())
                .select_from(CaseResult)
                .where(
                    CaseResult.run_id == run.id,
                    CaseResult.workspace_id == claim.workspace_id,
                )
            )
            exhausted = await session.scalar(
                select(CaseResult.id)
                .where(
                    CaseResult.run_id == run.id,
                    CaseResult.error_code == "budget_exhausted",
                )
                .limit(1)
            )
            finalized_cases = count == run.total_cases or exhausted is not None
        if finalized_cases:
            await finish_job(engine, claim, None, lease_seconds)
            return
        remaining = await remaining_run_seconds(engine, claim, lease_seconds)
        async with asyncio.timeout(remaining):
            await execute_cases(engine, claim, lease_seconds)

    work = asyncio.create_task(bounded())
    pulse = asyncio.create_task(heartbeat(engine, claim, lease_seconds))
    try:
        done, _ = await asyncio.wait({work, pulse}, return_when=asyncio.FIRST_COMPLETED)
        if pulse in done:
            work.cancel()
            await asyncio.gather(work, return_exceptions=True)
            await pulse  # Stop execution before attempting any error finalization.
        await work
    except LeaseLost:
        pass
    except (TimeoutError, RunDeadlineExceeded):
        with suppress(LeaseLost):
            await finish_job(engine, claim, "run_deadline_exceeded", lease_seconds)
    except Exception:
        # Never store provider exception text or scenario/credential content.
        with suppress(LeaseLost):
            await finish_job(engine, claim, "execution_failed", lease_seconds)
    finally:
        for task in (work, pulse):
            task.cancel()
        await asyncio.gather(work, pulse, return_exceptions=True)


async def execute_cases(engine: AsyncEngine, claim: Claim, lease_seconds: int) -> None:
    if Settings().public_demo:
        from scopecheck.datasets.demo import DEMO_WORKSPACE, fixed_hash
        from scopecheck.datasets.splits import MANIFEST_HASH

        if (
            claim.workspace_id != DEMO_WORKSPACE
            or claim.snapshot.get("mode") != "fake"
            or claim.snapshot.get("demo_manifest") != MANIFEST_HASH
            or claim.snapshot.get("dataset_hash") != fixed_hash()
            or claim.config != {"mode": "fake", "fixture": "safe"}
            or claim.snapshot.get("repetitions") != 1
            or claim.snapshot.get("limits") != {"max_tool_calls": 20}
        ):
            await finish_job(engine, claim, "public_demo_only", lease_seconds)
            return
    dataset = Dataset.model_validate(claim.dataset)
    config = (
        LiveConfig.model_validate(claim.config)
        if claim.snapshot["mode"] == "live"
        else ConfigInput.model_validate(claim.config)
    )
    if (
        claim.snapshot["mode"] != config.mode
        or claim.snapshot["dataset_schema_version"] != dataset.schema_version
        or any(
            claim.snapshot[key] != "1"
            for key in ("policy_version", "evaluator_version", "tool_schema_version")
        )
        or claim.snapshot["dataset_hash"] != content_hash(dataset)
        or claim.snapshot["config_hash"] != digest(config.model_dump(mode="json"))
    ):
        await finish_job(engine, claim, "incompatible_snapshot", lease_seconds)
        return
    if isinstance(config, LiveConfig):
        if (
            claim.snapshot.get("provider_sdk_version") != OPENAI_SDK_VERSION
            or claim.snapshot.get("tool_schema_hash") != TOOL_SCHEMA_HASH
            or claim.snapshot.get("generation_settings") != generation_settings(config)
            or claim.snapshot.get("owner_authorized") is not True
            or claim.snapshot.get("limits")
            != {
                "max_tool_calls": 6,
                "max_model_requests": 4,
                "case_deadline_seconds": 30,
            }
            or claim.snapshot.get("repetitions") != 1
        ):
            await finish_job(engine, claim, "incompatible_snapshot", lease_seconds)
            return
        config.price.require_current()
        key = os.environ.get("SCOPECHECK_OPENAI_API_KEY", "")
        if not key.strip():
            await finish_job(engine, claim, "missing_credentials", lease_seconds)
            return
        async with client_for_worker(key) as client:
            await execute_cases_concurrently(
                engine,
                claim,
                dataset,
                config,
                lease_seconds,
                OpenAIProvider(client, config),
            )
    else:
        await execute_cases_concurrently(engine, claim, dataset, config, lease_seconds)
    await finish_job(engine, claim, None, lease_seconds)


async def execute_cases_concurrently(
    engine: AsyncEngine,
    claim: Claim,
    dataset: Dataset,
    config: ConfigInput | LiveConfig,
    lease_seconds: int,
    provider: OpenAIProvider | None = None,
) -> None:
    stop_budget = asyncio.Event()
    async with AsyncSession(engine) as session:
        exhausted = await session.scalar(
            select(CaseResult.id)
            .where(
                CaseResult.run_id == claim.run_id,
                CaseResult.error_code == "budget_exhausted",
            )
            .limit(1)
        )
        if exhausted:
            return
    # Two consumers, rather than one task for every possible case/repetition.
    cases = iter(
        (case, repetition)
        for repetition in range(1, claim.snapshot["repetitions"] + 1)
        for case in dataset.cases
    )

    async def consume() -> None:
        for case, repetition in cases:
            if stop_budget.is_set():
                return
            if not await start_attempt(
                engine, claim, case.case_id, repetition, lease_seconds
            ):
                continue
            await execute_case(
                engine,
                claim,
                case,
                repetition,
                config,
                lease_seconds,
                provider,
                stop_budget,
            )

    tasks = [asyncio.create_task(consume()) for _ in range(2)]
    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def observed_fake(case: Case, provider: FakeProvider) -> CaseScore:
    with telemetry.span("provider.request", mode="fake") as current:
        try:
            result = run_case(case, provider)
            current.set_attribute("status", "final_response")
            return result
        except TimeoutError:
            current.set_attribute("error", "timeout")
            raise
        except Exception:
            current.set_attribute("error", "execution_failed")
            raise


async def execute_case(
    engine: AsyncEngine,
    claim: Claim,
    case: Case,
    repetition: int,
    config: ConfigInput | LiveConfig,
    lease_seconds: int,
    provider: OpenAIProvider | None,
    stop_budget: asyncio.Event,
) -> None:
    with telemetry.span("case.execute", run_id=str(claim.run_id), mode=config.mode):
        await _execute_case(
            engine,
            claim,
            case,
            repetition,
            config,
            lease_seconds,
            provider,
            stop_budget,
        )


async def _execute_case(
    engine: AsyncEngine,
    claim: Claim,
    case: Case,
    repetition: int,
    config: ConfigInput | LiveConfig,
    lease_seconds: int,
    provider: OpenAIProvider | None,
    stop_budget: asyncio.Event,
) -> None:
    provenance = None
    if isinstance(config, LiveConfig):
        assert provider is not None
        calls: list[dict[str, Any]] = []

        async def reserve(sequence: int) -> UUID:
            if stop_budget.is_set():
                raise BudgetExceeded
            return await reserve_call(
                engine, claim, case.case_id, repetition, sequence, config
            )

        async def record(reservation: UUID, turn: Turn) -> None:
            await settle_call(engine, claim, reservation, config, turn)
            calls.append(
                {
                    "reservation_id": str(reservation),
                    "outcome": turn.outcome,
                    "input_tokens": turn.input_tokens,
                    "output_tokens": turn.output_tokens,
                    "returned_model": turn.model,
                }
            )

        result = await run_live_case(
            case,
            provider,
            reserve,
            record,
            remaining_seconds=await remaining_run_seconds(engine, claim, lease_seconds),
        )
        score, status = result.score, result.status
        error = None if status == "completed" else result.outcome
        if result.outcome == "budget_exhausted":
            stop_budget.set()
        provenance = {
            "mode": "live",
            "provider": "openai",
            "model": config.model,
            "outcome": result.outcome,
            "requests": result.requests,
            "calls": calls,
            "tool_schema_hash": TOOL_SCHEMA_HASH,
        }
    else:
        limit = claim.snapshot["limits"]["max_tool_calls"]
        trace = case.traces[config.fixture]
        status, error = (
            ("limited", "tool_call_limit")
            if len(trace) > limit
            else ("completed", None)
        )
        try:
            async with asyncio.timeout(30):
                score = await asyncio.to_thread(
                    observed_fake, case, FakeProvider(trace[:limit])
                )
        except TimeoutError:
            score, status, error = None, "limited", "timeout"
        except Exception:
            score, status, error = None, "failed", "execution_failed"
    from opentelemetry.trace import get_current_span

    get_current_span().set_attribute("status", status)
    get_current_span().set_attribute("error", telemetry.error_code(error))
    await finish_attempt(
        engine,
        claim,
        case.case_id,
        repetition,
        score,
        status,
        error,
        lease_seconds,
        provenance,
    )


async def run_one(
    engine: AsyncEngine, owner: UUID | None = None, lease_seconds: int = LEASE_SECONDS
) -> bool:
    claim = await claim_job(engine, owner or uuid4(), lease_seconds)
    if claim is None:
        return False
    await execute_claim(engine, claim, lease_seconds)
    return True


async def serve(*, once: bool = False, poll_seconds: float = 1) -> None:
    settings = Settings()
    telemetry.configure(settings.trace_directory, settings.collector)
    engine = create_engine(settings)
    last_prune: float | None = None
    owner = uuid4()
    try:
        while True:
            now = asyncio.get_running_loop().time()
            if settings.trace_directory is not None and (
                last_prune is None or now - last_prune > 3600
            ):
                try:
                    await asyncio.to_thread(
                        maintain,
                        settings.trace_directory,
                        settings.trace_retention_days,
                    )
                except OSError:
                    logging.getLogger("scopecheck").warning(
                        "telemetry.retention",
                        extra={"telemetry": {"status": "error", "error": "other"}},
                    )
                last_prune = now
            worked = await run_one(engine, owner)
            if once:
                return
            if not worked:
                await asyncio.sleep(poll_seconds)
    finally:
        await engine.dispose()
