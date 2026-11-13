import asyncio
import json
import os
import sys
from dataclasses import asdict
from uuid import UUID, uuid4

import httpx2
import pytest
from conftest import migration
from recovery_helpers import race_processes
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from test_live import final, install_mock, queued, response

from scopecheck.providers.base import Turn
from scopecheck.runs import worker
from scopecheck.runs.budget import BudgetExceeded, reserve_call, settle_call
from scopecheck.runs.models import (
    CallReservation,
    CallSettlement,
    CaseAttempt,
    CaseResult,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

RACE = """
import asyncio,json
from uuid import UUID
from sqlalchemy import text
from scopecheck.db import create_engine
from scopecheck.settings import Settings
from scopecheck.providers.config import LiveConfig
from scopecheck.runs.worker import Claim
from scopecheck.runs.budget import reserve_call,BudgetExceeded
print("ready",flush=True)
payload=json.loads(input())
async def main():
    engine=create_engine(Settings())
    async with engine.connect() as connection:
        pid=await connection.scalar(text("SELECT pg_backend_pid()"))
    data=payload["claim"]
    for key in ("job_id","run_id","workspace_id","owner"):
        data[key]=UUID(data[key])
    claim=Claim(**data)
    try:
        await reserve_call(engine, claim, payload["case"], 1, payload["sequence"],
                           LiveConfig.model_validate(payload["config"]))
        outcome="reserved"
    except BudgetExceeded:
        outcome="budget_exhausted"
    print(json.dumps({"pid":pid,"outcome":outcome}),flush=True)
    await engine.dispose()
asyncio.run(main())
"""


async def prepare(api, database, config, budget):
    run = await queued(api, config, budget=budget, count=2)
    claim = await worker.claim_job(database.engine, uuid4())
    cases = [case["case_id"] for case in claim.dataset["cases"]]
    for case in cases:
        await worker.start_attempt(database.engine, claim, case, 1)
    return run, claim, cases


async def race(database, claim, config, cases, sequences):
    payloads = [
        {
            "claim": asdict(claim),
            "case": case,
            "sequence": sequence,
            "config": config.model_dump(mode="json"),
        }
        for case, sequence in zip(cases, sequences, strict=True)
    ]
    outcomes = await race_processes(database.url, RACE, payloads)
    assert len({r["pid"] for r in outcomes}) == 2
    assert sorted(r["outcome"] for r in outcomes) == ["budget_exhausted", "reserved"]


async def test_independent_process_last_budget_race(api, database, live_config):
    maximum = live_config.maximum_charge()
    run, claim, cases = await prepare(api, database, live_config, maximum)
    await race(database, claim, live_config, cases, [1, 1])
    async with AsyncSession(database.engine) as session:
        assert (
            await session.scalar(select(func.count()).select_from(CallReservation)) == 1
        )
    report = (
        await api.client.get(
            f"/api/v1/runs/{run['run_id']}/report", headers=api.headers()
        )
    ).json()
    assert report["budget_accounted_microusd"] == maximum
    assert report["spent_microusd"] == 0 and report["unknown_reservations"] == 1


async def test_settlement_releases_only_known_usage_then_last_budget_race(
    api, database, live_config
):
    maximum = live_config.maximum_charge()
    run, claim, cases = await prepare(api, database, live_config, maximum + 1000)
    reservation = await reserve_call(
        database.engine, claim, cases[0], 1, 1, live_config
    )
    with pytest.raises(BudgetExceeded):
        await reserve_call(database.engine, claim, cases[1], 1, 1, live_config)
    turn = Turn("tools", input_tokens=100, output_tokens=20, model="test-model")
    await settle_call(database.engine, claim, reservation, live_config, turn)
    await settle_call(
        database.engine, claim, reservation, live_config, turn
    )  # idempotent
    await race(database, claim, live_config, cases, [2, 1])
    report = (
        await api.client.get(
            f"/api/v1/runs/{run['run_id']}/report", headers=api.headers()
        )
    ).json()
    assert report["spent_microusd"] == 141
    assert report["outstanding_microusd"] == maximum
    assert report["budget_accounted_microusd"] <= maximum + 1000
    with pytest.raises(ValueError, match="Conflicting"):
        await settle_call(
            database.engine,
            claim,
            reservation,
            live_config,
            Turn("final_response", input_tokens=1, output_tokens=1),
        )
    async with AsyncSession(database.engine) as session:
        assert (
            await session.scalar(select(func.count()).select_from(CallSettlement)) == 1
        )
        for statement in (
            "UPDATE call_settlements SET amount_microusd=0",
            "DELETE FROM call_settlements",
        ):
            with pytest.raises(IntegrityError):
                async with session.begin_nested():
                    await session.execute(text(statement))


async def test_unknown_response_and_stale_owner_cannot_release_or_reserve(
    api, database, live_config
):
    maximum = live_config.maximum_charge()
    _, first, cases = await prepare(api, database, live_config, maximum * 2)
    reservation = await reserve_call(
        database.engine, first, cases[0], 1, 1, live_config
    )
    await settle_call(database.engine, first, reservation, live_config, Turn("timeout"))
    async with AsyncSession(database.engine) as session, session.begin():
        await session.execute(
            text(
                "UPDATE run_jobs SET lease_expires_at=clock_timestamp()-interval '1 "
                "second'"
            )
        )
    current = await worker.claim_job(database.engine, uuid4())
    with pytest.raises(worker.LeaseLost):
        await reserve_call(database.engine, first, cases[1], 1, 1, live_config)
    with pytest.raises(worker.LeaseLost):
        await settle_call(
            database.engine,
            first,
            reservation,
            live_config,
            Turn("final_response", input_tokens=1, output_tokens=1),
        )
    # A successor also cannot invent usage for a reservation from another generation.
    with pytest.raises(worker.LeaseLost):
        await settle_call(
            database.engine,
            current,
            reservation,
            live_config,
            Turn("final_response", input_tokens=1, output_tokens=1),
        )
    async with AsyncSession(database.engine) as session:
        settlement = await session.scalar(select(CallSettlement))
        assert settlement.outcome == "timeout" and settlement.amount_microusd is None


async def test_crash_after_remote_request_boundary_keeps_unknown_cost(
    api, database, live_config, monkeypatch
):
    maximum = live_config.maximum_charge()
    run = await queued(api, live_config, budget=maximum * 2)
    env = {
        **os.environ,
        "SCOPECHECK_DATABASE_URL": database.url,
        "SCOPECHECK_OPENAI_API_KEY": "offline-process-fixture-key",
    }
    code = """
import asyncio,httpx2
from openai import AsyncOpenAI
from scopecheck.runs import worker
async def remote(request):
    print("request_dispatched",flush=True)
    await asyncio.Event().wait()
def factory(key):
    return AsyncOpenAI(api_key=key, max_retries=0,
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(remote)))
worker.client_for_worker=factory
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
        assert (
            await asyncio.wait_for(process.stdout.readline(), 10)
            == b"request_dispatched\n"
        )
        process.kill()
        await process.wait()
        async with AsyncSession(database.engine) as session, session.begin():
            assert (
                await session.scalar(select(func.count()).select_from(CallReservation))
                == 1
            )
            assert (
                await session.scalar(select(func.count()).select_from(CallSettlement))
                == 0
            )
            await session.execute(
                text(
                    "UPDATE run_jobs SET lease_expires_at=clock_timestamp()-interval "
                    "'1 second'"
                )
            )
        install_mock(
            monkeypatch, lambda request: httpx2.Response(200, json=response([final()]))
        )
        await worker.run_one(database.engine)
        report = (
            await api.client.get(
                f"/api/v1/runs/{run['run_id']}/report", headers=api.headers()
            )
        ).json()
        assert report["complete"] and report["unknown_reservations"] == 1
        assert (
            report["outstanding_microusd"] == maximum
            and report["spent_microusd"] == 361
        )
        assert report["budget_accounted_microusd"] <= maximum * 2
        assert [r["outcome"] for r in report["request_ledger"]] == [
            "unknown",
            "final_response",
        ]
        async with AsyncSession(database.engine) as session:
            assert (
                await session.scalar(select(func.count()).select_from(CaseResult)) == 1
            )
            attempt = await session.scalar(select(CaseAttempt))
            assert attempt.attempt_number == 2 and len(attempt.history) == 1
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


async def test_prior_mvp_unknown_reservation_survives_upgrade(
    api, database, live_config
):
    _, claim, cases = await prepare(
        api, database, live_config, live_config.maximum_charge()
    )
    reservation = await reserve_call(
        database.engine, claim, cases[0], 1, 1, live_config
    )
    await migration(database.url, "downgrade", "0003")
    await migration(database.url, "upgrade", "head")
    await migration(database.url, "check")
    async with AsyncSession(database.engine) as session:
        record = await session.get(CallReservation, reservation)
        assert (
            record.amount_microusd == live_config.maximum_charge()
            and record.attempt_number == 1
        )
        assert (
            await session.scalar(select(func.count()).select_from(CallSettlement)) == 0
        )


async def test_incomplete_refund_case_restarts_from_initial_state(
    api, database, live_config, monkeypatch
):
    from pathlib import Path

    from scopecheck.runs.live_cli import queue_live

    source = json.loads(Path("datasets/dev/basic.json").read_text())
    source["cases"] = [
        next(c for c in source["cases"] if c["case_id"] == "refund-limit")
    ]
    imported = await api.client.post(
        "/api/v1/datasets", json=source, headers=api.headers()
    )
    run = await queue_live(
        "one",
        UUID(imported.json()["id"]),
        live_config,
        4 * live_config.maximum_charge(),
        True,
        "recover-refund",
    )
    env = {
        **os.environ,
        "SCOPECHECK_DATABASE_URL": database.url,
        "SCOPECHECK_OPENAI_API_KEY": "offline-process-fixture-key",
    }
    code = """
import asyncio,httpx2,json
from openai import AsyncOpenAI
from scopecheck.runs import worker
async def remote(request):
    body=json.loads(request.content)
    if len(body["input"]) > 1:
        assert json.loads(body["input"][-1]["output"])["status"] == "executed"
        print("after_refund",flush=True)
        await asyncio.Event().wait()
    return httpx2.Response(200,json={"id":"resp1","object":"response",
        "created_at":1,"model":"test-model","status":"completed",
        "usage":{"input_tokens":300,"output_tokens":30,"total_tokens":330},
        "output":[{"type":"function_call","id":"fc1","call_id":"call1",
            "name":"issue_refund","arguments":json.dumps({"order_id":"o1",
                "amount_minor":5000,"reason":"Synthetic test refund"})}]})
def factory(key):
    return AsyncOpenAI(api_key=key,max_retries=0,
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(remote)))
worker.client_for_worker=factory
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
        assert (
            await asyncio.wait_for(process.stdout.readline(), 10) == b"after_refund\n"
        )
        process.kill()
        await process.wait()
        async with AsyncSession(database.engine) as session, session.begin():
            assert (
                await session.scalar(select(func.count()).select_from(CallReservation))
                == 2
            )
            assert (
                await session.scalar(select(func.count()).select_from(CallSettlement))
                == 1
            )
            await session.execute(
                text(
                    "UPDATE run_jobs SET lease_expires_at="
                    "clock_timestamp()-interval '1 second'"
                )
            )

        def handler(request):
            if len(json.loads(request.content)["input"]) > 1:
                return httpx2.Response(200, json=response([final()]))
            return httpx2.Response(
                200,
                json=response(
                    [
                        {
                            "type": "function_call",
                            "id": "fc1",
                            "call_id": "call1",
                            "name": "issue_refund",
                            "arguments": json.dumps(
                                {
                                    "order_id": "o1",
                                    "amount_minor": 5000,
                                    "reason": "Synthetic test refund",
                                }
                            ),
                        }
                    ]
                ),
            )

        install_mock(monkeypatch, handler)
        await worker.run_one(database.engine)
        report = (
            await api.client.get(
                f"/api/v1/runs/{run['run_id']}/report", headers=api.headers()
            )
        ).json()
        assert (
            report["complete"] and report["legitimate_task_success"]["numerator"] == 1
        )
        assert (
            report["executed_violations"] == 0 and report["unknown_reservations"] == 1
        )
        async with AsyncSession(database.engine) as session:
            result = await session.scalar(select(CaseResult))
            assert result.score["final_state"]["orders"][0]["refunded_minor"] == 5000
            assert (
                result.score["events"][0]["before"]["orders"][0]["refunded_minor"] == 0
            )
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


@pytest.mark.parametrize("boundary", ["deadline", "configuration", "attempt"])
async def test_refused_reservation_cannot_consume_budget(
    api, database, live_config, boundary
):
    _, claim, cases = await prepare(
        api, database, live_config, live_config.maximum_charge() * 2
    )
    config, case = live_config, cases[0]
    expected, match = ValueError, "Current running attempt"
    if boundary == "deadline":
        async with database.engine.begin() as connection:
            await connection.execute(
                text(
                    "UPDATE run_jobs SET deadline_at = "
                    "clock_timestamp() - interval '1 second'"
                )
            )
        expected, match = worker.RunDeadlineExceeded, None
    elif boundary == "configuration":
        config = live_config.model_copy(
            update={
                "generation": live_config.generation.model_copy(
                    update={
                        "max_output_tokens": live_config.generation.max_output_tokens
                        + 1
                    }
                )
            }
        )
        match = "Reservation configuration mismatch"
    else:
        case = "no-running-attempt"
    with pytest.raises(expected, match=match):
        await reserve_call(database.engine, claim, case, 1, 1, config)
    async with AsyncSession(database.engine) as session:
        assert (
            await session.scalar(select(func.count()).select_from(CallReservation)) == 0
        )


async def test_wrong_settlement_configuration_cannot_release_reserved_cost(
    api, database, live_config
):
    _, claim, cases = await prepare(
        api, database, live_config, live_config.maximum_charge()
    )
    reservation = await reserve_call(
        database.engine, claim, cases[0], 1, 1, live_config
    )
    changed = live_config.model_copy(
        update={
            "generation": live_config.generation.model_copy(
                update={
                    "max_output_tokens": live_config.generation.max_output_tokens + 1
                }
            )
        }
    )
    with pytest.raises(ValueError, match="Settlement configuration mismatch"):
        await settle_call(
            database.engine,
            claim,
            reservation,
            changed,
            Turn(outcome="final_response", input_tokens=1, output_tokens=1),
        )
    async with AsyncSession(database.engine) as session:
        assert (
            await session.scalar(select(func.count()).select_from(CallSettlement)) == 0
        )
        assert (
            await session.get(CallReservation, reservation)
        ).amount_microusd == live_config.maximum_charge()
    with pytest.raises(BudgetExceeded):
        await reserve_call(database.engine, claim, cases[1], 1, 1, live_config)
