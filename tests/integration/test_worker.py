import asyncio
import json
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
import pytest
from conftest import migration
from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from scopecheck.auth import bootstrap
from scopecheck.datasets.service import import_dataset
from scopecheck.demo import seed
from scopecheck.runs import worker
from scopecheck.runs.models import CaseAttempt, CaseResult, Run, RunJob, Workspace
from scopecheck.runs.service import ConfigInput, create_config
from scopecheck.version import PACKAGE_VERSION

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
BASIC = Path("datasets/dev/basic.json")


async def launch(api, fixture="safe", *, repetitions=1, limits=None, content=None):
    dataset = await api.client.post(
        "/api/v1/datasets",
        json=content or json.loads(BASIC.read_text()),
        headers=api.headers(),
    )
    config = await api.client.post(
        "/api/v1/configs", json={"fixture": fixture}, headers=api.headers()
    )
    body = {
        "dataset_id": dataset.json()["id"],
        "config_id": config.json()["id"],
        "repetitions": repetitions,
    }
    if limits is not None:
        body["limits"] = limits
    run = await api.client.post(
        "/api/v1/runs",
        json=body,
        headers=api.headers(**{"Idempotency-Key": uuid4().hex}),
    )
    assert run.status_code == 201
    return run.json()


async def report(api, run):
    response = await api.client.get(
        f"/api/v1/runs/{run['id']}/report", headers=api.headers()
    )
    assert response.status_code == 200
    return response.json()


@pytest.mark.parametrize(
    "fixture,successes,unsafe", [("safe", 6, 0), ("unsafe", 2, 13), ("deny-all", 0, 0)]
)
async def test_fake_completion_and_unique_evidence(
    api, database, fixture, successes, unsafe
):
    run = await launch(api, fixture)
    assert await worker.run_one(database.engine)
    outcome = await report(api, run)
    assert outcome["complete"] and outcome["completed_cases"] == 12
    assert outcome["legitimate_task_success"]["numerator"] == successes
    assert outcome["unsafe_proposals"]["numerator"] == unsafe
    assert outcome["executed_violations"] == 0
    assert outcome["worker_versions"] == [PACKAGE_VERSION]
    assert not await worker.run_one(database.engine)
    async with AsyncSession(database.engine) as session:
        assert await session.scalar(select(func.count()).select_from(CaseAttempt)) == 12
        assert await session.scalar(select(func.count()).select_from(CaseResult)) == 12
        result = await session.scalar(select(CaseResult).limit(1))
        assert result is not None
        with pytest.raises(IntegrityError):
            async with session.begin_nested():
                session.add(
                    CaseResult(
                        workspace_id=result.workspace_id,
                        run_id=result.run_id,
                        case_id=result.case_id,
                        repetition=result.repetition,
                        status=result.status,
                        score=result.score,
                    )
                )
                await session.flush()
        with pytest.raises(IntegrityError):
            async with session.begin_nested():
                await session.execute(text("UPDATE case_results SET status='failed'"))


async def test_case_state_resets_per_repetition_and_run(api, database):
    first = await launch(api, repetitions=2)
    second = await launch(api, repetitions=2)
    await worker.run_one(database.engine)
    await worker.run_one(database.engine)
    a, b = await report(api, first), await report(api, second)
    assert (
        a["legitimate_task_success"]
        == b["legitimate_task_success"]
        == {"numerator": 12, "denominator": 12, "value": 1.0}
    )
    scores = []
    for run in (first, second):
        response = await api.client.get(
            f"/api/v1/runs/{run['id']}/cases?limit=100", headers=api.headers()
        )
        items = response.json()["items"]
        assert len(items) == 24
        scores.append(
            sorted(
                [(item["case_id"], item["repetition"], item["score"]) for item in items]
            )
        )
    assert scores[0] == scores[1]


async def test_skip_locked_and_no_duplicate_claim(api, database):
    first, second = await launch(api), await launch(api)
    async with AsyncSession(database.engine) as session, session.begin():
        await session.scalar(
            select(RunJob).where(RunJob.run_id == UUID(first["id"])).with_for_update()
        )
        claim = await asyncio.wait_for(
            worker.claim_job(database.engine, uuid4()), timeout=3
        )
        assert claim is not None and str(claim.run_id) == second["id"]
    claims = await asyncio.gather(
        worker.claim_job(database.engine, uuid4()),
        worker.claim_job(database.engine, uuid4()),
    )
    assert sum(c is not None for c in claims) == 1
    assert str(next(c for c in claims if c is not None).run_id) == first["id"]


@pytest.mark.parametrize("change", ["owner", "generation", "expiry"])
async def test_stale_lease_cannot_write_results_or_finalize(api, database, change):
    run = await launch(api)
    claim = await worker.claim_job(database.engine, uuid4())
    assert claim is not None
    case = claim.dataset["cases"][0]["case_id"]
    await worker.start_attempt(database.engine, claim, case, 1, 60)
    async with AsyncSession(database.engine) as session, session.begin():
        job = await session.get(RunJob, claim.job_id)
        if change == "owner":
            job.lease_owner = uuid4()
        elif change == "generation":
            job.lease_generation += 1
        else:
            job.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    with pytest.raises(worker.LeaseLost):
        await worker.finish_attempt(
            database.engine, claim, case, 1, None, "failed", "test"
        )
    with pytest.raises(worker.LeaseLost):
        await worker.finish_job(database.engine, claim, "test")
    outcome = await report(api, run)
    assert outcome["persisted_cases"] == 0 and outcome["status"] == "running"
    reclaimed = await worker.claim_job(database.engine, uuid4())
    if change == "expiry":
        assert reclaimed is not None and reclaimed.generation == claim.generation + 1
    else:
        assert reclaimed is None


async def test_no_transaction_during_execution(api, database, monkeypatch):
    original = worker.run_case
    seen = []
    barrier = threading.Barrier(2)
    conninfo = (
        make_url(database.url)
        .set(drivername="postgresql")
        .render_as_string(hide_password=False)
    )

    def observe(case, provider):
        # Both slots are executing: no other case is starting/committing evidence.
        barrier.wait(timeout=5)
        with psycopg.connect(conninfo, autocommit=True) as connection:
            count = connection.execute(
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE datname=current_database() "
                "AND state='idle in transaction'"
            ).fetchone()[0]
        seen.append(count)
        barrier.wait(timeout=5)
        return original(case, provider)

    monkeypatch.setattr(worker, "run_case", observe)
    run = await launch(api)
    await worker.run_one(database.engine)
    assert seen == [0] * 12
    assert (await report(api, run))["complete"]


async def test_partial_limits_failure_and_zero_denominators(api, database):
    run = await launch(api, limits={"max_tool_calls": 1})
    queued = await report(api, run)
    assert queued["missing_cases"] == 12 and queued["coverage"]["numerator"] == 0
    assert queued["legitimate_task_success"]["denominator"] is None
    assert queued["unsafe_proposals"]["denominator"] is None
    await worker.run_one(database.engine)
    partial = await report(api, run)
    assert partial["status"] == "completed_with_errors" and not partial["complete"]
    assert partial["limited_cases"] == 1 and partial["coverage"]["numerator"] == 11
    assert partial["legitimate_task_success"]["denominator"] == 5
    assert partial["expected_legitimate_cases"] == 6
    data = json.loads(BASIC.read_text())
    data["cases"] = [c for c in data["cases"] if c["case_id"] == "customer-refund"]
    empty = await launch(api, "deny-all", content=data)
    await worker.run_one(database.engine)
    result = await report(api, empty)
    assert result["complete"]
    assert result["legitimate_task_success"]["denominator"] is None
    assert result["unsafe_proposals"]["denominator"] is None


async def test_compatible_incompatible_partial_and_foreign_comparisons(api, database):
    first, second = await launch(api), await launch(api, "unsafe")
    path = f"/api/v1/comparisons?baseline={first['id']}&candidate={second['id']}"
    response = await api.client.get(path, headers=api.headers())
    assert (
        response.status_code == 409
        and "runs_not_terminal" in response.json()["message"]
    )
    await worker.run_one(database.engine)
    await worker.run_one(database.engine)
    assert (await api.client.get(path, headers=api.headers())).json()["complete"]
    for suffix in ("report", "cases"):
        assert (
            await api.client.get(
                f"/api/v1/runs/{first['id']}/{suffix}", headers=api.headers("other")
            )
        ).status_code == 404
        assert (
            await api.client.get(
                f"/api/v1/runs/{first['id']}/{suffix}", headers=api.headers("reader")
            )
        ).status_code == 200
    assert (await api.client.get(path, headers=api.headers("other"))).status_code == 404
    # A terminal failed run with missing evidence is compatible, but explicitly partial.
    third = await launch(api)
    claim = await worker.claim_job(database.engine, uuid4())
    await worker.finish_job(database.engine, claim, "execution_failed")
    response = await api.client.get(
        f"/api/v1/comparisons?baseline={first['id']}&candidate={third['id']}",
        headers=api.headers(),
    )
    assert response.status_code == 200 and not response.json()["complete"]
    assert response.json()["warning"]
    assert response.json()["candidate"]["coverage"]["numerator"] == 0


@pytest.mark.parametrize(
    "field,value",
    [
        ("dataset_hash", "different"),
        ("evaluator_version", "2"),
        ("policy_version", "2"),
        ("tool_schema_version", "2"),
        ("repetitions", 2),
        ("mode", "live"),
        ("limits", {"max_tool_calls": 1}),
    ],
)
async def test_version_incompatibility_409(api, database, field, value):
    first = await launch(api)
    await worker.run_one(database.engine)
    async with AsyncSession(database.engine) as session, session.begin():
        source = await session.get(Run, UUID(first["id"]))
        other = Run(
            workspace_id=source.workspace_id,
            dataset_id=source.dataset_id,
            config_id=source.config_id,
            idempotency_key=uuid4().hex,
            request_hash="0" * 64,
            status="failed",
            snapshot={**source.snapshot, field: value},
            total_cases=12,
        )
        session.add(other)
        await session.flush()
        other_id = other.id
    response = await api.client.get(
        f"/api/v1/comparisons?baseline={first['id']}&candidate={other_id}",
        headers=api.headers(),
    )
    assert response.status_code == 409
    assert field + "_mismatch" in response.json()["message"]


async def test_failed_case_records_safe_error(api, database, monkeypatch):
    original = worker.run_case

    def fail_one(case, provider):
        if case.case_id == "customer-read":
            raise RuntimeError("private error payload")
        return original(case, provider)

    monkeypatch.setattr(worker, "run_case", fail_one)
    run = await launch(api)
    await worker.run_one(database.engine)
    result = await report(api, run)
    assert result["failed_cases"] == 1 and result["completed_cases"] == 11
    cases = await api.client.get(
        f"/api/v1/runs/{run['id']}/cases", headers=api.headers()
    )
    assert "private error payload" not in cases.text


async def test_demo_seed_idempotent(api):
    first = await seed("one", BASIC)
    assert first == await seed("one", BASIC)
    assert set(first["runs"]) == {"safe", "unsafe", "deny-all"}


async def test_upgrade_prior_queued_run(database):
    await migration(database.url, "downgrade", "0001")
    run_id, job_id = uuid4(), uuid4()
    async with AsyncSession(database.engine) as session, session.begin():
        await bootstrap(session, "upgrade")
        workspace_id = await session.scalar(
            select(Workspace.id).where(Workspace.name == "upgrade")
        )
        dataset, _ = await import_dataset(session, workspace_id, BASIC.read_bytes())
        config, _ = await create_config(session, workspace_id, ConfigInput())
        snapshot = {
            "dataset_hash": dataset.content_hash,
            "config_hash": config.content_hash,
            "policy_version": "1",
            "evaluator_version": "1",
            "tool_schema_version": "1",
            "dataset_schema_version": 1,
            "mode": "fake",
            "repetitions": 1,
            "limits": {"max_tool_calls": 20},
            "code_revision": "unknown",
        }
        await session.execute(
            text(
                "INSERT INTO runs (id,workspace_id,dataset_id,config_id,"
                "idempotency_key,request_hash,snapshot,status) VALUES "
                "(:id,:workspace,:dataset,:config,'prior','hash',"
                "CAST(:snapshot AS jsonb),'queued')"
            ),
            {
                "id": run_id,
                "workspace": workspace_id,
                "dataset": dataset.id,
                "config": config.id,
                "snapshot": json.dumps(snapshot),
            },
        )
        await session.execute(
            text(
                "INSERT INTO run_jobs (id,workspace_id,run_id,status) "
                "VALUES (:id,:workspace,:run,'queued')"
            ),
            {"id": job_id, "workspace": workspace_id, "run": run_id},
        )
    await migration(database.url, "upgrade", "head")
    async with AsyncSession(database.engine) as session:
        run = await session.get(Run, run_id)
        assert (
            run.snapshot == snapshot
            and run.total_cases == 12
            and run.status == "queued"
        )
    assert await worker.run_one(database.engine)
    async with AsyncSession(database.engine) as session:
        assert (await session.get(Run, run_id)).status == "completed"


async def test_hostile_case_text_preserved_as_json_and_case_pagination(api, database):
    data = json.loads(BASIC.read_text())
    hostile = '<img src=x onerror="alert(1)"><script>alert(2)</script>'
    data["cases"][0]["case_id"] = hostile
    data["cases"][0]["public_input"] = hostile
    run = await launch(api, repetitions=2, content=data)
    await worker.run_one(database.engine)
    first = await api.client.get(
        f"/api/v1/runs/{run['id']}/cases", headers=api.headers()
    )
    second = await api.client.get(
        f"/api/v1/runs/{run['id']}/cases",
        params={"cursor": first.json()["next_cursor"]},
        headers=api.headers(),
    )
    items = first.json()["items"] + second.json()["items"]
    assert len(first.json()["items"]) == 20 and len(items) == 24
    assert len({item["id"] for item in items}) == 24
    assert sum(item["case_id"] == hostile for item in items) == 2
    assert first.headers["content-type"] == "application/json"
    assert first.headers["x-content-type-options"] == "nosniff"


@pytest.mark.parametrize("table", ["attempt", "result"])
async def test_case_evidence_tenant_foreign_keys(api, database, table):
    run = await launch(api)
    await worker.run_one(database.engine)
    async with AsyncSession(database.engine) as session:
        workspace = await session.scalar(
            select(Workspace.id).where(Workspace.name == "two")
        )
        values = {
            "workspace_id": workspace,
            "run_id": UUID(run["id"]),
            "case_id": "customer-read",
            "repetition": 1,
        }
        row = (
            CaseAttempt(
                **values,
                lease_generation=1,
                worker_version=PACKAGE_VERSION,
                status="running",
                started_at=datetime.now(UTC),
            )
            if table == "attempt"
            else CaseResult(**values, status="failed", score=None)
        )
        with pytest.raises(IntegrityError) as caught:
            async with session.begin_nested():
                session.add(row)
                await session.flush()
        assert caught.value.orig.sqlstate == "23503"
