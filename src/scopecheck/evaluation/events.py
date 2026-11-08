"""Structured replay evidence, including denied and malformed proposals."""

from typing import Literal

from pydantic import JsonValue

from scopecheck.sandbox.state import Order, State, StrictModel, Text


class ToolCall(StrictModel):
    name: Text
    arguments: dict[str, JsonValue]


class Event(StrictModel):
    call: ToolCall
    status: Literal["executed", "denied", "invalid"]
    before: State
    after: State
    result: Order | None = None
