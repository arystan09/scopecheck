"""Restore a trusted encrypted backup into a NEW local disposable database only.

PGSERVICE or PGHOST/PGPORT/PGUSER/PGPASSFILE must connect to the postgres admin DB.
The restored database is left for inspection; this tool never drops a database.
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import tarfile
import tempfile
import time
from pathlib import Path

import psycopg
from backup import command, inventory
from psycopg import sql


def validate_destination(name: str, confirmation: str) -> None:
    if (
        not re.fullmatch(r"scopecheck_restore_[a-f0-9]{32}", name)
        or confirmation != name
    ):
        raise ValueError("Explicit matching disposable database name required")


def restore(archive: Path, identity: Path, name: str, confirmation: str) -> dict:
    validate_destination(name, confirmation)
    started = time.monotonic()
    os.umask(0o077)
    with psycopg.connect("", autocommit=True) as admin:
        if (
            admin.info.dbname != "postgres"
            or admin.info.host not in ("127.0.0.1", "::1", "localhost")
            or admin.info.server_version // 10000 != 18
        ):
            raise ValueError("Use a loopback PostgreSQL 18 postgres admin connection")
        if admin.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (name,)
        ).fetchone():
            raise ValueError("Destination already exists; refusing restore")
        with tempfile.TemporaryDirectory(prefix="scopecheck-restore-") as directory:
            root = Path(directory)
            command(
                [
                    "age",
                    "--decrypt",
                    "--identity",
                    str(identity),
                    "--output",
                    str(root / "bundle.tar"),
                    str(archive),
                ]
            )
            with tarfile.open(root / "bundle.tar") as bundle:
                members = bundle.getmembers()
                if sorted(m.name for m in members) != [
                    "database.dump",
                    "manifest.json",
                ]:
                    raise ValueError("Unexpected archive entries")
                if any(not m.isfile() for m in members):
                    raise ValueError("Only ordinary backup files allowed")
                bundle.extractall(root, filter="data")
            manifest = json.loads((root / "manifest.json").read_text())
            with (root / "database.dump").open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            if (
                manifest["format"] != 1
                or manifest["postgres_major"] != 18
                or manifest["dump_sha256"] != digest
            ):
                raise ValueError("Backup manifest mismatch")
            # CREATE fails atomically if another process created this name.
            admin.execute(
                sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(
                    sql.Identifier(name)
                )
            )
            command(
                [
                    "pg_restore",
                    "--exit-on-error",
                    "--single-transaction",
                    "--no-owner",
                    "--no-acl",
                    "--dbname",
                    name,
                    str(root / "database.dump"),
                ]
            )
            params = admin.info.get_parameters()
            params["dbname"] = name
            with psycopg.connect(**params) as destination:
                destination.execute(
                    "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
                )
                observed = inventory(destination)
            if observed != manifest["tables"]:
                raise ValueError("Restored counts/hashes do not match")
    return {
        "verified": True,
        "database": name,
        "tables": observed,
        "recovery_seconds": round(time.monotonic() - started, 6),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--identity", type=Path, required=True)
    parser.add_argument("--database", required=True)
    parser.add_argument("--confirm-disposable", required=True)
    args = parser.parse_args()
    try:
        result = restore(
            args.archive, args.identity, args.database, args.confirm_disposable
        )
    except (
        ValueError,
        KeyError,
        OSError,
        psycopg.Error,
        subprocess.SubprocessError,
        tarfile.TarError,
    ):
        parser.exit(
            1, "Restore verification failed; destination may remain for inspection\n"
        )
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
