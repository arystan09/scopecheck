"""Local-only HTTP smoke test. Requires a running API, worker, and migrated database."""

import argparse
import asyncio
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession

from scopecheck.auth import bootstrap, hash_token, revoke
from scopecheck.db import create_engine
from scopecheck.runs.models import ApiToken
from scopecheck.settings import Settings
from scopecheck.version import PACKAGE_VERSION


async def credential(workspace: str) -> tuple[str, str]:
    settings = Settings()
    if settings.database_url is None:
        raise ValueError(
            "Set a local SCOPECHECK_DATABASE_URL for smoke credential creation"
        )
    if make_url(settings.database_url.get_secret_value()).host not in {
        "localhost",
        "127.0.0.1",
        "::1",
        "db",
    }:
        raise ValueError("Smoke database must be local/disposable")
    engine = create_engine(settings)
    try:
        async with AsyncSession(engine) as session, session.begin():
            token = await bootstrap(session, workspace)
            if token is None:
                raise ValueError("Smoke workspace must be fresh")
            token_id = await session.scalar(
                select(ApiToken.id).where(ApiToken.token_hash == hash_token(token))
            )
            return token, str(token_id)
    finally:
        await engine.dispose()


async def cleanup(workspace: str, token_id: str) -> None:
    from uuid import UUID

    engine = create_engine(Settings())
    try:
        async with AsyncSession(engine) as session, session.begin():
            await revoke(session, workspace, UUID(token_id))
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--timeout", type=int, default=60)
    args = parser.parse_args()
    parsed = urllib.parse.urlsplit(args.base_url)
    if (
        parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
        or parsed.scheme not in {"http", "https"}
        or parsed.username
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or not 1 <= args.timeout <= 300
    ):
        parser.error("Use a loopback HTTP URL and timeout of 1–300 seconds")
    base = args.base_url.rstrip("/")
    workspace = "demo-smoke-" + uuid4().hex
    token = os.environ.get("SCOPECHECK_DEMO_TOKEN")
    token_id = None
    if token is None:
        token, token_id = asyncio.run(credential(workspace))
    deadline = time.monotonic() + args.timeout

    def request(path, body=None, *, key=None, expected_status=None):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Smoke deadline exceeded")
        headers = {"Authorization": "Bearer " + token}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if key:
            headers["Idempotency-Key"] = key
        req = urllib.request.Request(
            base + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers=headers,
        )
        try:
            response = urllib.request.urlopen(req, timeout=min(5, remaining))
        except urllib.error.HTTPError as error:
            if error.code == expected_status:
                with error:
                    return json.load(error)
            raise RuntimeError(f"Smoke request failed with HTTP {error.code}") from None
        with response:
            if expected_status is not None:
                raise AssertionError("Expected incompatible request to fail")
            return json.load(response)

    try:
        assert request("/health/version")["package_version"] == PACKAGE_VERSION
        raw = json.loads(Path("datasets/dev/basic.json").read_text())
        dataset = request("/api/v1/datasets", raw)
        ids = {}
        for name, fixture, repetitions in (
            ("safe", "safe", 1),
            ("unsafe", "unsafe", 1),
            ("deny-all", "deny-all", 1),
            ("repeat", "safe", 1),
            ("incompatible", "safe", 2),
        ):
            config = request("/api/v1/configs", {"mode": "fake", "fixture": fixture})
            run = request(
                "/api/v1/runs",
                {
                    "dataset_id": dataset["id"],
                    "config_id": config["id"],
                    "repetitions": repetitions,
                },
                key=uuid4().hex,
            )
            ids[name] = run["id"]
        reports = {}
        for name, run_id in ids.items():
            while True:
                result = request(f"/api/v1/runs/{run_id}/report")
                if result["status"] in {"completed", "failed"}:
                    reports[name] = result
                    break
                time.sleep(min(0.1, max(0, deadline - time.monotonic())))
            assert result["complete"] and result["status"] == "completed"
            assert result["worker_versions"] == [PACKAGE_VERSION]
            assert result["synthetic"] and result["executed_violations"] == 0
        for name, successes in (("safe", 6), ("unsafe", 2), ("deny-all", 0)):
            result = reports[name]
            assert result["expected_cases"] == result["completed_cases"] == 12
            assert result["legitimate_task_success"]["numerator"] == successes
            assert result["legitimate_task_success"]["denominator"] == 6
        assert reports["unsafe"]["unsafe_proposals"]["numerator"] == 13
        assert reports["deny-all"]["unsafe_proposals"]["denominator"] is None

        def scores(run_id):
            return sorted(
                [
                    (i["case_id"], i["score"])
                    for i in request(f"/api/v1/runs/{run_id}/cases?limit=100")["items"]
                ]
            )

        assert scores(ids["safe"]) == scores(ids["repeat"])
        compatible = request(
            f"/api/v1/comparisons?baseline={ids['safe']}&candidate={ids['unsafe']}"
        )
        assert compatible["complete"] and compatible["synthetic"]
        invalid = request(
            f"/api/v1/comparisons?baseline={ids['safe']}&candidate={ids['incompatible']}",
            expected_status=409,
        )
        assert "repetitions_mismatch" in invalid["message"]
        print(
            json.dumps(
                {
                    "synthetic": True,
                    "status": "passed",
                    "package_version": PACKAGE_VERSION,
                    "runs": ids,
                    "legitimate_successes": {"safe": 6, "unsafe": 2, "deny-all": 0},
                }
            )
        )
    finally:
        if token_id is not None:
            asyncio.run(cleanup(workspace, token_id))


if __name__ == "__main__":
    main()
