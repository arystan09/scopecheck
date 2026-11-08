"""Dataset v1: private hand-labelled oracles are distinct from public input."""

from typing import Literal

from pydantic import Field, model_validator

from scopecheck.evaluation.events import ToolCall
from scopecheck.sandbox.state import Actor, Money, State, StrictModel, Text

Fixture = Literal["safe", "unsafe", "deny-all"]


class Permission(StrictModel):
    order_id: Text
    read: bool = False
    refund_budget_minor: Money = 0
    refund_action_limit_minor: Money = 5000
    update_address: bool = False


class Goals(StrictModel):
    read_order_ids: list[Text] = Field(default_factory=list)
    refund_delta_minor: dict[str, Money] = Field(default_factory=dict)
    addresses: dict[str, Text] = Field(default_factory=dict)


class Expected(StrictModel):
    label: Literal["legitimate", "forbidden", "invalid"]
    explanation: Text
    permissions: list[Permission]
    goals: Goals | None = None

    @model_validator(mode="after")
    def consistent_label(self) -> "Expected":
        ids = [p.order_id for p in self.permissions]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate permission ids")
        if (self.label == "legitimate") != (self.goals is not None):
            raise ValueError("only legitimate cases must have goals")
        if self.goals is not None and not (
            self.goals.read_order_ids
            or self.goals.refund_delta_minor
            or self.goals.addresses
        ):
            raise ValueError("legitimate goals must not be empty")
        return self


class Case(StrictModel):
    case_id: Text
    initial_state: State
    actor: Actor
    public_input: Text
    traces: dict[Fixture, list[ToolCall]]
    expected: Expected

    @model_validator(mode="after")
    def references(self) -> "Case":
        if set(self.traces) != {"safe", "unsafe", "deny-all"}:
            raise ValueError("all three scripted variants are required")
        ids = {o.order_id for o in self.initial_state.orders}
        if any(p.order_id not in ids for p in self.expected.permissions):
            raise ValueError("permission references unknown order")
        goals = self.expected.goals
        if (
            goals
            and not (
                set(goals.read_order_ids)
                | set(goals.refund_delta_minor)
                | set(goals.addresses)
            )
            <= ids
        ):
            raise ValueError("goal references unknown order")
        return self


class Dataset(StrictModel):
    schema_version: Literal[1]
    cases: list[Case] = Field(min_length=1)

    @model_validator(mode="before")
    @classmethod
    def strict_version(cls, value: object) -> object:
        if isinstance(value, dict) and type(value.get("schema_version")) is not int:
            raise ValueError("schema_version must be integer 1")
        return value

    @model_validator(mode="after")
    def unique_cases(self) -> "Dataset":
        ids = [case.case_id for case in self.cases]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate case ids")
        return self
