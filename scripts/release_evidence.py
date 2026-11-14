"""Reproduce development-only scripted evidence; never calls a live provider."""

import hashlib
import json
from pathlib import Path

from scopecheck.datasets.canonicalize import load_dataset
from scopecheck.evaluation.scorer import oracle_transition, replay
from scopecheck.providers.openai import TOOL_SCHEMA_HASH


def main() -> None:
    dataset = load_dataset(Path("datasets/dev/suite.json"))
    root = Path("artifacts/fake-dev")
    root.mkdir(parents=True, exist_ok=True)
    summaries = {}
    for fixture in ("safe", "unsafe", "deny-all"):
        report = replay(dataset, fixture)
        raw = report.model_dump(mode="json")
        (root / f"{fixture}.json").write_text(
            json.dumps(raw, indent=2, sort_keys=True) + "\n"
        )
        valid = unsafe = invalid = unsafe_cases = 0
        for case, score in zip(dataset.cases, report.cases, strict=True):
            case_unsafe = False
            for event in score.events:
                if event.status == "invalid":
                    invalid += 1
                    continue
                valid += 1
                forbidden = oracle_transition(case, event.before, event.call) is None
                unsafe += forbidden
                case_unsafe |= forbidden
            unsafe_cases += case_unsafe
        summaries[fixture] = {
            "unsafe_valid_proposals": unsafe,
            "valid_proposals": valid,
            "unsafe_proposal_rate": unsafe / valid if valid else None,
            "invalid_outputs": invalid,
            "unsafe_cases": unsafe_cases,
            "evaluable_cases": len(report.cases),
            "scheduled_cases": len(dataset.cases),
            "unsafe_case_rate": unsafe_cases / len(report.cases),
            "legitimate_success": raw["legitimate_task_success"],
            "executed_violations": report.executed_violations,
            "provider_requests": 0,
            "errors": 0,
        }
    # File hashes attribute this dirty working tree without claiming a commit
    # contains uncommitted changes. Excludes generated evidence and local secrets.
    sources = [
        *Path("src/scopecheck").rglob("*.py"),
        Path("scripts/release_evidence.py"),
    ]
    sources += [Path("uv.lock"), Path("datasets/manifest-v1.json")]
    source_hashes = {
        str(path): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(sources)
    }
    evidence = {
        "mode": "synthetic-scripted",
        "split": "development-only",
        "dataset_hash": report.dataset_hash,
        "tool_schema_hash": TOOL_SCHEMA_HASH,
        "source_sha256": source_hashes,
        "results": summaries,
    }
    (root / "summary.json").write_text(
        json.dumps(evidence, indent=2, sort_keys=True) + "\n"
    )
    print(json.dumps(summaries, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
