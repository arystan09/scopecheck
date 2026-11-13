"""Real loopback HTTP plus a separate worker process against a disposable database."""

import asyncio
import json
import os
import socket
import time

import httpx
import pytest

from scopecheck.version import PACKAGE_VERSION

pytestmark = [pytest.mark.e2e, pytest.mark.asyncio]


async def test_api_worker_demo_smoke_and_versions(database, tmp_path):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = {
        **os.environ,
        "SCOPECHECK_DATABASE_URL": database.url,
        "SCOPECHECK_TRACE_DIRECTORY": str(tmp_path),
    }
    env.pop("SCOPECHECK_DEMO_TOKEN", None)
    processes = []
    try:
        for args in (
            (
                "uvicorn",
                "scopecheck.app:create_app",
                "--factory",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
            ),
            ("scopecheck", "worker"),
        ):
            processes.append(
                await asyncio.create_subprocess_exec(
                    *args,
                    env=env,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
            )
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}", timeout=2
        ) as client:
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                try:
                    if (await client.get("/health/ready")).status_code == 200:
                        break
                except httpx.RequestError:
                    pass
                await asyncio.sleep(0.1)
            else:
                pytest.fail("API did not become ready before deadline")
            assert (await client.get("/health/version")).json()[
                "package_version"
            ] == PACKAGE_VERSION
            page = await client.get("/ui")
            assert "SYNTHETIC" in page.text and "set-cookie" not in page.headers
        bootstrap = await asyncio.create_subprocess_exec(
            "scopecheck",
            "db",
            "bootstrap",
            "--workspace",
            "local",
            env=env,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        assert await bootstrap.wait() == 0  # discard one-time token, never log it
        seed = await asyncio.create_subprocess_exec(
            "scopecheck",
            "demo",
            "seed",
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        output, _ = await seed.communicate()
        assert seed.returncode == 0 and len(json.loads(output)["runs"]) == 3
        smoke = await asyncio.create_subprocess_exec(
            "uv",
            "run",
            "python",
            "scripts/demo_smoke.py",
            "--base-url",
            f"http://127.0.0.1:{port}",
            "--timeout",
            "30",
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        output, error = await asyncio.wait_for(smoke.communicate(), timeout=40)
        assert smoke.returncode == 0, error.decode()
        result = json.loads(output)
        assert (
            result["status"] == "passed"
            and result["package_version"] == PACKAGE_VERSION
        )
        assert result["legitimate_successes"] == {"safe": 6, "unsafe": 2, "deny-all": 0}
        records = [
            json.loads(line)
            for path in tmp_path.glob("*.jsonl")
            for line in path.read_text().splitlines()
        ]
        spans = {record["span_id"]: record for record in records}
        # CLI-seeded runs legitimately have no API parent. Select the HTTP run
        # returned by the smoke flow, not an arbitrary provider span/file order.
        worker_span = next(
            record
            for record in records
            if record["name"] == "worker.run"
            and record["attributes"]["run_id"] == result["runs"]["safe"]
        )
        api_span = spans[worker_span["parent_span_id"]]
        assert api_span["attributes"]["route"] == "/api/v1/runs"
        assert api_span["attributes"]["http_status"] == 201
        case = next(
            record
            for record in records
            if record["name"] == "case.execute"
            and record["parent_span_id"] == worker_span["span_id"]
        )
        provider = next(
            record
            for record in records
            if record["name"] == "provider.request"
            and record["parent_span_id"] == case["span_id"]
        )
        chain = [api_span, worker_span, case, provider]
        assert [record["name"] for record in chain] == [
            "http.request",
            "worker.run",
            "case.execute",
            "provider.request",
        ]
        assert len({record["trace_id"] for record in chain}) == 1
        from pathlib import Path

        evidence = Path("outputs/milestone8-process-trace.json")
        evidence.parent.mkdir(exist_ok=True)
        evidence.write_text(json.dumps(chain, indent=2))
    finally:
        for process in processes:
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=5)
                except TimeoutError:
                    process.kill()
                    await process.wait()


async def test_public_mode_http_and_worker_process(database):
    from uuid import UUID

    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import AsyncSession

    from scopecheck.auth import issue_token
    from scopecheck.datasets.demo import DEMO_WORKSPACE
    from scopecheck.runs.models import Run

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = {
        **os.environ,
        "SCOPECHECK_DATABASE_URL": database.url,
        "SCOPECHECK_PUBLIC_DEMO": "true",
    }
    for name in ("OPENAI_API_KEY", "SCOPECHECK_OPENAI_API_KEY"):
        env.pop(name, None)
    processes = []
    try:
        for args in (
            (
                "uvicorn",
                "scopecheck.app:create_app",
                "--factory",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
            ),
            ("scopecheck", "worker"),
        ):
            processes.append(
                await asyncio.create_subprocess_exec(
                    *args,
                    env=env,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
            )
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}", timeout=2
        ) as client:
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                try:
                    if (await client.get("/health/ready")).status_code == 200:
                        break
                except httpx.RequestError:
                    pass
                await asyncio.sleep(0.1)
            else:
                pytest.fail("Public API did not become ready")
            response = await client.post("/demo/runs", json={})
            assert response.status_code == 202
            id = UUID(response.json()["id"])
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                async with AsyncSession(database.engine) as session:
                    run = await session.scalar(select(Run).where(Run.id == id))
                    if run.status == "completed":
                        break
                    assert run.status in {"queued", "running"}
                await asyncio.sleep(0.1)
            else:
                pytest.fail("Public fake run did not complete before deadline")
            async with AsyncSession(database.engine) as session, session.begin():
                token = await issue_token(session, DEMO_WORKSPACE, "reader")
            headers = {"Authorization": "Bearer " + token}
            result = await client.get(f"/api/v1/runs/{id}/report", headers=headers)
            assert result.status_code == 200
            assert result.json()["completed_cases"] == 24
            assert result.json()["legitimate_task_success"]["numerator"] == 10
            assert result.json()["executed_violations"] == 0
            assert (
                await client.post("/demo/runs", json={"mode": "live"})
            ).status_code == 422
            assert (
                await client.post("/api/v1/configs", json={}, headers=headers)
            ).status_code == 403
    finally:
        for process in processes:
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), 5)
                except TimeoutError:
                    process.kill()
                    await process.wait()
