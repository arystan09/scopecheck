"""Keyless dataset validation and deterministic synthetic replay."""

import argparse
from pathlib import Path

from pydantic import ValidationError

from scopecheck.datasets.canonicalize import canonical_json, content_hash, load_dataset
from scopecheck.evaluation.scorer import replay


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="scopecheck",
        description="ScopeCheck synthetic offline evaluation (no live model).",
    )
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
    args = parser.parse_args()
    if args.command is None:
        parser.print_help()
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
