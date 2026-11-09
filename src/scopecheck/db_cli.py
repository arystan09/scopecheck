"""Local credential administration. Secret output is one-time stdout only."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from scopecheck.auth import Role, bootstrap, hash_token, issue_token, revoke
from scopecheck.db import create_engine
from scopecheck.runs.models import ApiToken, Workspace
from scopecheck.settings import Settings


async def administer(
    command: str,
    workspace: str,
    role: Role = "reader",
    token_id: UUID | None = None,
) -> dict[str, str | bool]:
    if not workspace.strip() or len(workspace) > 100:
        raise ValueError("Invalid workspace name")
    engine = create_engine(Settings())
    try:
        async with AsyncSession(engine) as session, session.begin():
            if command == "revoke":
                if token_id is None:
                    raise ValueError("Token id required")
                return {"revoked": await revoke(session, workspace, token_id)}
            if command == "bootstrap":
                token = await bootstrap(session, workspace)
                if token is None:
                    return {
                        "created": False,
                        "message": "Workspace already exists; no credential issued",
                    }
            else:
                workspace_id = await session.scalar(
                    select(Workspace.id).where(Workspace.name == workspace)
                )
                if workspace_id is None:
                    raise ValueError("Workspace not found")
                token = await issue_token(session, workspace_id, role)
            record_id = await session.scalar(
                select(ApiToken.id).where(ApiToken.token_hash == hash_token(token))
            )
            assert record_id is not None
            # The transaction commits before this return is delivered to the caller.
            return {"created": True, "token_id": str(record_id), "token": token}
    finally:
        await engine.dispose()
