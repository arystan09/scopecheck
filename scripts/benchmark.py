"""Disposable PostgreSQL + real loopback HTTP; no model execution or paid requests."""

import argparse
import asyncio
import json
import math
import os
import platform
import socket
import subprocess
import sys
import time
from importlib.metadata import version
from pathlib import Path
from uuid import uuid4

import httpx
import psycopg
from psycopg import sql
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession

from scopecheck.auth import bootstrap
from scopecheck.db import create_engine
from scopecheck.settings import Settings


def hardware():
    memory = None
    processor = platform.processor() or platform.machine()
    if sys.platform == "darwin":
        memory = int(subprocess.check_output(["sysctl", "-n", "hw.memsize"]))
        processor = subprocess.check_output(
            ["sysctl", "-n", "machdep.cpu.brand_string"], text=True
        ).strip()
    elif hasattr(os, "sysconf"):
        memory = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    return {
        "os": platform.platform(),
        "architecture": platform.machine(),
        "cpu": processor,
        "logical_cpus": os.cpu_count(),
        "memory_bytes": memory,
    }


async def phase(base, token, count, concurrency, refs, ids, kind):
    queue = asyncio.Queue()
    for index in range(count):
        queue.put_nowait(index)
    samples, errors = [], {}

    async def client_worker():
        async with httpx.AsyncClient(
            base_url=base,
            headers={"Authorization": "Bearer " + token},
            timeout=10,
            limits=httpx.Limits(max_connections=1),
        ) as client:
            while not queue.empty():
                index = queue.get_nowait()
                started = time.perf_counter()
                status = "transport_error"
                try:
                    if kind == "read":
                        response = await client.get(
                            "/api/v1/runs/" + ids[index % len(ids)]
                        )
                    else:
                        response = await client.post(
                            "/api/v1/runs",
                            json=refs,
                            headers={"Idempotency-Key": "measured-" + str(index)},
                        )
                    status = str(response.status_code)
                    if response.status_code != (200 if kind == "read" else 201):
                        errors[status] = errors.get(status, 0) + 1
                except httpx.RequestError:
                    errors[status] = errors.get(status, 0) + 1
                finally:
                    samples.append((time.perf_counter() - started) * 1000)

    started = time.perf_counter()
    await asyncio.gather(*(client_worker() for _ in range(concurrency)))
    duration = time.perf_counter() - started
    samples.sort()
    p95 = samples[math.ceil(len(samples) * 0.95) - 1]
    target = 300 if kind == "read" else 500
    return {
        "samples": len(samples),
        "clients": concurrency,
        "duration_seconds": duration,
        "p50_ms": samples[math.ceil(len(samples) * 0.5) - 1],
        "p95_ms": p95,
        "max_ms": max(samples),
        "error_count": sum(errors.values()),
        "errors": errors,
        "target_p95_ms": target,
        "target_met": p95 < target,
        "latencies_include_errors": True,
    }


async def benchmark(args):
    raw = os.environ.get("SCOPECHECK_TEST_ADMIN_URL")
    if not raw:
        raise ValueError(
            "Set SCOPECHECK_TEST_ADMIN_URL to local disposable PostgreSQL 18"
        )
    parsed = make_url(raw)
    if parsed.drivername != "postgresql+psycopg" or parsed.host not in {
        "localhost",
        "127.0.0.1",
        "::1",
    }:
        raise ValueError("Only local PostgreSQL administration is allowed")
    admin_dsn = parsed.set(drivername="postgresql").render_as_string(
        hide_password=False
    )
    name = "scopecheck_bench_" + uuid4().hex
    url = parsed.set(database=name).render_as_string(hide_password=False)
    dsn = parsed.set(drivername="postgresql", database=name).render_as_string(
        hide_password=False
    )
    server = None
    engine = None
    started = time.perf_counter()
    async with await psycopg.AsyncConnection.connect(
        admin_dsn, autocommit=True
    ) as admin:
        row = await (await admin.execute("SHOW server_version_num")).fetchone()
        if int(row[0]) // 10000 != 18:
            raise ValueError("PostgreSQL 18 required")
        await admin.execute(
            sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(
                sql.Identifier(name)
            )
        )
    env = {
        **os.environ,
        "SCOPECHECK_DATABASE_URL": url,
        "SCOPECHECK_PUBLIC_DEMO": "false",
        "SCOPECHECK_COLLECTOR": "disabled",
    }
    for field in (
        "OPENAI_API_KEY",
        "SCOPECHECK_OPENAI_API_KEY",
        "SCOPECHECK_METRICS_TOKEN",
        "SCOPECHECK_TRACE_DIRECTORY",
    ):
        env.pop(field, None)
    try:
        migration = await asyncio.create_subprocess_exec(
            "alembic",
            "upgrade",
            "head",
            env=env,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        if await migration.wait() != 0:
            raise RuntimeError("Disposable migration failed")
        engine = create_engine(Settings(database_url=url, public_demo=False))
        async with AsyncSession(engine) as session, session.begin():
            token = await bootstrap(session, "benchmark")
            assert token is not None
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        base = f"http://127.0.0.1:{port}"
        server = await asyncio.create_subprocess_exec(
            "uvicorn",
            "scopecheck.app:create_app",
            "--factory",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--no-access-log",
            env=env,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        async with httpx.AsyncClient(
            base_url=base, headers={"Authorization": "Bearer " + token}, timeout=10
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
                raise RuntimeError("API readiness deadline exceeded")
            dataset = await client.post(
                "/api/v1/datasets", content=Path("datasets/dev/basic.json").read_bytes()
            )
            config = await client.post("/api/v1/configs", json={"fixture": "safe"})
            if dataset.status_code != 201 or config.status_code != 201:
                raise RuntimeError("Seed setup failed")
            refs = {
                "dataset_id": dataset.json()["id"],
                "config_id": config.json()["id"],
            }
            ids = []
            for index in range(args.runs):
                response = await client.post(
                    "/api/v1/runs",
                    json=refs,
                    headers={"Idempotency-Key": "seed-" + str(index)},
                )
                if response.status_code != 201:
                    raise RuntimeError("Seed run failed")
                ids.append(response.json()["id"])
            for id in ids[:10]:
                if (await client.get("/api/v1/runs/" + id)).status_code != 200:
                    raise RuntimeError("Read warmup failed")
        async with await psycopg.AsyncConnection.connect(dsn) as db:
            size_before = (
                await (
                    await db.execute("SELECT pg_database_size(current_database())")
                ).fetchone()
            )[0]
            pg_version = (await (await db.execute("SHOW server_version")).fetchone())[0]
        reads = await phase(base, token, args.runs, args.concurrency, refs, ids, "read")
        enqueue = await phase(
            base, token, args.runs, args.concurrency, refs, ids, "enqueue"
        )
        async with await psycopg.AsyncConnection.connect(dsn) as db:
            size_after = (
                await (
                    await db.execute("SELECT pg_database_size(current_database())")
                ).fetchone()
            )[0]
            stored = (await (await db.execute("SELECT count(*) FROM runs")).fetchone())[
                0
            ]
        return {
            "synthetic": True,
            "hardware": hardware(),
            "software": {
                name: version(name)
                for name in (
                    "scopecheck",
                    "fastapi",
                    "uvicorn",
                    "sqlalchemy",
                    "psycopg",
                    "opentelemetry-sdk",
                )
            },
            "python": platform.python_version(),
            "postgresql": pg_version,
            "initial_stored_runs": args.runs,
            "final_stored_runs": stored,
            "database_bytes_before": size_before,
            "database_bytes_after": size_after,
            "cache_state": (
                "Warm: sequential seed enqueues and 10 read warmups; OS/PG "
                "caches not flushed. Cold performance not measured."
            ),
            "telemetry": (
                "JSON logging + in-process spans enabled; file/OTLP export "
                "disabled; no worker"
            ),
            "transport": "real HTTP loopback, one API process and local PostgreSQL",
            "reads": reads,
            "enqueue": enqueue,
            "total_seconds": time.perf_counter() - started,
            "errors": reads["error_count"] + enqueue["error_count"],
            "limitations": (
                "Single local warm trial, 100 samples per phase by default;"
                " excludes model execution and network/WAN latency; not a "
                "throughput or production SLA."
            ),
        }
    finally:
        if server is not None and server.returncode is None:
            server.terminate()
            try:
                await asyncio.wait_for(server.wait(), 5)
            except TimeoutError:
                server.kill()
                await server.wait()
        if engine is not None:
            await engine.dispose()
        async with await psycopg.AsyncConnection.connect(
            admin_dsn, autocommit=True
        ) as admin:
            await admin.execute(
                sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name))
            )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=100)
    parser.add_argument("--concurrency", type=int, default=10)
    args = parser.parse_args()
    if not 1 <= args.concurrency <= args.runs <= 1000:
        parser.error("Require 1 <= concurrency <= runs <= 1000")
    try:
        report = asyncio.run(benchmark(args))
    except Exception:
        parser.error(
            "Benchmark failed; check local PostgreSQL admin access and "
            "migrations (credentials suppressed)"
        )
    print(json.dumps(report, indent=2))
    # Missed latency targets remain reported results, never hidden by dropping samples.
    return 1 if report["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
