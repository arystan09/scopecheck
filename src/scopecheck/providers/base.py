"""Public-only boundaries for scripted replay and sequential model turns."""

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from scopecheck.evaluation.events import ToolCall
from scopecheck.sandbox.state import StrictModel, Text


class PublicInput(StrictModel):
    text: Text


class ScriptedProvider(Protocol):
    def propose(self, request: PublicInput) -> list[ToolCall]: ...


Outcome = Literal[
    "tools",
    "final_response",
    "refusal",
    "timeout",
    "rate_limit",
    "server_error",
    "invalid_credentials",
    "connection_error",
    "provider_error",
    "malformed_output",
    "output_token_limit",
    "input_token_limit",
]


@dataclass(frozen=True)
class ProviderCall:
    call_id: str
    call: ToolCall


@dataclass(frozen=True)
class Turn:
    outcome: Outcome
    calls: list[ProviderCall] = field(default_factory=list)
    continuation: list[dict[str, Any]] = field(default_factory=list)
    input_tokens: int | None = None
    output_tokens: int | None = None
    model: str | None = None


class Provider(Protocol):
    async def respond(
        self, request: PublicInput, history: list[dict[str, Any]], timeout: float
    ) -> Turn: ...
