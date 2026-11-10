"""Explicit local CLI seeding; no anonymous HTTP writes or live calls."""

from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession

from scopecheck.datasets.service import import_dataset
from scopecheck.db import create_engine
from scopecheck.runs.models import Workspace
from scopecheck.runs.service import ConfigInput, RunInput, create_config, create_run
from scopecheck.settings import Settings
from scopecheck.version import PACKAGE_VERSION


async def seed(workspace: str, path: Path) -> dict[str, Any]:
    settings = Settings()
    if settings.database_url is None or make_url(
        settings.database_url.get_secret_value()
    ).host not in {"localhost", "127.0.0.1", "::1", "db"}:
        raise ValueError("Demo seeding requires a local/disposable database")
    engine = create_engine(settings)
    try:
        async with AsyncSession(engine) as session, session.begin():
            workspace_id = await session.scalar(
                select(Workspace.id).where(Workspace.name == workspace)
            )
            if workspace_id is None:
                raise ValueError("Bootstrap the local workspace first")
            dataset, _ = await import_dataset(session, workspace_id, path.read_bytes())
            runs = {}
            for fixture in ("safe", "unsafe", "deny-all"):
                config, _ = await create_config(
                    session,
                    workspace_id,
                    ConfigInput.model_validate({"fixture": fixture}),
                )
                run, _ = await create_run(
                    session,
                    workspace_id,
                    RunInput(dataset_id=dataset.id, config_id=config.id),
                    f"demo-v1-{dataset.content_hash}-{fixture}",
                    settings.code_revision,
                )
                runs[fixture] = str(run.id)
            return {
                "synthetic": True,
                "package_version": PACKAGE_VERSION,
                "dataset_id": str(dataset.id),
                "runs": runs,
            }
    finally:
        await engine.dispose()
