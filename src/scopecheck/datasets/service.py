"""Atomic immutable dataset import, deduplicated within an operator workspace."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from scopecheck.api.errors import ApiError
from scopecheck.datasets.canonicalize import content_hash, parse_dataset
from scopecheck.runs.models import DatasetVersion

MAX_IMPORT_BYTES = 1024 * 1024
MAX_CASES = 100


async def import_dataset(
    session: AsyncSession,
    workspace_id: UUID,
    raw: bytes,
) -> tuple[DatasetVersion, bool]:
    if len(raw) > MAX_IMPORT_BYTES:
        raise ApiError(413, "dataset_too_large", "Dataset exceeds 1 MiB")
    try:
        dataset = parse_dataset(raw.decode("utf-8"))
        digest = content_hash(dataset)
    except (ValueError, RecursionError):
        raise ApiError(422, "invalid_dataset", "Dataset validation failed") from None
    if len(dataset.cases) > MAX_CASES:
        raise ApiError(422, "too_many_cases", "Dataset exceeds 100 cases")
    record = await session.scalar(
        insert(DatasetVersion)
        .values(
            workspace_id=workspace_id,
            content_hash=digest,
            content=dataset.model_dump(mode="json"),
        )
        .on_conflict_do_nothing(index_elements=["workspace_id", "content_hash"])
        .returning(DatasetVersion)
    )
    if record is not None:
        return record, True
    existing = await session.scalar(
        select(DatasetVersion).where(
            DatasetVersion.workspace_id == workspace_id,
            DatasetVersion.content_hash == digest,
        )
    )
    assert existing is not None
    return existing, False
