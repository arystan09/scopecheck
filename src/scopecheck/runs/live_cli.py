"""Local DB-owner authority only; no bearer-token or public HTTP live writes."""

from pathlib import Path
from typing import Any
from uuid import UUID

from openai import __version__ as OPENAI_SDK_VERSION
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession

from scopecheck.db import create_engine
from scopecheck.providers.config import LiveConfig
from scopecheck.providers.openai import TOOL_SCHEMA_HASH, generation_settings
from scopecheck.runs.models import DatasetVersion, Workspace
from scopecheck.runs.service import digest, queue_run, store_config
from scopecheck.settings import Settings


async def queue_live(
    workspace: str,
    dataset_id: UUID,
    config: LiveConfig,
    budget_microusd: int,
    owner_authorized: bool,
    key: str,
) -> dict[str, Any]:
    if (
        owner_authorized is not True
        or type(budget_microusd) is not int
        or not 0 < budget_microusd <= 10**12
    ):
        raise ValueError("Explicit owner authorization and budget required")
    if not key or len(key) > 128:
        raise ValueError("Idempotency key required")
    config.price.require_current()
    if budget_microusd < config.maximum_charge():
        raise ValueError("Budget cannot fund one conservative request reservation")
    settings = Settings()
    if settings.database_url is None or make_url(
        settings.database_url.get_secret_value()
    ).host not in {
        "127.0.0.1",
        "localhost",
        "::1",
        "db",
    }:
        raise ValueError("Local database required")
    engine = create_engine(settings)
    try:
        async with AsyncSession(engine) as session, session.begin():
            # Local DB ownership, not an editor/reader API token, authorizes spending.
            owner = await session.scalar(
                text("""
                SELECT pg_has_role(current_user, datdba, 'MEMBER')
                FROM pg_database WHERE datname = current_database()
            """)
            )
            if owner is not True:
                raise ValueError("Database-owner credentials required")
            workspace_id = await session.scalar(
                select(Workspace.id).where(Workspace.name == workspace)
            )
            dataset = await session.scalar(
                select(DatasetVersion).where(
                    DatasetVersion.id == dataset_id,
                    DatasetVersion.workspace_id == workspace_id,
                )
            )
            if workspace_id is None or dataset is None:
                raise ValueError("Owned dataset required")
            stored, _ = await store_config(
                session, workspace_id, config.model_dump(mode="json")
            )
            snapshot = {
                "dataset_hash": dataset.content_hash,
                "config_hash": stored.content_hash,
                "policy_version": "1",
                "evaluator_version": "1",
                "tool_schema_version": "1",
                "tool_schema_hash": TOOL_SCHEMA_HASH,
                "dataset_schema_version": dataset.content["schema_version"],
                "code_revision": settings.code_revision,
                "mode": "live",
                "provider": "openai",
                "provider_sdk_version": OPENAI_SDK_VERSION,
                "model": config.model,
                "generation_settings": generation_settings(config),
                "generation": config.generation.model_dump(mode="json"),
                "price_snapshot": config.price.model_dump(mode="json"),
                "budget_microusd": budget_microusd,
                "owner_authorized": True,
                "authorization": "local_database_owner",
                "repetitions": 1,
                "limits": {
                    "max_tool_calls": 6,
                    "max_model_requests": 4,
                    "case_deadline_seconds": 30,
                },
            }
            request_hash = digest({"dataset_id": str(dataset.id), **snapshot})
            run, created = await queue_run(
                session, workspace_id, dataset, stored, key, request_hash, snapshot
            )
            return {
                "run_id": str(run.id),
                "created": created,
                "status": run.status,
                "mode": "live",
                "synthetic": True,
                "budget_microusd": budget_microusd,
            }
    finally:
        await engine.dispose()


def load_live_config(path: Path) -> LiveConfig:
    if path.stat().st_size > 16_384:
        raise ValueError("Configuration too large")
    return LiveConfig.model_validate_json(path.read_bytes())
