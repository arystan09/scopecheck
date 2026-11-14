"""Real encrypted pg_dump/pg_restore round trip into a fresh database."""

import json
import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from sqlalchemy.engine import make_url

from scopecheck.runs import worker

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_encrypted_restore_counts_hashes_and_existing_rejection(
    database, api, tmp_path
):
    dataset = await api.client.post(
        "/api/v1/datasets",
        content=Path("datasets/dev/basic.json").read_bytes(),
        headers=api.headers(),
    )
    config = await api.client.post(
        "/api/v1/configs", json={"fixture": "safe"}, headers=api.headers()
    )
    assert dataset.status_code == config.status_code == 201
    run = await api.client.post(
        "/api/v1/runs",
        json={"dataset_id": dataset.json()["id"], "config_id": config.json()["id"]},
        headers=api.headers(**{"Idempotency-Key": uuid4().hex}),
    )
    assert run.status_code == 201
    assert await worker.run_one(database.engine)
    parsed = make_url(database.url)
    env = {
        **os.environ,
        "PGHOST": parsed.host,
        "PGPORT": str(parsed.port or 5432),
        "PGUSER": parsed.username,
        "PGDATABASE": parsed.database,
        "PGPASSWORD": parsed.password or "",
    }
    identity = tmp_path / "key.txt"
    subprocess.run(["age-keygen", "-o", str(identity)], check=True, capture_output=True)
    recipient = subprocess.check_output(
        ["age-keygen", "-y", str(identity)], text=True
    ).strip()
    archive = tmp_path / "backup.age"
    subprocess.run(
        [
            sys.executable,
            "scripts/backup.py",
            "--output",
            str(archive),
            "--recipient",
            recipient,
        ],
        env=env,
        check=True,
        capture_output=True,
    )
    assert archive.read_bytes().startswith(b"age-encryption.org/")
    name = "scopecheck_restore_" + uuid4().hex
    env["PGDATABASE"] = "postgres"
    command = [
        sys.executable,
        "scripts/restore_check.py",
        "--archive",
        str(archive),
        "--identity",
        str(identity),
        "--database",
        name,
        "--confirm-disposable",
        name,
    ]
    admin_url = parsed.set(
        drivername="postgresql", database="postgres"
    ).render_as_string(hide_password=False)
    try:
        wrong_identity = tmp_path / "wrong-key.txt"
        subprocess.run(
            ["age-keygen", "-o", str(wrong_identity)], check=True, capture_output=True
        )
        wrong_command = command.copy()
        wrong_command[wrong_command.index("--identity") + 1] = str(wrong_identity)
        wrong = subprocess.run(wrong_command, env=env, capture_output=True)
        assert wrong.returncode != 0
        with psycopg.connect(admin_url) as admin:
            assert not admin.execute(
                "SELECT 1 FROM pg_database WHERE datname = %s", (name,)
            ).fetchone()
        result = subprocess.run(
            command, env=env, check=True, capture_output=True, text=True
        )
        report = json.loads(result.stdout)
        assert report["verified"]
        assert report["tables"]["workspaces"]["count"] == 2
        assert report["tables"]["api_tokens"]["count"] == 3
        assert report["tables"]["dataset_versions"]["count"] == 1
        assert report["tables"]["runs"]["count"] == 1
        assert report["tables"]["case_results"]["count"] == 12
        assert report["recovery_seconds"] > 0
        print(json.dumps(report, sort_keys=True))
        duplicate = subprocess.run(command, env=env, capture_output=True)
        assert duplicate.returncode != 0
        with psycopg.connect(admin_url.replace("/postgres", "/" + name)) as connection:
            assert (
                connection.execute("SELECT count(*) FROM workspaces").fetchone()[0] == 2
            )
    finally:
        with psycopg.connect(admin_url, autocommit=True) as admin:
            admin.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                    sql.Identifier(name)
                )
            )
