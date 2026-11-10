"""Short fenced reservations. Uncertain and successful charges both stay reserved."""

from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from scopecheck.providers.config import LiveConfig
from scopecheck.runs.models import CallReservation, Run
from scopecheck.runs.service import digest

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
    from scopecheck.runs.worker import owned_job

    config.price.require_current()
    amount = config.maximum_charge()
    async with AsyncSession(engine) as session, session.begin():
        await owned_job(session, claim, 60)
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
            select(func.coalesce(func.sum(CallReservation.amount_microusd), 0)).where(
                CallReservation.run_id == run.id,
                CallReservation.workspace_id == run.workspace_id,
            )
        )
        if int(spent or 0) + amount > budget:
            raise BudgetExceeded
        reservation_id = uuid4()
        session.add(
            CallReservation(
                id=reservation_id,
                workspace_id=claim.workspace_id,
                run_id=claim.run_id,
                case_id=case_id,
                repetition=repetition,
                sequence=sequence,
                lease_generation=claim.generation,
                amount_microusd=amount,
                price_hash=digest(config.price.model_dump(mode="json")),
            )
        )
    return reservation_id
