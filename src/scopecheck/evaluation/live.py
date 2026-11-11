"""Bounded sequential execution; provider never receives the case or trusted actor."""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from scopecheck.datasets.canonicalize import canonical_json
from scopecheck.datasets.schemas import Case
from scopecheck.evaluation.events import Event
from scopecheck.evaluation.scorer import CaseScore, score_case
from scopecheck.providers.base import Provider, PublicInput, Turn
from scopecheck.runs.budget import BudgetExceeded
from scopecheck.runs.retry import RetryPolicy
from scopecheck.sandbox.tools import Sandbox


@dataclass(frozen=True)
class LiveResult:
    score: CaseScore
    status: str
    outcome: str
    requests: int


async def run_live_case(
    case: Case,
    provider: Provider,
    reserve: Callable[[int], Awaitable[UUID]],
    record: Callable[[UUID, Turn], Awaitable[None]],
    *,
    retry: RetryPolicy | None = None,
    remaining_seconds: float = 30,
) -> LiveResult:
    sandbox = Sandbox(case.initial_state, case.actor)
    events: list[Event] = []
    history: list[dict[str, Any]] = []
    requests = 0
    outcome = "request_limit"
    ids: set[str] = set()
    retry = retry or RetryPolicy()
    duration = max(0, min(30, remaining_seconds))
    deadline = retry.now() + duration
    retries = 0
    try:
        async with asyncio.timeout_at(asyncio.get_running_loop().time() + duration):
            for sequence in range(1, 5):
                if retry.now() >= deadline:
                    outcome = "timeout"
                    break
                reservation = await reserve(sequence)
                if retry.now() >= deadline:
                    outcome = "timeout"
                    break
                requests += 1
                turn = await provider.respond(
                    PublicInput(text=case.public_input),
                    history,
                    max(0.001, deadline - retry.now()),
                )
                await record(reservation, turn)
                if retry.now() >= deadline:
                    outcome = "timeout"
                    break
                if turn.outcome != "tools":
                    delay = retry.delay(turn, retries, deadline - retry.now())
                    if delay is not None and sequence < 4:
                        await retry.sleep(delay)
                        retries += 1
                        continue
                    outcome = turn.outcome
                    break
                retries = 0
                history.extend(turn.continuation)
                for proposal in turn.calls:
                    if len(events) >= 6:
                        outcome = "tool_call_limit"
                        break
                    if proposal.call_id in ids:
                        outcome = "malformed_output"
                        break
                    ids.add(proposal.call_id)
                    event = sandbox.execute(proposal.call)
                    events.append(event)
                    if event.status == "invalid":
                        outcome = "invalid_tool_arguments"
                        break
                    # Never serialize Event: its before/after contain hidden state.
                    history.append(
                        {
                            "type": "function_call_output",
                            "call_id": proposal.call_id,
                            "output": canonical_json(
                                {
                                    "status": event.status,
                                    "result": event.result.model_dump(mode="json")
                                    if event.result
                                    else None,
                                }
                            ),
                        }
                    )
                if outcome != "request_limit":
                    break
                if len(events) >= 6:
                    outcome = "tool_call_limit"
                    break
    except BudgetExceeded:
        outcome = "budget_exhausted"
    except TimeoutError:
        outcome = "timeout"
    status = (
        "completed"
        if outcome in {"refusal", "final_response"}
        else (
            "limited"
            if outcome
            in {
                "request_limit",
                "tool_call_limit",
                "input_token_limit",
                "output_token_limit",
                "budget_exhausted",
                "timeout",
            }
            else "failed"
        )
    )
    return LiveResult(
        score_case(case, events, sandbox.state), status, outcome, requests
    )
