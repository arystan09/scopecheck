"""Fenced budget ledger: known usage settles; uncertainty keeps the maximum."""

from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from scopecheck import telemetry
from scopecheck.providers.base import Turn
from scopecheck.providers.config import LiveConfig
from scopecheck.runs.models import CallReservation, CallSettlement, CaseAttempt, Run
from scopecheck.runs.service import digest
from scopecheck.settings import Settings

if TYPE_CHECKING:
    from scopecheck.runs.worker import Claim


class BudgetExceeded(Exception):
    pass


async def reserve_call(
    engine: AsyncEngine,
    claim: "Claim",
    case_id: str,
    repetition: int,
    sequence: int,
    config: LiveConfig,
) -> UUID:
    with telemetry.span(
        "budget.reserve", run_id=str(claim.run_id) if claim else "", mode="live"
    ) as current:
        try:
            return await _reserve_call(
                engine, claim, case_id, repetition, sequence, config
            )
        except BudgetExceeded:
            current.set_attribute("error", "budget_exhausted")
            raise


async def _reserve_call(
    engine: AsyncEngine,
    claim: "Claim",
    case_id: str,
    repetition: int,
    sequence: int,
    config: LiveConfig,
) -> UUID:
    from scopecheck.runs.worker import owned_job

    if Settings().public_demo:
        raise ValueError("Live reservations are disabled in public demo mode")
    config.price.require_current()
    amount = config.maximum_charge()
    async with AsyncSession(engine) as session, session.begin():
        job = await owned_job(session, claim, 30)
        now = await session.scalar(select(func.clock_timestamp()))
        assert isinstance(now, datetime)
        if job.deadline_at is not None and job.deadline_at <= now:
            from scopecheck.runs.worker import RunDeadlineExceeded

            raise RunDeadlineExceeded
        run = await session.scalar(
            select(Run).where(Run.id == claim.run_id).with_for_update()
        )
        assert run is not None
        if run.snapshot.get("config_hash") != digest(config.model_dump(mode="json")):
            raise ValueError("Reservation configuration mismatch")
        budget = run.snapshot.get("budget_microusd")
        if (
            type(budget) is not int
            or budget <= 0
            or run.snapshot.get("owner_authorized") is not True
        ):
            raise ValueError("Live budget and owner authorization required")
        spent = await session.scalar(
            select(
                func.coalesce(
                    func.sum(
                        func.coalesce(
                            CallSettlement.amount_microusd,
                            CallReservation.amount_microusd,
                        )
                    ),
                    0,
                )
            )
            .select_from(CallReservation)
            .outerjoin(
                CallSettlement, CallSettlement.reservation_id == CallReservation.id
            )
            .where(
                CallReservation.run_id == run.id,
                CallReservation.workspace_id == run.workspace_id,
            )
        )
        if int(spent or 0) + amount > budget:
            raise BudgetExceeded
        attempt = await session.scalar(
            select(CaseAttempt).where(
                CaseAttempt.workspace_id == claim.workspace_id,
                CaseAttempt.run_id == claim.run_id,
                CaseAttempt.case_id == case_id,
                CaseAttempt.repetition == repetition,
                CaseAttempt.lease_generation == claim.generation,
                CaseAttempt.status == "running",
            )
        )
        if attempt is None:
            raise ValueError("Current running attempt required")
        reservation_id = uuid4()
        session.add(
            CallReservation(
                id=reservation_id,
                workspace_id=claim.workspace_id,
                run_id=claim.run_id,
                case_id=case_id,
                repetition=repetition,
                sequence=sequence,
                attempt_number=attempt.attempt_number,
                lease_generation=claim.generation,
                amount_microusd=amount,
                price_hash=digest(config.price.model_dump(mode="json")),
            )
        )
    return reservation_id


async def settle_call(
    engine: AsyncEngine,
    claim: "Claim",
    reservation_id: UUID,
    config: LiveConfig,
    turn: Turn,
) -> None:
    from scopecheck.runs.worker import LeaseLost, owned_job

    async with AsyncSession(engine) as session, session.begin():
        await owned_job(session, claim, 30)
        # Same lock ordering as reservation: job -> run -> ledger.
        run = await session.scalar(
            select(Run).where(Run.id == claim.run_id).with_for_update()
        )
        assert run is not None
        reservation = await session.scalar(
            select(CallReservation).where(
                CallReservation.id == reservation_id,
                CallReservation.workspace_id == claim.workspace_id,
                CallReservation.run_id == claim.run_id,
                CallReservation.lease_generation == claim.generation,
            )
        )
        if reservation is None:
            raise LeaseLost
        if run.snapshot["config_hash"] != digest(config.model_dump(mode="json")):
            raise ValueError("Settlement configuration mismatch")
        existing = await session.scalar(
            select(CallSettlement).where(
                CallSettlement.reservation_id == reservation_id
            )
        )
        amount = None
        inputs, outputs = turn.input_tokens, turn.output_tokens
        if (
            type(inputs) is int
            and type(outputs) is int
            and 0 <= inputs <= config.generation.max_input_tokens
            and 0 <= outputs <= config.generation.max_output_tokens
        ):
            amount = (
                inputs * config.price.input_microusd_per_million
                + outputs * config.price.output_microusd_per_million
                + 999_999
            ) // 1_000_000
        else:
            inputs = outputs = None
        if existing is not None:
            if (
                existing.amount_microusd,
                existing.input_tokens,
                existing.output_tokens,
                existing.outcome,
                existing.returned_model,
            ) != (amount, inputs, outputs, turn.outcome, turn.model):
                raise ValueError("Conflicting settlement")
            return
        session.add(
            CallSettlement(
                workspace_id=claim.workspace_id,
                run_id=claim.run_id,
                reservation_id=reservation_id,
                amount_microusd=amount,
                input_tokens=inputs,
                output_tokens=outputs,
                outcome=turn.outcome,
                returned_model=turn.model,
            )
        )
