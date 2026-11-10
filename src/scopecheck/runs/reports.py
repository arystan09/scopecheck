"""Observed counts and incomplete coverage; never infer missing outcomes."""

from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from scopecheck.api.errors import ApiError
from scopecheck.evaluation.scorer import CaseScore, metric
from scopecheck.runs.models import (
    CallReservation,
    CaseAttempt,
    CaseResult,
    DatasetVersion,
    Run,
)
from scopecheck.version import PACKAGE_VERSION

TERMINAL = {"completed", "failed"}
COMPATIBILITY_FIELDS = (
    "dataset_hash",
    "dataset_schema_version",
    "evaluator_version",
    "policy_version",
    "tool_schema_version",
    "tool_schema_hash",
    "repetitions",
    "mode",
    "limits",
)


async def owned_run(session: AsyncSession, workspace_id: UUID, run_id: UUID) -> Run:
    run = await session.scalar(
        select(Run).where(Run.id == run_id, Run.workspace_id == workspace_id)
    )
    if run is None:
        raise ApiError(404, "not_found", "Resource not found")
    return run


async def report(session: AsyncSession, run: Run) -> dict[str, Any]:
    results = list(
        (
            await session.scalars(
                select(CaseResult)
                .where(
                    CaseResult.workspace_id == run.workspace_id,
                    CaseResult.run_id == run.id,
                )
                .order_by(CaseResult.repetition, CaseResult.case_id)
            )
        ).all()
    )
    dataset = await session.get(DatasetVersion, run.dataset_id)
    assert dataset is not None
    expected = len(dataset.content["cases"]) * run.snapshot["repetitions"]
    expected_legitimate = (
        sum(c["expected"]["label"] == "legitimate" for c in dataset.content["cases"])
        * run.snapshot["repetitions"]
    )
    observed = [
        CaseScore.model_validate(r.score) for r in results if r.score is not None
    ]
    completed = [
        CaseScore.model_validate(r.score)
        for r in results
        if r.status == "completed" and r.score is not None
    ]
    legitimate = [score for score in completed if score.legitimate_success is not None]
    events = [event for score in observed for event in score.events]
    versions = list(
        (
            await session.scalars(
                select(CaseAttempt.worker_version)
                .where(
                    CaseAttempt.workspace_id == run.workspace_id,
                    CaseAttempt.run_id == run.id,
                )
                .distinct()
                .order_by(CaseAttempt.worker_version)
            )
        ).all()
    )
    reserved = await session.scalar(
        select(func.coalesce(func.sum(CallReservation.amount_microusd), 0)).where(
            CallReservation.run_id == run.id,
            CallReservation.workspace_id == run.workspace_id,
        )
    )
    return {
        "run_id": run.id,
        "status": run.status,
        "synthetic": True,
        "mode": run.snapshot["mode"],
        "description": (
            "Live model in a synthetic sandbox"
            if run.snapshot["mode"] == "live"
            else "Scripted synthetic outcomes; not live-model measurements"
        ),
        "budget_microusd": run.snapshot.get("budget_microusd"),
        "reserved_microusd": int(reserved or 0),
        "provider_outcomes": {
            outcome: sum(
                r.provenance is not None and r.provenance.get("outcome") == outcome
                for r in results
            )
            for outcome in sorted(
                {r.provenance["outcome"] for r in results if r.provenance}
            )
        },
        "snapshot": run.snapshot,
        "api_package_version": PACKAGE_VERSION,
        "worker_versions": versions,
        "expected_cases": expected,
        "persisted_cases": len(results),
        "completed_cases": len(completed),
        "missing_cases": expected - len(results),
        "limited_cases": sum(r.status == "limited" for r in results),
        "failed_cases": sum(r.status == "failed" for r in results),
        "complete": len(completed) == expected and run.status == "completed",
        "coverage": metric(len(completed), expected).model_dump(),
        "expected_legitimate_cases": expected_legitimate,
        "legitimate_task_success": metric(
            sum(s.legitimate_success is True for s in legitimate), len(legitimate)
        ).model_dump(),
        "unsafe_proposals": metric(
            sum(s.unsafe_proposals for s in observed), len(events)
        ).model_dump(),
        "executed_violations": sum(s.executed_violations for s in observed),
        "executed_actions": sum(e.status == "executed" for e in events),
        "proposal_coverage": metric(
            sum(bool(s.events) for s in completed), expected
        ).model_dump(),
    }


def incompatible_reasons(baseline: Run, candidate: Run) -> list[str]:
    reasons = []
    if baseline.status not in TERMINAL or candidate.status not in TERMINAL:
        reasons.append("runs_not_terminal")
    for field in COMPATIBILITY_FIELDS:
        if baseline.snapshot.get(field) != candidate.snapshot.get(field):
            reasons.append(field + "_mismatch")
    return reasons


async def compare(
    session: AsyncSession, workspace_id: UUID, baseline: UUID, candidate: UUID
) -> dict[str, Any]:
    first = await owned_run(session, workspace_id, baseline)
    second = await owned_run(session, workspace_id, candidate)
    reasons = incompatible_reasons(first, second)
    if reasons:
        raise ApiError(409, "incompatible_runs", "; ".join(reasons))
    a, b = await report(session, first), await report(session, second)
    return {
        "synthetic": True,
        "baseline": a,
        "candidate": b,
        "complete": a["complete"] and b["complete"],
        "warning": None
        if a["complete"] and b["complete"]
        else "Incomplete coverage; observed counts only",
    }
