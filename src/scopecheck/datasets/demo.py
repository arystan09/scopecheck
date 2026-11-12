"""Fixed synthetic work and durable admission, never client-supplied configuration."""

from datetime import timedelta
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from scopecheck.api.errors import ApiError
from scopecheck.datasets.canonicalize import canonical_json, content_hash
from scopecheck.datasets.service import import_dataset
from scopecheck.datasets.splits import MANIFEST_HASH, frozen_dataset
from scopecheck.runs.models import DemoAdmission, Run, RunJob, Workspace
from scopecheck.runs.service import ConfigInput, RunInput, create_config, create_run
from scopecheck.settings import Settings

DEMO_WORKSPACE = UUID("352eb6f3-a511-4b66-bf0e-d4abcda61791")
MAX_ADMISSIONS = 100
MAX_QUEUED_OR_RUNNING = 2
THROTTLE_SECONDS = 5


def fixed_hash() -> str:
    return content_hash(frozen_dataset("dev"))


async def admit_demo(session: AsyncSession, settings: Settings) -> Run:
    if not settings.public_demo:
        raise ApiError(404, "not_found", "Resource not found")
    quota = await session.scalar(
        select(DemoAdmission).where(DemoAdmission.id == 1).with_for_update()
    )
    if quota is None:
        raise ApiError(503, "schema_not_ready", "Database schema is not ready")
    now = await session.scalar(select(func.clock_timestamp()))
    assert now is not None
    if quota.admitted >= MAX_ADMISSIONS or (
        quota.last_admitted_at is not None
        and now < quota.last_admitted_at + timedelta(seconds=THROTTLE_SECONDS)
    ):
        raise ApiError(429, "demo_limit", "Public demo admission limit reached")
    active = await session.scalar(
        select(func.count())
        .select_from(RunJob)
        .where(
            RunJob.workspace_id == DEMO_WORKSPACE,
            RunJob.status.in_(["queued", "running"]),
        )
    )
    if active is not None and active >= MAX_QUEUED_OR_RUNNING:
        raise ApiError(429, "demo_limit", "Public demo admission limit reached")
    await session.execute(
        insert(Workspace)
        .values(id=DEMO_WORKSPACE, name="scopecheck-public-demo")
        .on_conflict_do_nothing(index_elements=["id"])
    )
    dataset, _ = await import_dataset(
        session,
        DEMO_WORKSPACE,
        canonical_json(frozen_dataset("dev").model_dump(mode="json")).encode(),
    )
    config, _ = await create_config(
        session, DEMO_WORKSPACE, ConfigInput(fixture="safe")
    )
    run, _ = await create_run(
        session,
        DEMO_WORKSPACE,
        RunInput(dataset_id=dataset.id, config_id=config.id),
        "demo-" + uuid4().hex,
        settings.code_revision,
        demo_manifest=MANIFEST_HASH,
    )
    quota.admitted += 1
    quota.last_admitted_at = now
    # Caller commits this counter together with the new run/job or rolls all back.
    await session.flush()
    return run
