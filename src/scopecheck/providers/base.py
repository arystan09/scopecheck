"""Narrow public-only provider boundary; no policy or expected outcomes."""

from typing import Protocol

from scopecheck.evaluation.events import ToolCall
from scopecheck.sandbox.state import StrictModel, Text


class PublicInput(StrictModel):
    text: Text


class Provider(Protocol):
    def propose(self, request: PublicInput) -> list[ToolCall]: ...
