"""Create an age-encrypted logical backup using one PostgreSQL snapshot.

Connection credentials use libpq environment/service files, never CLI arguments.
Only restore archives produced by this tool from your trusted backup destination.
"""

import argparse
import hashlib
import json
import os
import subprocess
import tarfile
import tempfile
from pathlib import Path

import psycopg
from psycopg import sql


def command(arguments: list[str], **kwargs: object) -> None:
    # External tools can echo credentials or rows in errors; never relay stderr.
    subprocess.run(
        arguments,
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        **kwargs,
    )


def inventory(connection: psycopg.Connection) -> dict:
    connection.execute("SET TIME ZONE 'UTC'")
    tables = connection.execute(
        "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename"
    ).fetchall()
    result = {}
    for (table,) in tables:
        digest = hashlib.sha256()
        count = 0
        # Ordering JSON text covers tables without primary keys too. C collation
        # makes the digest independent of the destination's locale.
        with connection.cursor(name="backup_rows") as cursor:
            cursor.execute(
                sql.SQL(
                    "SELECT row_to_json(t)::text FROM public.{} t "
                    'ORDER BY row_to_json(t)::text COLLATE "C"'
                ).format(sql.Identifier(table))
            )
            for (row,) in cursor:
                digest.update(row.encode() + b"\n")
                count += 1
        result[table] = {"count": count, "sha256": digest.hexdigest()}
    return result


def backup(output: Path, recipient: str) -> None:
    if not recipient.startswith("age1") or output.exists():
        raise ValueError("Use an age public recipient and a new output path")
    os.umask(0o077)
    with tempfile.TemporaryDirectory(prefix="scopecheck-backup-") as directory:
        root = Path(directory)
        with psycopg.connect("") as connection:
            connection.execute(
                "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
            )
            if connection.info.server_version // 10000 != 18:
                raise ValueError("PostgreSQL 18 required")
            snapshot = connection.execute("SELECT pg_export_snapshot()").fetchone()[0]
            command(
                [
                    "pg_dump",
                    "--format=custom",
                    "--no-owner",
                    "--no-acl",
                    "--snapshot",
                    snapshot,
                    "--file",
                    str(root / "database.dump"),
                ]
            )
            manifest = {
                "format": 1,
                "postgres_major": 18,
                "tables": inventory(connection),
                "dump_sha256": hashlib.file_digest(
                    (root / "database.dump").open("rb"), "sha256"
                ).hexdigest(),
            }
        (root / "manifest.json").write_text(json.dumps(manifest, sort_keys=True))
        with tarfile.open(root / "bundle.tar", "w") as archive:
            for name in ("database.dump", "manifest.json"):
                archive.add(root / name, arcname=name)
        command(
            [
                "age",
                "--recipient",
                recipient,
                "--output",
                str(root / "backup.age"),
                str(root / "bundle.tar"),
            ]
        )
        # Exclusive create prevents accidentally replacing an earlier backup.
        with (
            output.open("xb") as destination,
            (root / "backup.age").open("rb") as source,
        ):
            import shutil

            shutil.copyfileobj(source, destination)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--recipient", required=True)
    args = parser.parse_args()
    try:
        backup(args.output, args.recipient)
    except (ValueError, OSError, psycopg.Error, subprocess.SubprocessError):
        parser.exit(1, "Backup failed; no raw database diagnostics emitted\n")
    print("Encrypted snapshot backup created")


if __name__ == "__main__":
    main()
