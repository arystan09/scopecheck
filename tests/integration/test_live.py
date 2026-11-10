import asyncio
import json
from dataclasses import replace
from pathlib import Path
from uuid import UUID, uuid4

import httpx2
import psycopg
import pytest
from conftest import migration
from openai import AsyncOpenAI
from psycopg import sql
from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from scopecheck.runs import worker
from scopecheck.runs.budget import BudgetExceeded, reserve_call
from scopecheck.runs.live_cli import queue_live
from scopecheck.runs.models import CallReservation, CaseResult, Run

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
KEY = "mock-worker-key-never-real"


async def queued(api, config, *, budget=None, key=None, count=1):
    body = json.loads(Path("datasets/dev/basic.json").read_text())
    body["cases"] = body["cases"][:count]
    response = await api.client.post(
        "/api/v1/datasets", json=body, headers=api.headers()
    )
    assert response.status_code in (200, 201)
    return await queue_live(
        "one",
        UUID(response.json()["id"]),
        config,
        budget if budget is not None else config.maximum_charge() * count * 4,
        True,
        key or uuid4().hex,
    )


def response(output):
    return {
        "id": "resp_mock",
        "object": "response",
        "created_at": 1,
        "model": "test-model",
        "status": "completed",
        "output": output,
        "usage": {"input_tokens": 300, "output_tokens": 30, "total_tokens": 330},
    }


def final():
    return {
        "type": "message",
        "role": "assistant",
        "status": "completed",
        "id": "msg_mock",
        "content": [{"type": "output_text", "text": "done", "annotations": []}],
    }


def install_mock(monkeypatch, handler):
    monkeypatch.setenv("SCOPECHECK_OPENAI_API_KEY", KEY)

    def factory(key):
        assert key == KEY
        return AsyncOpenAI(
            api_key=key,
            max_retries=0,
            http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),
        )

    monkeypatch.setattr(worker, "client_for_worker", factory)


async def test_live_worker_reserves_before_http_and_reports_provenance(
    api, database, live_config, monkeypatch
):
    run = await queued(api, live_config)
    requests = []

    async def handler(request):
        async with AsyncSession(database.engine) as session:
            assert (
                await session.scalar(
                    text(
                        "SELECT count(*) FROM pg_stat_activity "
                        "WHERE datname = current_database() "
                        "AND state = 'idle in transaction'"
                    )
                )
                == 0
            )
            reservations = list((await session.scalars(select(CallReservation))).all())
            assert len(reservations) == len(requests) + 1  # committed before dispatch
        requests.append(json.loads(request.content))
        output = (
            [
                {
                    "type": "function_call",
                    "id": "fc_mock",
                    "call_id": "call1",
                    "name": "get_order",
                    "arguments": '{"order_id":"o1"}',
                }
            ]
            if len(requests) == 1
            else [final()]
        )
        return httpx2.Response(200, json=response(output))

    install_mock(monkeypatch, handler)
    assert await worker.run_one(database.engine)
    report = (
        await api.client.get(
            f"/api/v1/runs/{run['run_id']}/report", headers=api.headers()
        )
    ).json()
    assert report["complete"] and report["mode"] == "live" and report["synthetic"]
    assert report["legitimate_task_success"]["numerator"] == 1
    assert report["reserved_microusd"] == 2 * live_config.maximum_charge()
    assert report["provider_outcomes"] == {"final_response": 1}
    assert report["snapshot"]["model"] == "test-model"
    cases = (
        await api.client.get(
            f"/api/v1/runs/{run['run_id']}/cases", headers=api.headers()
        )
    ).json()
    assert cases["items"][0]["provenance"]["requests"] == 2
    assert cases["items"][0]["provenance"]["calls"][0]["input_tokens"] == 300
    assert KEY not in json.dumps(cases) + json.dumps(report) + json.dumps(requests)
    assert (
        await api.client.get(
            f"/api/v1/runs/{run['run_id']}/report", headers=api.headers("other")
        )
    ).status_code == 404
    async with AsyncSession(database.engine) as session:
        stored = await session.get(Run, UUID(run["run_id"]))
        # Knowing a local live config ID does not enable public live execution.
        refused = await api.client.post(
            "/api/v1/runs",
            json={
                "dataset_id": str(stored.dataset_id),
                "config_id": str(stored.config_id),
            },
            headers=api.headers(**{"Idempotency-Key": uuid4().hex}),
        )
        assert refused.status_code == 422
    assert not await worker.run_one(database.engine)


@pytest.mark.parametrize(
    "status,expected",
    [
        (401, "invalid_credentials"),
        (429, "rate_limit"),
        (503, "server_error"),
        (None, "timeout"),
    ],
)
async def test_unknown_or_error_outcomes_keep_reservations(
    api, database, live_config, monkeypatch, status, expected
):
    run = await queued(api, live_config, budget=live_config.maximum_charge())
    calls = []

    def handler(request):
        calls.append(request)
        if status is None:
            raise httpx2.ReadTimeout(KEY, request=request)
        return httpx2.Response(status, json={"error": {"message": KEY}})

    install_mock(monkeypatch, handler)
    await worker.run_one(database.engine)
    report = (
        await api.client.get(
            f"/api/v1/runs/{run['run_id']}/report", headers=api.headers()
        )
    ).json()
    assert (
        len(calls) == 1 and report["reserved_microusd"] == live_config.maximum_charge()
    )
    assert report["provider_outcomes"] == {expected: 1} and not report["complete"]
    assert KEY not in json.dumps(report)
    async with AsyncSession(database.engine) as session:
        reservation = await session.scalar(select(CallReservation))
        for operation in (
            "DELETE FROM call_reservations",
            "UPDATE call_reservations SET amount_microusd = 1",
        ):
            with pytest.raises(IntegrityError):
                async with session.begin_nested():
                    await session.execute(text(operation))
        assert reservation.amount_microusd == live_config.maximum_charge()


async def test_concurrent_budget_reservations_fenced_unique_and_tenant_safe(
    api, database, live_config
):
    await queued(api, live_config, budget=live_config.maximum_charge())
    claim = await worker.claim_job(database.engine, uuid4())
    case_id = claim.dataset["cases"][0]["case_id"]
    await worker.start_attempt(database.engine, claim, case_id, 1, 60)
    outcomes = await asyncio.gather(
        *(
            reserve_call(database.engine, claim, case_id, 1, i, live_config)
            for i in (1, 2)
        ),
        return_exceptions=True,
    )
    assert sum(isinstance(x, UUID) for x in outcomes) == 1
    assert sum(isinstance(x, BudgetExceeded) for x in outcomes) == 1
    with pytest.raises(worker.LeaseLost):
        await reserve_call(
            database.engine,
            replace(claim, generation=claim.generation + 1),
            case_id,
            1,
            3,
            live_config,
        )
    async with AsyncSession(database.engine) as session:
        reservation = await session.scalar(select(CallReservation))
        values = {
            column.name: getattr(reservation, column.name)
            for column in CallReservation.__table__.columns
            if column.name not in {"id", "created_at"}
        }
        for changes in ({}, {"workspace_id": uuid4(), "sequence": 4}):
            with pytest.raises(IntegrityError):
                async with session.begin_nested():
                    session.add(CallReservation(**{**values, **changes}))
                    await session.flush()
        assert (
            await session.scalar(select(func.count()).select_from(CallReservation)) == 1
        )


async def test_local_owner_and_budget_required_public_live_forbidden(api, live_config):
    run = await queued(api, live_config, key="stable-live")
    again = await queued(api, live_config, key="stable-live")
    assert again["run_id"] == run["run_id"] and again["created"] is False
    for body in ({"mode": "live"}, live_config.model_dump(mode="json")):
        assert (
            await api.client.post("/api/v1/configs", json=body, headers=api.headers())
        ).status_code == 422
    for authorization, budget in ((False, 1000000), (True, 0), (True, 1)):
        with pytest.raises(ValueError):
            await queue_live("one", uuid4(), live_config, budget, authorization, "x")
    for role, expected in (("editor", 422), ("reader", 403)):
        rejected = await api.client.post(
            "/api/v1/runs",
            json={
                "mode": "live",
                "dataset_id": str(uuid4()),
                "config_id": str(uuid4()),
            },
            headers=api.headers(role, **{"Idempotency-Key": uuid4().hex}),
        )
        assert rejected.status_code == expected


async def test_non_owner_database_role_cannot_queue(
    api, database, live_config, monkeypatch
):
    parsed = make_url(database.url)
    conninfo = parsed.set(drivername="postgresql").render_as_string(hide_password=False)
    role = "scopecheck_nonowner_" + uuid4().hex
    async with await psycopg.AsyncConnection.connect(
        conninfo, autocommit=True
    ) as admin:
        await admin.execute(
            sql.SQL("CREATE ROLE {} LOGIN").format(sql.Identifier(role))
        )
        try:
            monkeypatch.setenv(
                "SCOPECHECK_DATABASE_URL",
                parsed.set(username=role).render_as_string(hide_password=False),
            )
            with pytest.raises(ValueError, match="owner"):
                await queue_live("one", uuid4(), live_config, 1_000_000, True, "x")
        finally:
            await admin.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))


async def test_migration_preserves_prior_fake_fixture(api, database, live_config):
    # Downgrade an untouched schema to the actual prior revision, then seed it.
    await migration(database.url, "downgrade", "0002")
    from test_worker import launch

    fake = await launch(api)
    await migration(database.url, "upgrade", "head")
    await migration(database.url, "check")
    await worker.run_one(database.engine)
    report = (
        await api.client.get(f"/api/v1/runs/{fake['id']}/report", headers=api.headers())
    ).json()
    assert report["complete"] and report["mode"] == "fake"
    assert report["reserved_microusd"] == 0


async def test_missing_key_never_constructs_client(
    api, database, live_config, monkeypatch
):
    run = await queued(api, live_config)
    monkeypatch.delenv("SCOPECHECK_OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(
        worker, "client_for_worker", lambda key: pytest.fail("No credential supplied")
    )
    await worker.run_one(database.engine)
    async with AsyncSession(database.engine) as session:
        stored = await session.get(Run, UUID(run["run_id"]))
        assert stored.error_code == "missing_credentials"
        assert (
            await session.scalar(select(func.count()).select_from(CallReservation)) == 0
        )


async def test_worker_budget_stops_dispatch_after_first_reserved_call(
    api, database, live_config, monkeypatch
):
    run = await queued(api, live_config, budget=live_config.maximum_charge())
    calls = []

    def handler(request):
        calls.append(request)
        return httpx2.Response(
            200,
            json=response(
                [
                    {
                        "type": "function_call",
                        "call_id": "call1",
                        "id": "fc1",
                        "name": "get_order",
                        "arguments": '{"order_id":"o1"}',
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
    assert len(calls) == 1 and report["provider_outcomes"] == {"budget_exhausted": 1}
    assert report["limited_cases"] == 1 and report["coverage"]["numerator"] == 0
    assert report["legitimate_task_success"]["denominator"] is None


async def test_live_reset_and_normalized_outcomes(
    api, database, live_config, monkeypatch
):
    # Six different legitimate cases, including refunds and address changes.
    source = json.loads(Path("datasets/dev/basic.json").read_text())
    public_traces = {
        c["public_input"]: c["traces"]["safe"] for c in source["cases"][:6]
    }

    def handler(request):
        body = json.loads(request.content)
        if len(body["input"]) > 1:
            return httpx2.Response(200, json=response([final()]))
        trace = public_traces[body["input"][0]["content"]]
        return httpx2.Response(
            200,
            json=response(
                [
                    {
                        "type": "function_call",
                        "id": f"fc{i}",
                        "call_id": f"call{i}",
                        "name": call["name"],
                        "arguments": json.dumps(call["arguments"]),
                    }
                    for i, call in enumerate(trace)
                ]
            ),
        )

    install_mock(monkeypatch, handler)
    normalized = []
    for _ in range(2):
        run = await queued(api, live_config, count=6)
        await worker.run_one(database.engine)
        report = (
            await api.client.get(
                f"/api/v1/runs/{run['run_id']}/report", headers=api.headers()
            )
        ).json()
        assert report["legitimate_task_success"]["numerator"] == 6
        assert report["executed_violations"] == 0 and report["complete"]
        async with AsyncSession(database.engine) as session:
            scores = list(
                (
                    await session.scalars(
                        select(CaseResult.score)
                        .where(CaseResult.run_id == UUID(run["run_id"]))
                        .order_by(CaseResult.case_id)
                    )
                ).all()
            )
            normalized.append(scores)
    assert normalized[0] == normalized[1]


async def test_changed_live_budget_conflicts_and_expired_price_cannot_dispatch(
    api, database, live_config, monkeypatch
):

    from scopecheck.api.errors import ApiError

    await queued(api, live_config, key="same")
    with pytest.raises(ApiError) as conflict:
        await queued(
            api, live_config, key="same", budget=live_config.maximum_charge() * 3
        )
    assert conflict.value.status == 409
    monkeypatch.setattr(
        worker,
        "client_for_worker",
        lambda key: pytest.fail("Expired price must prevent dispatch"),
    )
    monkeypatch.setattr(
        type(live_config.price),
        "require_current",
        lambda self: (_ for _ in ()).throw(ValueError("expired")),
    )
    await worker.run_one(database.engine)
    async with AsyncSession(database.engine) as session:
        assert (
            await session.scalar(select(func.count()).select_from(CallReservation)) == 0
        )


async def test_actual_local_cli_queues_without_reading_provider_key(
    api, database, live_config, tmp_path
):
    import os

    data = json.loads(Path("datasets/dev/basic.json").read_text())
    imported = await api.client.post(
        "/api/v1/datasets", json=data, headers=api.headers()
    )
    config_path = tmp_path / "config.json"
    config_path.write_text(live_config.model_dump_json())
    env = {**os.environ, "SCOPECHECK_DATABASE_URL": database.url}
    env.pop("SCOPECHECK_OPENAI_API_KEY", None)
    command = [
        "scopecheck",
        "run",
        "live",
        "--workspace",
        "one",
        "--dataset-id",
        imported.json()["id"],
        "--config",
        str(config_path),
        "--budget-microusd",
        "1000000",
        "--idempotency-key",
        "cli-tested",
        "--owner-authorized",
    ]
    process = await asyncio.create_subprocess_exec(
        *command,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    output, error = await process.communicate()
    assert process.returncode == 0, error.decode()
    queued_run = json.loads(output)
    assert queued_run["mode"] == "live" and queued_run["status"] == "queued"
    async with AsyncSession(database.engine) as session:
        assert (
            await session.scalar(select(func.count()).select_from(CallReservation)) == 0
        )
        run = await session.get(Run, UUID(queued_run["run_id"]))
        assert run.snapshot["authorization"] == "local_database_owner"
