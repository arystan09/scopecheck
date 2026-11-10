"""Keyless dataset validation and deterministic synthetic replay."""

import argparse
import asyncio
from pathlib import Path
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from scopecheck.api.errors import ApiError
from scopecheck.datasets.canonicalize import canonical_json, content_hash, load_dataset
from scopecheck.db_cli import administer
from scopecheck.demo import seed
from scopecheck.evaluation.scorer import replay
from scopecheck.runs.worker import serve
from scopecheck.version import PACKAGE_VERSION


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="scopecheck",
        description="ScopeCheck synthetic offline evaluation (no live model).",
    )
    parser.add_argument("--version", action="version", version=PACKAGE_VERSION)
    commands = parser.add_subparsers(dest="command")
    dataset = commands.add_parser("dataset", help="Dataset operations")
    dataset_commands = dataset.add_subparsers(dest="dataset_command", required=True)
    validate = dataset_commands.add_parser("validate", help="Validate schema v1")
    validate.add_argument("path", type=Path)
    replay_parser = commands.add_parser(
        "replay", help="Replay synthetic scripted traces"
    )
    replay_parser.add_argument("path", type=Path)
    replay_parser.add_argument(
        "--fixture", choices=["safe", "unsafe", "deny-all"], required=True
    )
    db = commands.add_parser("db", help="Local database credential administration")
    db_commands = db.add_subparsers(dest="db_command", required=True)
    for name in ("bootstrap", "token-create", "revoke"):
        command = db_commands.add_parser(name)
        command.add_argument("--workspace", required=True)
        if name == "token-create":
            command.add_argument("--role", choices=["reader", "editor"], required=True)
        if name == "revoke":
            command.add_argument("--token-id", type=UUID, required=True)
    worker = commands.add_parser("worker", help="Run the fake-only worker process")
    worker.add_argument(
        "--once", action="store_true", help="Claim at most one job then exit"
    )
    demo = commands.add_parser("demo", help="Local synthetic demo data")
    demo_commands = demo.add_subparsers(dest="demo_command", required=True)
    demo_seed = demo_commands.add_parser("seed")
    demo_seed.add_argument("--workspace", default="local")
    demo_seed.add_argument(
        "--dataset", type=Path, default=Path("datasets/dev/basic.json")
    )
    args = parser.parse_args()
    if args.command is None:
        parser.print_help()
        return
    if args.command == "worker":
        try:
            asyncio.run(serve(once=args.once))
        except KeyboardInterrupt:
            return
        except (ValueError, SQLAlchemyError):
            parser.error("worker failed; check database settings and migrations")
        return
    if args.command == "demo":
        try:
            print(canonical_json(asyncio.run(seed(args.workspace, args.dataset))))
        except (OSError, ValueError, SQLAlchemyError, ApiError):
            parser.error("demo seeding failed; check local workspace and dataset")
        return
    if args.command == "db":
        try:
            result = asyncio.run(
                administer(
                    args.db_command,
                    args.workspace,
                    getattr(args, "role", "reader"),
                    getattr(args, "token_id", None),
                )
            )
        except (ValueError, SQLAlchemyError):
            parser.error(
                "database administration failed; check settings and migrations"
            )
        print(canonical_json(result))
        return
    try:
        data = load_dataset(args.path)
    except (OSError, ValueError, ValidationError):
        # Untrusted data/paths may contain secrets: never echo raw validation input.
        parser.error(
            "invalid dataset: unreadable file or schema/JSON validation failed"
        )
    if args.command == "dataset":
        print(
            canonical_json(
                {
                    "valid": True,
                    "schema_version": data.schema_version,
                    "cases": len(data.cases),
                    "content_hash": content_hash(data),
                }
            )
        )
    else:
        print(canonical_json(replay(data, args.fixture).model_dump(mode="json")))
