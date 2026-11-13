"""Public fixed work, atomic global quotas, and fail-closed deployment mode."""

from contextlib import asynccontextmanager
from uuid import UUID, uuid4

import httpx
import pytest
from conftest import migration
from recovery_helpers import race_processes
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from test_live import queued
from test_persistence import seed
from test_worker import launch

from scopecheck.app import create_app
from scopecheck.datasets import demo
from scopecheck.datasets.splits import MANIFEST_HASH
from scopecheck.runs import worker
from scopecheck.runs.live_cli import queue_live
from scopecheck.runs.models import CallReservation, DemoAdmission, Run, RunJob
from scopecheck.runs.reports import report
from scopecheck.settings import Settings

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@asynccontextmanager
async def public_client(monkeypatch):
    monkeypatch.setenv("SCOPECHECK_PUBLIC_DEMO", "true")
    monkeypatch.delenv("SCOPECHECK_OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    app = create_app()
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client,
    ):
        yield client


async def counters(database):
    async with AsyncSession(database.engine) as session:
        return (
            await session.scalar(select(DemoAdmission.admitted)),
            await session.scalar(select(func.count()).select_from(Run)),
            await session.scalar(select(func.count()).select_from(RunJob)),
        )


async def advance_throttle(database):
    async with database.engine.begin() as connection:
        await connection.execute(
            text(
                "UPDATE demo_admission SET last_admitted_at = "
                "clock_timestamp() - interval '6 seconds'"
            )
        )


async def test_fixed_public_run_completes_without_provider_and_throttles(
    database, monkeypatch
):
    def no_provider(*args, **kwargs):
        pytest.fail("Public demo must never construct a provider client")

    monkeypatch.setattr(worker, "client_for_worker", no_provider)
    async with public_client(monkeypatch) as client:
        first = await client.post("/demo/runs", json={})
        assert first.status_code == 202
        assert first.json()["mode"] == "fake" and first.json()["synthetic"] is True
        for headers in ({}, {"X-Forwarded-For": "other-client"}):
            assert (await client.post("/demo/runs", headers=headers)).status_code == 429
        await advance_throttle(database)
        assert (await client.post("/demo/runs")).status_code == 202
        await advance_throttle(database)
        assert (await client.post("/demo/runs")).status_code == 429
        assert await counters(database) == (2, 2, 2)
        assert await worker.run_one(database.engine)
        async with AsyncSession(database.engine) as session:
            run = await session.get(Run, UUID(first.json()["id"]))
            assert run.snapshot["demo_manifest"] == MANIFEST_HASH
            assert run.snapshot["repetitions"] == 1
            assert run.snapshot["dataset_hash"] == demo.fixed_hash()
            result = await report(session, run)
            assert result["complete"]
            assert result["completed_cases"] == 24
            assert result["legitimate_task_success"]["numerator"] == 10
            assert result["executed_violations"] == 0
            assert (
                await session.scalar(select(func.count()).select_from(CallReservation))
                == 0
            )
        assert (await client.post("/demo/runs")).status_code == 202
        # Admission creates no anonymous read capability.
        assert (
            await client.get(f"/api/v1/runs/{first.json()['id']}")
        ).status_code == 401


async def test_public_rejects_all_overrides_and_operator_writes(
    api, database, monkeypatch
):
    refs = await seed(api)
    run = await launch(api)
    before = await counters(database)
    async with public_client(monkeypatch) as client:
        for body in (
            {"mode": "live"},
            {"prompt": "override"},
            {"model": "chosen"},
            {"url": "http://invalid.test"},
            {"dataset_id": refs["dataset_id"]},
            {"config_id": refs["config_id"]},
            {"fixture": "unsafe"},
            {"workspace_id": str(uuid4())},
            {"repetitions": 100},
            {"budget_microusd": 1},
            {"owner_authorized": True},
            [],
        ):
            response = await client.post("/demo/runs", json=body)
            assert response.status_code in (413, 422)
        assert (await client.post("/demo/runs?mode=live")).status_code == 422
        assert (await client.post("/demo/runs", content=b" " * 65)).status_code == 413
        for path, body in [
            ("/api/v1/datasets", {}),
            ("/api/v1/configs", {}),
            ("/api/v1/runs", refs),
        ]:
            for role in ("editor", "reader"):
                response = await client.post(
                    path,
                    json=body,
                    headers=api.headers(role, **{"Idempotency-Key": "public-disabled"}),
                )
                assert response.status_code == 403
        assert (
            await client.get(f"/api/v1/runs/{run['id']}", headers=api.headers("reader"))
        ).status_code == 200
        assert (
            await client.get(f"/api/v1/runs/{run['id']}", headers=api.headers("other"))
        ).status_code == 404
    assert await counters(database) == before


async def test_local_mode_has_no_anonymous_writes(api, database):
    assert (await api.client.post("/demo/runs")).status_code == 404
    assert await counters(database) == (0, 0, 0)


async def test_public_disables_local_live_creation_and_preexisting_jobs(
    api, database, live_config, monkeypatch
):
    live = await queued(api, live_config)
    ordinary_fake = await launch(api)
    monkeypatch.setenv("SCOPECHECK_PUBLIC_DEMO", "true")
    monkeypatch.delenv("SCOPECHECK_OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    def forbidden(*args, **kwargs):
        pytest.fail(
            "No live client in public mode, including previously queued live runs"
        )

    monkeypatch.setattr(worker, "client_for_worker", forbidden)
    with pytest.raises(ValueError, match="disabled"):
        await queue_live(
            "one",
            uuid4(),
            live_config,
            live_config.maximum_charge(),
            True,
            "public-live",
        )
    assert await worker.run_one(database.engine)
    assert await worker.run_one(database.engine)
    async with AsyncSession(database.engine) as session:
        for id in (live["run_id"], ordinary_fake["id"]):
            run = await session.get(Run, UUID(id))
            assert run.status == "failed"
            assert run.error_code == "public_demo_only"
            assert run.completed_cases == 0
        assert (
            await session.scalar(select(func.count()).select_from(CallReservation)) == 0
        )


RACE = """
import asyncio,json
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from scopecheck.datasets import demo
from scopecheck.api.errors import ApiError
from scopecheck.settings import Settings
from scopecheck.db import create_engine
print("ready",flush=True)
payload=json.loads(input())
async def main():
    settings=Settings(public_demo=True)
    # Isolate last queue/lifetime slots from the separate throttle constraint.
    if payload["isolate"]:
        demo.THROTTLE_SECONDS=0
    engine=create_engine(settings)
    async with AsyncSession(engine) as session:
        try:
            async with session.begin():
                pid=await session.scalar(text("SELECT pg_backend_pid()"))
                await demo.admit_demo(session,settings)
            outcome="accepted"
        except ApiError as error:
            assert error.status==429
            outcome="limited"
    print(json.dumps({"pid":pid,"outcome":outcome}),flush=True)
    await engine.dispose()
asyncio.run(main())
"""


@pytest.mark.parametrize("boundary", ["throttle", "queue", "lifetime"])
async def test_global_admission_races_across_independent_processes(
    database, boundary, monkeypatch
):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    if boundary == "queue":
        async with AsyncSession(database.engine) as session, session.begin():
            await demo.admit_demo(session, Settings(public_demo=True))
        await advance_throttle(database)
    if boundary == "lifetime":
        async with database.engine.begin() as connection:
            await connection.execute(text("UPDATE demo_admission SET admitted=99"))
    outcomes = await race_processes(
        database.url, RACE, [{"isolate": boundary != "throttle"}] * 2
    )
    assert len({item["pid"] for item in outcomes}) == 2
    assert sorted(item["outcome"] for item in outcomes) == ["accepted", "limited"]
    assert (
        await counters(database)
        == {"throttle": (1, 1, 1), "queue": (2, 2, 2), "lifetime": (100, 1, 1)}[
            boundary
        ]
    )


async def test_admission_rollback_does_not_consume_quota(database):
    with pytest.raises(RuntimeError, match="rollback"):
        async with AsyncSession(database.engine) as session, session.begin():
            await demo.admit_demo(session, Settings(public_demo=True))
            raise RuntimeError("rollback")
    assert await counters(database) == (0, 0, 0)


async def test_upgrade_from_mvp_preserves_queued_data(api, database):
    run = await launch(api)
    await migration(database.url, "downgrade", "0004")
    await migration(database.url, "upgrade", "head")
    async with AsyncSession(database.engine) as session:
        existing = await session.get(Run, UUID(run["id"]))
        assert existing.status == "queued"
        assert existing.snapshot == run["snapshot"]
        assert await session.scalar(select(DemoAdmission.admitted)) == 0
    assert await worker.run_one(database.engine)
