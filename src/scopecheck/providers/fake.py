"""Script playback only, not a model or a claim of model performance."""

from typing import Any

from scopecheck.evaluation.events import ToolCall
from scopecheck.providers.base import ProviderCall, PublicInput, Turn


class FakeProvider:
    def __init__(self, trace: list[ToolCall]) -> None:
        self._trace = [call.model_copy(deep=True) for call in trace]

    def propose(self, request: PublicInput) -> list[ToolCall]:
        return [call.model_copy(deep=True) for call in self._trace]

    async def respond(
        self, request: PublicInput, history: list[dict[str, Any]], timeout: float
    ) -> Turn:
        if history:
            return Turn("final_response")
        return Turn(
            "tools" if self._trace else "final_response",
            [
                ProviderCall(f"fake-{i}", call)
                for i, call in enumerate(self.propose(request))
            ],
        )
