import asyncio
import json
import os
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from conftest import migration
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from scopecheck.auth import hash_token, revoke
from scopecheck.datasets.canonicalize import content_hash, load_dataset
from scopecheck.datasets.schemas import Dataset
from scopecheck.datasets.service import import_dataset
from scopecheck.evaluation.scorer import replay
from scopecheck.runs.models import (
    ApiToken,
    AssistantConfig,
    DatasetVersion,
    Run,
    RunJob,
    Workspace,
)
from scopecheck.runs.service import RunInput, create_run

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
BASIC = Path("datasets/dev/basic.json")


async def seed(api, role="editor"):
    dataset = await api.client.post(
        "/api/v1/datasets", content=BASIC.read_bytes(), headers=api.headers(role)
    )
    config = await api.client.post(
        "/api/v1/configs", json={"mode": "fake"}, headers=api.headers(role)
    )
    assert dataset.status_code in (200, 201)
    assert config.status_code in (200, 201)
    return {"dataset_id": dataset.json()["id"], "config_id": config.json()["id"]}


async def test_clean_migration_and_repeat_upgrade(database):
    async with database.engine.connect() as connection:
        assert (
            int(await connection.scalar(text("SHOW server_version_num"))) // 10000 == 18
        )
        assert (
            await connection.scalar(text("SELECT version_num FROM alembic_version"))
            == "0003"
        )
        names = set(
            (
                await connection.execute(
                    text("SELECT tablename FROM pg_tables WHERE schemaname='public'")
                )
            ).scalars()
        )
        assert names == {
            "alembic_version",
            "workspaces",
            "api_tokens",
            "dataset_versions",
            "assistant_configs",
            "runs",
            "run_jobs",
            "case_attempts",
            "case_results",
            "call_reservations",
        }
    await migration(database.url, "upgrade", "head")
    await migration(database.url, "check")


async def test_prior_stage_fixture_import_and_upgrade(api, database):
    dataset = load_dataset(BASIC)
    response = await api.client.post(
        "/api/v1/datasets", content=BASIC.read_bytes(), headers=api.headers()
    )
    assert response.status_code == 201
    assert response.json()["content_hash"] == content_hash(dataset)
    await migration(database.url, "upgrade", "head")
    retrieved = await api.client.get(
        "/api/v1/datasets/" + response.json()["id"], headers=api.headers()
    )
    stored = Dataset.model_validate(retrieved.json()["content"])
    assert content_hash(stored) == content_hash(dataset)
    assert replay(stored, "safe").legitimate_task_success.numerator == 6


async def test_auth_roles_revocation_and_errors(api, database):
    for token in (None, "Bearer bad", "Basic anything", "Bearer " + "a" * 43):
        response = await api.client.get(
            "/api/v1/datasets", headers={"Authorization": token} if token else {}
        )
        assert response.status_code == 401
        assert set(response.json()) == {"code", "message", "request_id"}
        assert response.headers["x-request-id"] == response.json()["request_id"]
        assert response.headers["www-authenticate"] == "Bearer"
    for path, body in (("/datasets", {}), ("/configs", {}), ("/runs", {})):
        response = await api.client.post(
            "/api/v1" + path, json=body, headers=api.headers("reader")
        )
        assert response.status_code == 403
    assert (
        await api.client.get("/api/v1/datasets", headers=api.headers("reader"))
    ).status_code == 200
    async with AsyncSession(database.engine) as session, session.begin():
        token_id = await session.scalar(
            select(ApiToken.id).where(ApiToken.token_hash == hash_token(api.editor))
        )
        assert not await revoke(session, "two", token_id)
        assert await revoke(session, "one", token_id)
        assert not await revoke(session, "one", token_id)
        stored = list((await session.scalars(select(ApiToken.token_hash))).all())
        assert all(len(value) == 64 for value in stored)
        assert api.editor not in stored and api.reader not in stored
    response = await api.client.get("/api/v1/datasets", headers=api.headers())
    assert response.status_code == 401


async def test_ownership_reads_and_api_references(api):
    one, two = await seed(api), await seed(api, "other")
    assert one["dataset_id"] != two["dataset_id"]
    assert one["config_id"] != two["config_id"]
    for body in (
        {**one, "dataset_id": two["dataset_id"]},
        {**one, "config_id": two["config_id"]},
    ):
        result = await api.client.post(
            "/api/v1/runs",
            json=body,
            headers=api.headers(**{"Idempotency-Key": "foreign"}),
        )
        assert result.status_code == 404
    foreign = await api.client.get(
        "/api/v1/datasets/" + two["dataset_id"], headers=api.headers()
    )
    unknown = await api.client.get(
        "/api/v1/datasets/" + str(uuid4()), headers=api.headers()
    )
    assert foreign.status_code == unknown.status_code == 404
    assert foreign.json()["message"] == unknown.json()["message"]
    run = await api.client.post(
        "/api/v1/runs", json=one, headers=api.headers(**{"Idempotency-Key": "owned"})
    )
    run_id = run.json()["id"]
    assert (
        await api.client.get("/api/v1/runs/" + run_id, headers=api.headers("other"))
    ).status_code == 404
    assert (
        await api.client.get("/api/v1/runs/" + run_id, headers=api.headers("reader"))
    ).status_code == 200
    listed = (await api.client.get("/api/v1/runs", headers=api.headers("other"))).json()
    assert listed["items"] == []


@pytest.mark.parametrize("foreign_field", ["dataset_id", "config_id"])
async def test_composite_fk_blocks_cross_workspace_run_inserts(
    api, database, foreign_field
):
    one, two = await seed(api), await seed(api, "other")
    async with AsyncSession(database.engine) as session:
        workspace = await session.scalar(
            select(Workspace.id).where(Workspace.name == "one")
        )
        values = {key: UUID(value) for key, value in one.items()}
        values[foreign_field] = UUID(two[foreign_field])
        with pytest.raises(IntegrityError):
            async with session.begin_nested():
                session.add(
                    Run(
                        workspace_id=workspace,
                        **values,
                        idempotency_key="direct",
                        request_hash="0" * 64,
                        snapshot={},
                        status="queued",
                    )
                )
                await session.flush()


async def test_composite_fk_blocks_cross_workspace_job(api, database):
    body = await seed(api)
    run = await api.client.post(
        "/api/v1/runs", json=body, headers=api.headers(**{"Idempotency-Key": "job"})
    )
    async with AsyncSession(database.engine) as session:
        workspace = await session.scalar(
            select(Workspace.id).where(Workspace.name == "two")
        )
        with pytest.raises(IntegrityError):
            async with session.begin_nested():
                session.add(
                    RunJob(
                        workspace_id=workspace,
                        run_id=UUID(run.json()["id"]),
                        status="queued",
                    )
                )
                await session.flush()


async def test_atomic_invalid_import_limits_and_versioning(api, database):
    bad = json.loads(BASIC.read_text())
    bad["cases"][-1]["initial_state"]["orders"][0]["total_minor"] = 1.5
    assert (
        await api.client.post("/api/v1/datasets", json=bad, headers=api.headers())
    ).status_code == 422
    assert (
        await api.client.post(
            "/api/v1/datasets", content=b"{" * (1024 * 1024 + 1), headers=api.headers()
        )
    ).status_code == 413
    huge = json.loads(BASIC.read_text())
    huge["cases"] = [{**huge["cases"][0], "case_id": str(i)} for i in range(101)]
    assert (
        await api.client.post("/api/v1/datasets", json=huge, headers=api.headers())
    ).status_code == 422
    async with AsyncSession(database.engine) as session:
        assert (
            await session.scalar(select(func.count()).select_from(DatasetVersion)) == 0
        )
    original = await api.client.post(
        "/api/v1/datasets", content=BASIC.read_bytes(), headers=api.headers()
    )
    duplicate = await api.client.post(
        "/api/v1/datasets", json=json.loads(BASIC.read_text()), headers=api.headers()
    )
    assert original.status_code == 201 and duplicate.status_code == 200
    assert original.json()["id"] == duplicate.json()["id"]
    changed = json.loads(BASIC.read_text())
    changed["cases"][0]["public_input"] = "New version"
    new = await api.client.post("/api/v1/datasets", json=changed, headers=api.headers())
    assert new.status_code == 201 and new.json()["id"] != original.json()["id"]


@pytest.mark.parametrize("table", ["dataset_versions", "assistant_configs", "runs"])
@pytest.mark.parametrize("operation", ["UPDATE", "DELETE"])
async def test_database_immutability(api, database, table, operation):
    body = await seed(api)
    await api.client.post(
        "/api/v1/runs",
        json=body,
        headers=api.headers(**{"Idempotency-Key": "immutable"}),
    )
    statement = (
        f"DELETE FROM {table}"
        if operation == "DELETE"
        else f"UPDATE {table} SET created_at = now()"
    )
    async with AsyncSession(database.engine) as session:
        with pytest.raises(IntegrityError):
            await session.execute(text(statement))
        await session.rollback()
        assert await session.scalar(text(f"SELECT count(*) FROM {table}")) == 1


async def test_concurrent_idempotency_and_mismatch(api, database):
    body = await seed(api)
    headers = api.headers(**{"Idempotency-Key": "retry"})
    responses = await asyncio.gather(
        *[
            api.client.post("/api/v1/runs", json=body, headers=headers)
            for _ in range(12)
        ]
    )
    assert sorted(r.status_code for r in responses) == [200] * 11 + [201]
    assert len({r.json()["id"] for r in responses}) == 1
    changed = {**body, "repetitions": 2}
    mismatches = await asyncio.gather(
        *[
            api.client.post("/api/v1/runs", json=changed, headers=headers)
            for _ in range(5)
        ]
    )
    assert all(r.status_code == 409 for r in mismatches)
    async with AsyncSession(database.engine) as session:
        assert await session.scalar(select(func.count()).select_from(Run)) == 1
        assert await session.scalar(select(func.count()).select_from(RunJob)) == 1
    snapshot = responses[0].json()["snapshot"]
    assert snapshot["mode"] == "fake" and snapshot["code_revision"] == "unknown"
    assert snapshot["repetitions"] == 1 and snapshot["limits"] == {"max_tool_calls": 20}
    assert (
        snapshot["policy_version"]
        == snapshot["evaluator_version"]
        == snapshot["tool_schema_version"]
        == "1"
    )
    assert len(snapshot["dataset_hash"]) == len(snapshot["config_hash"]) == 64
    assert responses[0].json()["status"] == "queued"


async def test_concurrent_first_requests_with_different_hashes(api, database):
    body = await seed(api)
    headers = api.headers(**{"Idempotency-Key": "race"})
    responses = await asyncio.gather(
        *[
            api.client.post(
                "/api/v1/runs", json={**body, "repetitions": i}, headers=headers
            )
            for i in (1, 2)
        ]
    )
    assert sorted(r.status_code for r in responses) == [201, 409]
    async with AsyncSession(database.engine) as session:
        assert await session.scalar(select(func.count()).select_from(Run)) == 1
        assert await session.scalar(select(func.count()).select_from(RunJob)) == 1


async def test_run_and_job_rollback_together(api, database):
    body = await seed(api)
    async with AsyncSession(database.engine) as session:
        workspace = await session.scalar(
            select(Workspace.id).where(Workspace.name == "one")
        )
        await session.rollback()
        with pytest.raises(RuntimeError, match="force rollback"):
            async with session.begin():
                await create_run(
                    session,
                    workspace,
                    RunInput.model_validate(body),
                    "rollback",
                    "unknown",
                )
                raise RuntimeError("force rollback")
        assert await session.scalar(select(func.count()).select_from(Run)) == 0
        assert await session.scalar(select(func.count()).select_from(RunJob)) == 0


async def test_pagination_for_datasets_and_runs(api, database):
    body = await seed(api)
    async with AsyncSession(database.engine) as session, session.begin():
        workspace = await session.scalar(
            select(Workspace.id).where(Workspace.name == "one")
        )
        for i in range(24):
            content = json.loads(BASIC.read_text())
            content["cases"][0]["public_input"] = str(i)
            await import_dataset(session, workspace, json.dumps(content).encode())
            await create_run(
                session, workspace, RunInput.model_validate(body), str(i), "unknown"
            )
    for resource, count in (("datasets", 25), ("runs", 24)):
        path = "/api/v1/" + resource
        first = (await api.client.get(path, headers=api.headers())).json()
        assert len(first["items"]) == 20
        second = (
            await api.client.get(
                path, params={"cursor": first["next_cursor"]}, headers=api.headers()
            )
        ).json()
        ids = [item["id"] for item in first["items"] + second["items"]]
        assert len(ids) == len(set(ids)) == count
        assert second["next_cursor"] is None
        assert (
            await api.client.get(path, params={"limit": 101}, headers=api.headers())
        ).status_code == 422
        assert (
            await api.client.get(path, params={"limit": 0}, headers=api.headers())
        ).status_code == 422
        assert (
            await api.client.get(path, params={"cursor": "bad"}, headers=api.headers())
        ).status_code == 422


async def test_validation_fake_only_and_no_workspace_override(api):
    body = await seed(api)
    for changes in (
        {"mode": "live"},
        {"workspace_id": str(uuid4())},
        {"repetitions": True},
        {"limits": {"max_tool_calls": 0}},
        {"actor_id": "spoof"},
    ):
        result = await api.client.post(
            "/api/v1/runs",
            json={**body, **changes},
            headers=api.headers(**{"Idempotency-Key": "bad"}),
        )
        assert result.status_code == 422
        assert set(result.json()) == {"code", "message", "request_id"}
    assert (
        await api.client.post("/api/v1/runs", json=body, headers=api.headers())
    ).status_code == 422
    assert (
        await api.client.post(
            "/api/v1/configs", json={"mode": "live"}, headers=api.headers()
        )
    ).status_code == 422
    assert (await api.client.get("/health/ready")).status_code == 200


async def test_bootstrap_cli_once_and_revoke(database):
    async def cli(*args):
        process = await asyncio.create_subprocess_exec(
            "scopecheck",
            "db",
            *args,
            env={**os.environ, "SCOPECHECK_DATABASE_URL": database.url},
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await process.communicate()
        assert process.returncode == 0
        return json.loads(stdout)

    # Credentials are captured in memory only; never printed or written to disk.
    first = await cli("bootstrap", "--workspace", "local")
    assert first["created"] is True
    token = first.pop("token")
    assert len(token) == 43
    second = await cli("bootstrap", "--workspace", "local")
    assert second["created"] is False and "token" not in second
    reader = await cli("token-create", "--workspace", "local", "--role", "reader")
    assert reader["created"] is True
    async with AsyncSession(database.engine) as session:
        hashes = list((await session.scalars(select(ApiToken.token_hash))).all())
        assert token not in hashes and hash_token(token) in hashes
    result = await cli(
        "revoke", "--workspace", "local", "--token-id", first["token_id"]
    )
    assert result == {"revoked": True}


async def test_commit_failure_is_not_reported_as_success(api, database):
    body = await seed(api)
    async with database.engine.begin() as connection:
        await connection.execute(
            text(
                "CREATE CONSTRAINT TRIGGER fail_commit AFTER INSERT ON run_jobs "
                "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
                "EXECUTE FUNCTION scopecheck_reject_mutation()"
            )
        )
    response = await api.client.post(
        "/api/v1/runs",
        json=body,
        headers=api.headers(**{"Idempotency-Key": "commit-failure"}),
    )
    assert response.status_code == 503
    assert set(response.json()) == {"code", "message", "request_id"}
    assert "immutable record" not in response.text
    async with AsyncSession(database.engine) as session:
        assert await session.scalar(select(func.count()).select_from(Run)) == 0
        assert await session.scalar(select(func.count()).select_from(RunJob)) == 0


async def test_concurrent_import_and_config_deduplication(api, database):
    imports = await asyncio.gather(
        *[
            api.client.post(
                "/api/v1/datasets", content=BASIC.read_bytes(), headers=api.headers()
            )
            for _ in range(6)
        ]
    )
    configs = await asyncio.gather(
        *[
            api.client.post("/api/v1/configs", json={}, headers=api.headers())
            for _ in range(6)
        ]
    )
    for responses in (imports, configs):
        assert sorted(r.status_code for r in responses) == [200] * 5 + [201]
        assert len({r.json()["id"] for r in responses}) == 1
    async with AsyncSession(database.engine) as session:
        assert (
            await session.scalar(select(func.count()).select_from(DatasetVersion)) == 1
        )
        assert (
            await session.scalar(select(func.count()).select_from(AssistantConfig)) == 1
        )


async def test_workspace_keys_are_independent_and_snapshots_do_not_drift(api):
    one, two = await seed(api), await seed(api, "other")
    first = await api.client.post(
        "/api/v1/runs",
        json=one,
        headers=api.headers(**{"Idempotency-Key": "shared-key"}),
    )
    second = await api.client.post(
        "/api/v1/runs",
        json=two,
        headers=api.headers("other", **{"Idempotency-Key": "shared-key"}),
    )
    assert first.status_code == second.status_code == 201
    assert first.json()["id"] != second.json()["id"]
    config = await api.client.post(
        "/api/v1/configs", json={"fixture": "unsafe"}, headers=api.headers()
    )
    assert config.status_code == 201
    retrieved = await api.client.get(
        "/api/v1/runs/" + first.json()["id"], headers=api.headers()
    )
    assert retrieved.json()["snapshot"] == first.json()["snapshot"]
    assert retrieved.json()["snapshot"]["config_hash"] != config.json()["content_hash"]


async def test_import_inclusive_size_and_case_limits(api):
    raw = BASIC.read_bytes()
    padded = raw + b" " * (1024 * 1024 - len(raw))
    assert (
        await api.client.post("/api/v1/datasets", content=padded, headers=api.headers())
    ).status_code == 201
    content = json.loads(raw)
    content["cases"] = [{**content["cases"][0], "case_id": str(i)} for i in range(100)]
    response = await api.client.post(
        "/api/v1/datasets", json=content, headers=api.headers()
    )
    assert response.status_code == 201 and response.json()["case_count"] == 100


async def test_readiness_fails_on_schema_mismatch(api, database):
    async with database.engine.begin() as connection:
        await connection.execute(text("UPDATE alembic_version SET version_num='wrong'"))
    response = await api.client.get("/health/ready")
    assert response.status_code == 503
    assert response.json()["code"] == "schema_not_ready"
    assert (await api.client.get("/health/live")).status_code == 200


async def test_normalized_retry_and_foreign_cursor(api):
    one, two = await seed(api), await seed(api, "other")
    headers = api.headers(**{"Idempotency-Key": "normalized"})
    first = await api.client.post("/api/v1/runs", json=one, headers=headers)
    explicit = {
        **one,
        "mode": "fake",
        "repetitions": 1,
        "limits": {"max_tool_calls": 20},
    }
    second = await api.client.post("/api/v1/runs", json=explicit, headers=headers)
    assert first.status_code == 201 and second.status_code == 200
    assert first.json() == second.json()
    cursor = await api.client.get(
        "/api/v1/datasets", params={"cursor": two["dataset_id"]}, headers=api.headers()
    )
    assert cursor.status_code == 404


async def test_malformed_imports_are_atomic_and_do_not_echo_payload(api, database):
    for raw in (
        b"\xff",
        b'{"secret":"do-not-echo"}',
        b"[" * 2000 + b"]" * 2000,
        b'{"schema_version":1,"schema_version":1,"cases":[]}',
    ):
        response = await api.client.post(
            "/api/v1/datasets", content=raw, headers=api.headers()
        )
        assert response.status_code == 422
        assert set(response.json()) == {"code", "message", "request_id"}
        assert "do-not-echo" not in response.text
    async with AsyncSession(database.engine) as session:
        assert (
            await session.scalar(select(func.count()).select_from(DatasetVersion)) == 0
        )
