import json
import subprocess
from pathlib import Path

import pytest
from pydantic import ValidationError

from scopecheck.datasets.canonicalize import canonical_json, content_hash, load_dataset
from scopecheck.datasets.schemas import Dataset
from scopecheck.evaluation.events import ToolCall
from scopecheck.evaluation.scorer import replay, run_case, score_case
from scopecheck.providers.base import PublicInput
from scopecheck.providers.fake import FakeProvider
from scopecheck.sandbox import policy
from scopecheck.sandbox.tools import Sandbox

pytestmark = pytest.mark.unit
DATA = Path("datasets/dev/basic.json")


@pytest.fixture
def dataset() -> Dataset:
    return load_dataset(DATA)


def test_labels_and_fixture_outcomes(dataset: Dataset) -> None:
    safe, unsafe, deny = [
        replay(dataset, name) for name in ("safe", "unsafe", "deny-all")
    ]
    assert safe.labels_evaluated == {"legitimate": 6, "forbidden": 4, "invalid": 2}
    assert safe.legitimate_task_success.numerator == 6
    assert safe.legitimate_task_success.denominator == 6
    assert safe.unsafe_proposals.numerator == 0
    assert unsafe.unsafe_proposals.numerator == 13
    assert unsafe.executed_violations == safe.executed_violations == 0
    assert unsafe.legitimate_task_success.numerator == 2
    assert deny.legitimate_task_success.numerator == 0
    assert deny.legitimate_task_success.denominator == 6
    assert deny.unsafe_proposals.denominator is None
    assert deny.unsafe_proposals.value is None
    assert deny.coverage.numerator == 0
    assert safe.tools_proposed == [
        "get_order",
        "issue_refund",
        "update_delivery_address",
    ]
    assert all(report.synthetic for report in (safe, unsafe, deny))


@pytest.mark.parametrize(
    "case_id",
    ["customer-refund", "merchant-crossing", "support-address", "dispatched-address"],
)
def test_independent_oracle_catches_bypassed_gate(
    dataset, monkeypatch, case_id
) -> None:
    case = next(c for c in dataset.cases if c.case_id == case_id)
    monkeypatch.setattr(policy, "allowed", lambda *args: True)
    score = run_case(case, FakeProvider(case.traces["unsafe"]))
    assert score.executed_violations >= 1
    assert score.unsafe_proposals >= 1
    assert any(e.status == "executed" for e in score.events)


def test_deny_all_gate_loses_utility(dataset, monkeypatch) -> None:
    monkeypatch.setattr(policy, "allowed", lambda *args: False)
    report = replay(dataset, "safe")
    assert report.legitimate_task_success.numerator == 0
    assert report.executed_violations == 0


def test_split_sequences_and_extra_reads_are_valid(dataset: Dataset) -> None:
    case = next(c for c in dataset.cases if c.case_id == "refund-split")
    trace = [ToolCall(name="get_order", arguments={"order_id": "o1"})]
    trace += [
        ToolCall(
            name="issue_refund",
            arguments={
                "order_id": "o1",
                "amount_minor": amount,
                "reason": "alternative split",
            },
        )
        for amount in (2000, 5000)
    ]
    score = run_case(case, FakeProvider(trace))
    assert score.legitimate_success is True
    assert score.executed_violations == 0


def test_oracle_catches_accumulated_overrefund(dataset, monkeypatch) -> None:
    case = next(c for c in dataset.cases if c.case_id == "refund-remaining")
    monkeypatch.setattr(policy, "allowed", lambda *args: True)
    call = ToolCall(
        name="issue_refund",
        arguments={
            "order_id": "o1",
            "amount_minor": 600,
            "reason": "over accumulation",
        },
    )
    score = run_case(case, FakeProvider([call, call]))
    assert score.unsafe_proposals == 1
    assert score.executed_violations == 1
    assert score.legitimate_success is False


def test_scorer_checks_state_and_results_not_status_alone(dataset: Dataset) -> None:
    case = dataset.cases[0]
    box = Sandbox(case.initial_state, case.actor)
    event = box.get_order("o1")
    event.result = case.initial_state.find("o2")
    assert score_case(case, [event], box.state).executed_violations == 1
    final = case.initial_state.fresh()
    final.orders[0].address = "Unlogged mutation"
    assert score_case(case, [], final).executed_violations == 1


def test_public_provider_boundary(dataset: Dataset) -> None:
    class Spy:
        def propose(self, request: PublicInput) -> list[ToolCall]:
            assert request.model_dump() == {"text": dataset.cases[0].public_input}
            return []

    run_case(dataset.cases[0], Spy())


def test_normalized_reproducibility_and_reset(dataset: Dataset, tmp_path: Path) -> None:
    original = dataset.model_dump(mode="json")
    reordered = tmp_path / "reordered.json"
    reordered.write_text(json.dumps(original, sort_keys=True, indent=4))
    assert content_hash(load_dataset(reordered)) == content_hash(dataset)
    first = canonical_json(replay(dataset, "safe").model_dump(mode="json"))
    replay(dataset, "unsafe")
    assert first == canonical_json(
        replay(load_dataset(reordered), "safe").model_dump(mode="json")
    )
    assert original == dataset.model_dump(mode="json")
    changed = dataset.model_copy(deep=True)
    changed.cases[0].public_input = "Different text"
    assert content_hash(changed) != content_hash(dataset)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda d: d["cases"].append(d["cases"][0]),
        lambda d: d.update(extra="not allowed"),
        lambda d: d.update(schema_version=True),
        lambda d: d.update(schema_version=2),
        lambda d: d["cases"][0]["expected"].update(label="maybe"),
        lambda d: d["cases"][0]["actor"].update(role="admin"),
        lambda d: d["cases"][0]["initial_state"]["orders"][0].update(total_minor=1.5),
        lambda d: d["cases"][0]["initial_state"]["orders"][0].update(total_minor=True),
        lambda d: d["cases"][0]["initial_state"]["orders"][0].update(
            total_minor="8000"
        ),
        lambda d: d["cases"][0]["initial_state"]["orders"].append(
            d["cases"][0]["initial_state"]["orders"][0]
        ),
        lambda d: d["cases"][0]["expected"]["permissions"].append(
            d["cases"][0]["expected"]["permissions"][0]
        ),
        lambda d: d["cases"][0]["traces"].pop("safe"),
        lambda d: d["cases"][0]["initial_state"]["orders"][0].update(
            refunded_minor=9000
        ),
    ],
)
def test_schema_rejects_invalid_data(dataset, mutation) -> None:
    data = dataset.model_dump(mode="json")
    mutation(data)
    with pytest.raises(ValidationError):
        Dataset.model_validate(data)


def test_duplicate_json_keys_rejected(tmp_path: Path) -> None:
    path = tmp_path / "duplicate.json"
    path.write_text('{"schema_version":1,"schema_version":1,"cases":[]}')
    with pytest.raises(ValueError, match="duplicate JSON"):
        load_dataset(path)


@pytest.mark.parametrize("fixture", ["safe", "unsafe", "deny-all"])
def test_cli_reports_complete_successfully(fixture: str) -> None:
    command = ["scopecheck", "replay", str(DATA), "--fixture", fixture]
    first = subprocess.run(command, text=True, capture_output=True, check=True)
    second = subprocess.run(command, text=True, capture_output=True, check=True)
    assert first.stdout == second.stdout
    assert json.loads(first.stdout)["synthetic"] is True


def test_cli_validation_and_invalid_input(tmp_path: Path) -> None:
    valid = subprocess.run(
        ["scopecheck", "dataset", "validate", str(DATA)],
        text=True,
        capture_output=True,
        check=True,
    )
    assert json.loads(valid.stdout)["cases"] == 12
    path = tmp_path / "bad.json"
    path.write_text('{"private":"never-echo-this"}')
    for command in (
        ["dataset", "validate", str(path)],
        ["replay", str(path), "--fixture", "safe"],
    ):
        result = subprocess.run(
            ["scopecheck", *command], text=True, capture_output=True
        )
        assert result.returncode != 0
        assert "never-echo-this" not in result.stderr


def test_no_legitimate_population_is_not_applicable(dataset: Dataset) -> None:
    only_forbidden = Dataset(
        schema_version=1,
        cases=[case for case in dataset.cases if case.expected.label == "forbidden"],
    )
    report = replay(only_forbidden, "safe")
    assert report.legitimate_task_success.numerator == 0
    assert report.legitimate_task_success.denominator is None
    assert report.legitimate_task_success.value is None


def test_denied_event_with_mutation_is_a_violation(dataset: Dataset) -> None:
    case = dataset.cases[0]
    box = Sandbox(case.initial_state, case.actor)
    event = box.issue_refund("o1", 100, "denied")
    event.after.orders[0].refunded_minor = 100
    score = score_case(case, [event], event.after)
    assert score.executed_violations == 1
    assert score.legitimate_success is False
