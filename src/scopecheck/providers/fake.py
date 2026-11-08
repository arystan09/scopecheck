"""Script playback only, not a model or a claim of model performance."""

from scopecheck.evaluation.events import ToolCall
from scopecheck.providers.base import PublicInput


class FakeProvider:
    def __init__(self, trace: list[ToolCall]) -> None:
        self._trace = [call.model_copy(deep=True) for call in trace]

    def propose(self, request: PublicInput) -> list[ToolCall]:
        return [call.model_copy(deep=True) for call in self._trace]
