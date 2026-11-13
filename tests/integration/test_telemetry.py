import json
from uuid import UUID

import httpx
import pytest
from prometheus_client.parser import text_string_to_metric_families
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from test_worker import launch

from scopecheck.app import create_app
from scopecheck.runs import worker
from scopecheck.runs.models import RunJob

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
TOKEN = "synthetic-private-metrics-token-for-tests"


async def test_linked_api_worker_provider_and_fake_timeout(
    api, database, tmp_path, monkeypatch
):
    monkeypatch.setenv("SCOPECHECK_TRACE_DIRECTORY", str(tmp_path))
    monkeypatch.setenv("SCOPECHECK_METRICS_TOKEN", TOKEN)
    app = create_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        original = api.client
        api.client = client
        run = await launch(api)
        api.client = original
        async with AsyncSession(database.engine) as session:
            job = await session.scalar(
                select(RunJob).where(RunJob.run_id == UUID(run["id"]))
            )
            assert job.trace_parent is not None
            trace_id = job.trace_parent.split("-")[1]

        def timeout(*args):
            raise TimeoutError("PRIVATE synthetic payload must not be captured")

        monkeypatch.setattr(worker, "run_case", timeout)
        assert await worker.run_one(database.engine)
        records = [
            json.loads(line)
            for line in (tmp_path / (trace_id + ".jsonl")).read_text().splitlines()
        ]
        http = next(record for record in records if record["name"] == "http.request")
        run_span = next(record for record in records if record["name"] == "worker.run")
        assert run_span["parent_span_id"] == http["span_id"]
        cases = {
            record["span_id"]: record
            for record in records
            if record["name"] == "case.execute"
        }
        providers = [
            record for record in records if record["name"] == "provider.request"
        ]
        assert len(providers) == 12
        assert all(
            p["parent_span_id"] in cases and p["attributes"]["error"] == "timeout"
            for p in providers
        )
        assert all(c["parent_span_id"] == run_span["span_id"] for c in cases.values())
        assert "PRIVATE" not in json.dumps(records)
        report = await client.get(
            f"/api/v1/runs/{run['id']}/report", headers=api.headers()
        )
        assert report.json()["status"] == "completed_with_errors"
        metrics = await client.get(
            "/metrics", headers={"Authorization": "Bearer " + TOKEN}
        )
        assert metrics.status_code == 200
        assert (
            'scopecheck_provider_failures_total{error="timeout"} 12.0' in metrics.text
        )
        assert (
            run["id"] not in metrics.text
            and trace_id not in metrics.text
            and TOKEN not in metrics.text
        )
        for family in text_string_to_metric_families(metrics.text):
            for sample in family.samples:
                assert set(sample.labels) <= {"route", "status", "error", "le"}
        # Persist a sanitized evidence artifact outside test DB lifetime for inspection.
        from pathlib import Path

        evidence = Path("outputs/milestone8-linked-trace.json")
        evidence.parent.mkdir(exist_ok=True)
        evidence.write_text(
            json.dumps(
                {
                    "trace_id": trace_id,
                    "run_status": report.json()["status"],
                    "spans": records,
                    "failure_count": 12,
                },
                indent=2,
            )
        )
    await app.state.engine.dispose()


async def test_private_metrics_requires_separate_key_and_public_overrides_it(
    api, database, monkeypatch
):
    monkeypatch.setenv("SCOPECHECK_METRICS_TOKEN", TOKEN)
    for public in (False, True):
        monkeypatch.setenv("SCOPECHECK_PUBLIC_DEMO", str(public))
        app = create_app()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            for headers in ({}, api.headers(), {"Authorization": "Bearer " + TOKEN}):
                response = await client.get("/metrics", headers=headers)
                expected = (
                    404
                    if public
                    else 200
                    if headers.get("Authorization") == "Bearer " + TOKEN
                    else 401
                )
                assert response.status_code == expected
        await app.state.engine.dispose()


async def test_database_down_readiness_is_safe_and_traced(tmp_path, monkeypatch):
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        unused_port = sock.getsockname()[1]
    monkeypatch.setenv(
        "SCOPECHECK_DATABASE_URL",
        f"postgresql+psycopg://unavailable:PRIVATE@127.0.0.1:{unused_port}/absent",
    )
    monkeypatch.setenv("SCOPECHECK_TRACE_DIRECTORY", str(tmp_path))
    app = create_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/health/ready")
        assert response.status_code == 503
        assert response.json()["code"] == "database_unavailable"
        assert "PRIVATE" not in response.text
        assert (await client.get("/health/live")).status_code == 200
    records = [
        json.loads(line)
        for path in tmp_path.glob("*.jsonl")
        for line in path.read_text().splitlines()
    ]
    ready = next(r for r in records if r["attributes"].get("route") == "/health/ready")
    assert ready["attributes"]["error"] == "database_unavailable"
    assert "PRIVATE" not in json.dumps(records)
    await app.state.engine.dispose()


async def test_automatic_retention_failure_does_not_stop_evaluation(
    api, database, tmp_path, monkeypatch
):
    run = await launch(api)
    monkeypatch.setenv("SCOPECHECK_TRACE_DIRECTORY", str(tmp_path))
    attempted = []

    def unavailable(*args):
        attempted.append(True)
        raise OSError("PRIVATE path diagnostic")

    monkeypatch.setattr(worker, "maintain", unavailable)
    await worker.serve(once=True)
    assert attempted == [True]
    response = await api.client.get(f"/api/v1/runs/{run['id']}", headers=api.headers())
    assert response.json()["status"] == "completed"
