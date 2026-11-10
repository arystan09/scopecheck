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


async def test_api_worker_demo_smoke_and_versions(database):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = {**os.environ, "SCOPECHECK_DATABASE_URL": database.url}
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
    finally:
        for process in processes:
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=5)
                except TimeoutError:
                    process.kill()
                    await process.wait()
