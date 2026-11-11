import asyncio
import os
import sys
import threading
from uuid import uuid4

import pytest
from recovery_helpers import race_processes
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from test_worker import launch, report

from scopecheck.datasets.schemas import Dataset
from scopecheck.evaluation.scorer import run_case
from scopecheck.providers.fake import FakeProvider
from scopecheck.runs import worker
from scopecheck.runs.models import CaseAttempt, CaseResult, Run

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def expire(database, claim):
    async with AsyncSession(database.engine) as session, session.begin():
        await session.execute(
            text(
                "UPDATE run_jobs SET lease_expires_at = clock_timestamp() - interval "
                "'1 second' WHERE id=:id"
            ),
            {"id": claim.job_id},
        )


async def test_independent_process_claim_race_and_database_lease(api, database):
    run = await launch(api)
    program = """
import asyncio, json
from uuid import UUID
from sqlalchemy import text
from scopecheck.db import create_engine
from scopecheck.settings import Settings
from scopecheck.runs.worker import claim_job
print("ready", flush=True)
payload=json.loads(input())
async def main():
    engine=create_engine(Settings())
    async with engine.connect() as connection:
        pid=await connection.scalar(text("SELECT pg_backend_pid()"))
    claim=await claim_job(engine, UUID(payload["owner"]))
    print(json.dumps({"pid":pid,"run":str(claim.run_id) if claim else None}),
          flush=True)
    await engine.dispose()
asyncio.run(main())
"""
    results = await race_processes(
        database.url, program, [{"owner": str(uuid4())}, {"owner": str(uuid4())}]
    )
    assert len({r["pid"] for r in results}) == 2
    assert [r["run"] for r in results].count(run["id"]) == 1
    async with database.engine.connect() as connection:
        seconds = await connection.scalar(
            text(
                "SELECT extract(epoch FROM lease_expires_at-clock_timestamp()) FROM "
                "run_jobs"
            )
        )
        assert 20 < seconds <= 30
        assert (
            await connection.scalar(text("SELECT lease_generation FROM run_jobs")) == 1
        )


async def test_one_run_per_worker_and_atomic_takeover(api, database):
    await launch(api)
    await launch(api)
    owner = uuid4()
    claims = await asyncio.gather(
        *(worker.claim_job(database.engine, owner) for _ in range(2))
    )
    assert sum(c is not None for c in claims) == 1
    first = next(c for c in claims if c)
    # Finish the other queued run, leaving only the expired candidate.
    other = await worker.claim_job(database.engine, uuid4())
    await worker.execute_claim(database.engine, other)
    await expire(database, first)
    claims = await asyncio.gather(
        worker.claim_job(database.engine, uuid4()),
        worker.claim_job(database.engine, uuid4()),
    )
    assert sum(c is not None for c in claims) == 1
    current = next(c for c in claims if c)
    assert current.run_id == first.run_id and current.generation == first.generation + 1
    with pytest.raises(worker.LeaseLost):
        await worker.finish_job(database.engine, first, None)


async def test_completed_reused_incomplete_reset_and_no_duplicate_result(
    api, database, monkeypatch
):
    run = await launch(api)
    first = await worker.claim_job(database.engine, uuid4())
    cases = Dataset.model_validate(first.dataset).cases
    await worker.start_attempt(database.engine, first, cases[0].case_id, 1)
    score = run_case(cases[0], FakeProvider(cases[0].traces["safe"]))
    await worker.finish_attempt(
        database.engine, first, cases[0].case_id, 1, score, "completed", None
    )
    await worker.start_attempt(database.engine, first, cases[1].case_id, 1)
    # Simulate a partial execution that changed its local address before crashing.
    partial = cases[1].initial_state.fresh()
    partial.orders[0].address = "uncommitted partial state"
    await expire(database, first)
    current = await worker.claim_job(database.engine, uuid4())
    seen = []
    original = worker.run_case

    def observe(case, provider):
        seen.append(case.case_id)
        assert case.initial_state == next(
            c.initial_state for c in cases if c.case_id == case.case_id
        )
        return original(case, provider)

    monkeypatch.setattr(worker, "run_case", observe)
    await worker.execute_claim(database.engine, current)
    outcome = await report(api, run)
    assert outcome["complete"] and outcome["legitimate_task_success"]["numerator"] == 6
    assert cases[0].case_id not in seen and seen.count(cases[1].case_id) == 1
    async with AsyncSession(database.engine) as session:
        assert await session.scalar(select(func.count()).select_from(CaseResult)) == 12
        attempt = await session.scalar(
            select(CaseAttempt).where(CaseAttempt.case_id == cases[1].case_id)
        )
        assert (
            attempt.attempt_number == 2
            and attempt.history[0]["status"] == "interrupted"
        )
    with pytest.raises(worker.LeaseLost):
        await worker.finish_attempt(
            database.engine, first, cases[1].case_id, 1, score, "completed", None
        )
    assert not await worker.run_one(database.engine)


async def test_two_attempt_limit_terminal_errors_are_not_passes(api, database):
    run = await launch(api)
    first = await worker.claim_job(database.engine, uuid4())
    case_id = first.dataset["cases"][0]["case_id"]
    await worker.start_attempt(database.engine, first, case_id, 1)
    await expire(database, first)
    second = await worker.claim_job(database.engine, uuid4())
    await worker.start_attempt(database.engine, second, case_id, 1)
    await expire(database, second)
    third = await worker.claim_job(database.engine, uuid4())
    await worker.execute_claim(database.engine, third)
    outcome = await report(api, run)
    assert outcome["status"] == "completed_with_errors" and not outcome["complete"]
    assert outcome["failed_cases"] == 1 and outcome["completed_cases"] == 11
    assert outcome["legitimate_task_success"]["numerator"] == 5
    async with AsyncSession(database.engine) as session:
        attempt = await session.scalar(
            select(CaseAttempt).where(CaseAttempt.case_id == case_id)
        )
        assert attempt.attempt_number == 2 and len(attempt.history) == 2
        result = await session.scalar(
            select(CaseResult).where(CaseResult.case_id == case_id)
        )
        assert result.error_code == "attempt_limit" and result.score is None


async def test_heartbeat_uses_database_clock_and_fences_loss(api, database):
    await launch(api)
    claim = await worker.claim_job(database.engine, uuid4())
    wake, sleeping = asyncio.Event(), asyncio.Event()

    async def clock_sleep(seconds):
        assert seconds == 5
        sleeping.set()
        await wake.wait()
        wake.clear()

    async with AsyncSession(database.engine) as session, session.begin():
        await session.execute(
            text(
                "UPDATE run_jobs SET lease_expires_at = clock_timestamp() + interval "
                "'10 seconds'"
            )
        )
    pulse = asyncio.create_task(
        worker.heartbeat(database.engine, claim, sleep=clock_sleep)
    )
    try:
        await asyncio.wait_for(sleeping.wait(), 3)
        sleeping.clear()
        wake.set()
        await asyncio.wait_for(sleeping.wait(), 3)  # renewal completed, waiting again
        async with database.engine.connect() as connection:
            remaining = await connection.scalar(
                text(
                    "SELECT extract(epoch FROM lease_expires_at-clock_timestamp()) "
                    "FROM run_jobs"
                )
            )
            assert 25 < remaining <= 30
        await expire(database, claim)
        wake.set()
        with pytest.raises(worker.LeaseLost):
            await asyncio.wait_for(pulse, 3)
    finally:
        pulse.cancel()
        await asyncio.gather(pulse, return_exceptions=True)


async def test_run_deadline_survives_takeover(api, database):
    run = await launch(api)
    first = await worker.claim_job(database.engine, uuid4())
    await worker.start_attempt(
        database.engine, first, first.dataset["cases"][0]["case_id"], 1
    )
    async with AsyncSession(database.engine) as session, session.begin():
        await session.execute(
            text(
                "UPDATE run_jobs SET deadline_at=clock_timestamp()-interval '1 "
                "second', lease_expires_at=clock_timestamp()-interval '1 second'"
            )
        )
    second = await worker.claim_job(database.engine, uuid4())
    await worker.execute_claim(database.engine, second)
    outcome = await report(api, run)
    assert outcome["status"] == "completed_with_errors" and not outcome["complete"]
    assert outcome["failed_cases"] == 1 and outcome["coverage"]["numerator"] == 0
    async with AsyncSession(database.engine) as session:
        stored = await session.get(Run, first.run_id)
        assert stored.error_code == "run_deadline_exceeded"


async def test_two_case_slots_bound_execution(api, database, monkeypatch):
    run = await launch(api)
    original = worker.run_case
    entered = threading.Barrier(2)
    lock = threading.Lock()
    active = maximum = 0

    def blocked(case, provider):
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(active, maximum)
        entered.wait(timeout=5)
        result = original(case, provider)
        with lock:
            active -= 1
        return result

    monkeypatch.setattr(worker, "run_case", blocked)
    await worker.run_one(database.engine)
    assert maximum == 2 and active == 0
    assert (await report(api, run))["complete"]


async def test_worker_process_interruption_recovers_by_deadline(api, database):
    run = await launch(api)
    env = {**os.environ, "SCOPECHECK_DATABASE_URL": database.url}
    env.pop("SCOPECHECK_OPENAI_API_KEY", None)
    code = """
import asyncio, threading
from scopecheck.runs import worker
def interrupted(case, provider):
    print("executing", flush=True)
    threading.Event().wait()
worker.run_case=interrupted
asyncio.run(worker.serve(once=True))
"""
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        code,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        assert await asyncio.wait_for(process.stdout.readline(), 10) == b"executing\n"
        process.kill()
        await process.wait()
        async with AsyncSession(database.engine) as session, session.begin():
            # Deterministic failure injection advances DB lease expiry, not wall sleeps.
            await session.execute(
                text(
                    "UPDATE run_jobs SET lease_expires_at=clock_timestamp()-interval "
                    "'1 second', deadline_at=clock_timestamp()+interval '10 seconds'"
                )
            )
        recovery = await asyncio.create_subprocess_exec(
            "scopecheck",
            "worker",
            "--once",
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            await asyncio.wait_for(recovery.communicate(), 10)
            assert recovery.returncode == 0
        finally:
            if recovery.returncode is None:
                recovery.kill()
                await recovery.wait()
        outcome = await report(api, run)
        assert outcome["complete"] and outcome["persisted_cases"] == 12
        async with database.engine.connect() as connection:
            assert await connection.scalar(
                text(
                    "SELECT bool_and(finished_at < deadline_at) FROM runs JOIN "
                    "run_jobs ON runs.id=run_jobs.run_id"
                )
            )
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


async def test_heartbeat_loss_cancels_execution_before_writing(
    api, database, monkeypatch
):
    run = await launch(api)
    claim = await worker.claim_job(database.engine, uuid4())
    entered, cancelled, wake = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def blocked(*args):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    original_heartbeat = worker.heartbeat

    async def sleep(seconds):
        assert seconds == 5
        await wake.wait()

    async def pulse(engine, claim, lease_seconds):
        await original_heartbeat(engine, claim, lease_seconds, sleep=sleep)

    monkeypatch.setattr(worker, "execute_cases", blocked)
    monkeypatch.setattr(worker, "heartbeat", pulse)
    task = asyncio.create_task(worker.execute_claim(database.engine, claim))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        await expire(database, claim)
        wake.set()
        await asyncio.wait_for(task, 3)
        assert cancelled.is_set()
        outcome = await report(api, run)
        assert outcome["status"] == "running" and outcome["persisted_cases"] == 0
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_third_case_slot_rejected(api, database):
    await launch(api)
    claim = await worker.claim_job(database.engine, uuid4())
    cases = [c["case_id"] for c in claim.dataset["cases"]]
    assert await worker.start_attempt(database.engine, claim, cases[0], 1)
    assert await worker.start_attempt(database.engine, claim, cases[1], 1)
    with pytest.raises(ValueError, match="Two case slots"):
        await worker.start_attempt(database.engine, claim, cases[2], 1)


async def test_crash_after_last_result_only_finalizes_even_after_deadline(
    api, database, monkeypatch
):
    run = await launch(api)
    first = await worker.claim_job(database.engine, uuid4())
    finalize = worker.finish_job

    async def crash(*args, **kwargs):
        raise asyncio.CancelledError

    monkeypatch.setattr(worker, "finish_job", crash)
    with pytest.raises(asyncio.CancelledError):
        await worker.execute_claim(database.engine, first)
    async with AsyncSession(database.engine) as session, session.begin():
        assert await session.scalar(select(func.count()).select_from(CaseResult)) == 12
        await session.execute(
            text(
                "UPDATE run_jobs SET lease_expires_at="
                "clock_timestamp()-interval '1 second', "
                "deadline_at=clock_timestamp()-interval '1 second'"
            )
        )
    monkeypatch.setattr(worker, "finish_job", finalize)
    monkeypatch.setattr(
        worker, "run_case", lambda *args: pytest.fail("Completed cases must not repeat")
    )
    await worker.run_one(database.engine)
    assert (await report(api, run))["complete"]
