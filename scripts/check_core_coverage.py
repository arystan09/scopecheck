"""Report and enforce the 80% branch target per critical module, not total coverage."""

import json
import sys
from pathlib import Path

CORE = ("sandbox/policy.py", "evaluation/scorer.py", "runs/budget.py")


def main() -> int:
    data = json.loads(Path(sys.argv[1]).read_text())
    results = {}
    for suffix in CORE:
        matches = [
            value for key, value in data["files"].items() if key.endswith(suffix)
        ]
        if len(matches) != 1:
            raise ValueError("Core coverage is missing or ambiguous")
        summary = matches[0]["summary"]
        total, covered = summary["num_branches"], summary["covered_branches"]
        percentage = 100 * covered / total if total else 0
        results[suffix] = {
            "covered_branches": covered,
            "branches": total,
            "branch_percent": percentage,
            "target": 80,
            "target_met": percentage >= 80,
        }
    print(json.dumps(results, indent=2))
    return 0 if all(result["target_met"] for result in results.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
