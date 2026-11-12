"""Every operator route: roles, revocation, tenant ownership and bounded input."""

from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from test_persistence import seed
from test_worker import launch

from scopecheck.auth import hash_token, revoke
from scopecheck.runs import worker
from scopecheck.runs.models import ApiToken

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


def read_paths(dataset_id, run_id):
    return [
        "/api/v1/datasets",
        f"/api/v1/datasets/{dataset_id}",
        "/api/v1/runs",
        f"/api/v1/runs/{run_id}",
        f"/api/v1/runs/{run_id}/cases",
        f"/api/v1/runs/{run_id}/report",
        f"/api/v1/comparisons?baseline={run_id}&candidate={run_id}",
    ]


def writes(refs):
    return [
        ("/api/v1/datasets", {"schema_version": 1, "cases": []}),
        ("/api/v1/configs", {"fixture": "safe"}),
        ("/api/v1/runs", refs),
    ]


async def test_operator_route_inventory_roles_and_revocation(api, database):
    refs = await seed(api)
    run = await launch(api)
    assert await worker.run_one(database.engine)
    paths = read_paths(refs["dataset_id"], run["id"])
    # Fail when a new operator route is added without extending this audit.
    from scopecheck.api.routes import router

    audited = {
        ("GET", "/api/v1/datasets"),
        ("POST", "/api/v1/datasets"),
        ("GET", "/api/v1/datasets/{dataset_id}"),
        ("POST", "/api/v1/configs"),
        ("GET", "/api/v1/runs"),
        ("POST", "/api/v1/runs"),
        ("GET", "/api/v1/runs/{run_id}"),
        ("GET", "/api/v1/runs/{run_id}/cases"),
        ("GET", "/api/v1/runs/{run_id}/report"),
        ("GET", "/api/v1/comparisons"),
    }
    assert {
        (method, route.path)
        for route in router.routes
        if route.path.startswith("/api/")
        for method in route.methods
    } == audited
    for path in paths:
        for role in ("reader", "editor"):
            assert (
                await api.client.get(path, headers=api.headers(role))
            ).status_code == 200
        assert (await api.client.get(path)).status_code == 401
    for path, body in writes(refs):
        assert (
            await api.client.post(
                path,
                json=body,
                headers=api.headers("reader", **{"Idempotency-Key": "role-audit"}),
            )
        ).status_code == 403
        assert (
            await api.client.post(
                path, json=body, headers={"Idempotency-Key": "role-audit"}
            )
        ).status_code == 401
    async with AsyncSession(database.engine) as session, session.begin():
        token = await session.scalar(
            select(ApiToken).where(ApiToken.token_hash == hash_token(api.editor))
        )
        assert await revoke(session, "one", token.id)
    for path in paths:
        assert (await api.client.get(path, headers=api.headers())).status_code == 401
    for path, body in writes(refs):
        assert (
            await api.client.post(
                path, json=body, headers=api.headers(**{"Idempotency-Key": "revoked"})
            )
        ).status_code == 401


async def test_foreign_ids_are_indistinguishable_and_lists_are_scoped(api, database):
    refs = await seed(api)
    run = await launch(api)
    await worker.run_one(database.engine)
    for suffix in ("", "/cases", "/report"):
        foreign = await api.client.get(
            f"/api/v1/runs/{run['id']}{suffix}", headers=api.headers("other")
        )
        missing = await api.client.get(
            f"/api/v1/runs/{uuid4()}{suffix}", headers=api.headers("other")
        )
        assert foreign.status_code == missing.status_code == 404
        assert {k: v for k, v in foreign.json().items() if k != "request_id"} == {
            k: v for k, v in missing.json().items() if k != "request_id"
        }
    for path in (
        f"/api/v1/datasets/{refs['dataset_id']}",
        f"/api/v1/datasets?cursor={refs['dataset_id']}",
        f"/api/v1/runs?cursor={run['id']}",
        f"/api/v1/comparisons?baseline={run['id']}&candidate={uuid4()}",
        f"/api/v1/comparisons?baseline={uuid4()}&candidate={run['id']}",
    ):
        response = await api.client.get(path, headers=api.headers("other"))
        assert response.status_code == 404
        assert response.json()["message"] == "Resource not found"
    for path in ("/api/v1/runs", "/api/v1/datasets"):
        response = await api.client.get(path, headers=api.headers("other"))
        assert response.json()["items"] == []
    other_refs = await seed(api, "other")
    for body in (
        refs,
        {**other_refs, "dataset_id": refs["dataset_id"]},
        {**other_refs, "config_id": refs["config_id"]},
    ):
        result = await api.client.post(
            "/api/v1/runs",
            json=body,
            headers=api.headers("other", **{"Idempotency-Key": uuid4().hex}),
        )
        assert result.status_code == 404
    own_cases = await api.client.get(
        f"/api/v1/runs/{run['id']}/cases", headers=api.headers()
    )
    second = await launch(api)
    cursor = own_cases.json()["items"][0]["id"]
    response = await api.client.get(
        f"/api/v1/runs/{second['id']}/cases?cursor={cursor}", headers=api.headers()
    )
    assert response.status_code == 404


async def test_forged_payload_authority_and_size_limits(api):
    refs = await seed(api)
    for field in (
        "workspace_id",
        "actor_id",
        "merchant_id",
        "role",
        "owner_authorized",
        "prompt",
        "model",
        "url",
    ):
        for path, base in [("/api/v1/configs", {}), ("/api/v1/runs", refs)]:
            response = await api.client.post(
                path,
                json={**base, field: "forged"},
                headers=api.headers(**{"Idempotency-Key": uuid4().hex}),
            )
            assert response.status_code == 422
            assert "forged" not in response.text
    for path, size in [
        ("/api/v1/configs", 16_385),
        ("/api/v1/runs", 16_385),
        ("/api/v1/datasets", 1024 * 1024 + 1),
    ]:

        async def chunks(size=size):
            yield b" " * (size // 2)
            yield b" " * (size - size // 2)

        response = await api.client.post(
            path,
            content=chunks(),
            headers=api.headers(
                **{"Content-Length": "1", "Idempotency-Key": "oversized"}
            ),
        )
        assert response.status_code == 413
        assert set(response.json()) == {"code", "message", "request_id"}
