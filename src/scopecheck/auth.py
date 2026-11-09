"""Opaque operator tokens. Only digests reach persistence."""

import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from scopecheck.runs.models import ApiToken, Workspace

Role = Literal["reader", "editor"]


@dataclass(frozen=True)
class Principal:
    workspace_id: UUID
    role: str


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def authenticate(session: AsyncSession, token: str) -> Principal | None:
    record = await session.scalar(
        select(ApiToken).where(
            ApiToken.token_hash == hash_token(token),
            ApiToken.revoked_at.is_(None),
        )
    )
    return Principal(record.workspace_id, record.role) if record else None


async def issue_token(session: AsyncSession, workspace_id: UUID, role: Role) -> str:
    token = secrets.token_urlsafe(32)  # 256 bits of cryptographic entropy.
    session.add(
        ApiToken(workspace_id=workspace_id, token_hash=hash_token(token), role=role)
    )
    await session.flush()
    return token


async def bootstrap(session: AsyncSession, name: str) -> str | None:
    workspace_id = await session.scalar(
        insert(Workspace)
        .values(name=name)
        .on_conflict_do_nothing(index_elements=[Workspace.name])
        .returning(Workspace.id)
    )
    if workspace_id is None:
        return None
    return await issue_token(session, workspace_id, "editor")


async def revoke(session: AsyncSession, workspace: str, token_id: UUID) -> bool:
    workspace_id = (
        select(Workspace.id).where(Workspace.name == workspace).scalar_subquery()
    )
    result = await session.scalar(
        update(ApiToken)
        .where(
            ApiToken.id == token_id,
            ApiToken.workspace_id == workspace_id,
            ApiToken.revoked_at.is_(None),
        )
        .values(revoked_at=datetime.now(UTC))
        .returning(ApiToken.id)
    )
    return result is not None
