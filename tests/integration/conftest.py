"""Every test creates and drops its own database; never reset a supplied database."""

import asyncio
import os
from dataclasses import dataclass, field
from uuid import uuid4

import httpx
import psycopg
import pytest
import pytest_asyncio
from psycopg import sql
from sqlalchemy import select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from scopecheck.app import create_app
from scopecheck.auth import bootstrap, issue_token
from scopecheck.db import create_engine
from scopecheck.runs.models import Workspace
from scopecheck.settings import Settings


@dataclass
class Database:
    url: str = field(repr=False)
    engine: AsyncEngine


async def migration(url: str, *arguments: str) -> None:
    env = {**os.environ, "SCOPECHECK_DATABASE_URL": url}
    process = await asyncio.create_subprocess_exec(
        "alembic",
        *arguments,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await process.communicate()
    if process.returncode:
        pytest.fail("Alembic command failed; no credentials or raw diagnostics emitted")


@pytest_asyncio.fixture
async def database(monkeypatch):
    admin_url = os.environ.get("SCOPECHECK_TEST_ADMIN_URL")
    if not admin_url:
        pytest.fail(
            "Set SCOPECHECK_TEST_ADMIN_URL to a disposable PostgreSQL 18 admin URL"
        )
    parsed = make_url(admin_url)
    if parsed.drivername != "postgresql+psycopg":
        pytest.fail("Integration tests require postgresql+psycopg")
    name = "scopecheck_test_" + uuid4().hex
    conninfo = parsed.set(drivername="postgresql").render_as_string(hide_password=False)
    async with await psycopg.AsyncConnection.connect(
        conninfo, autocommit=True
    ) as admin:
        await admin.execute(
            sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(
                sql.Identifier(name)
            )
        )
    url = parsed.set(database=name).render_as_string(hide_password=False)
    monkeypatch.setenv("SCOPECHECK_DATABASE_URL", url)
    monkeypatch.setenv("SCOPECHECK_CODE_REVISION", "unknown")
    engine = create_engine(Settings())
    try:
        await migration(url, "upgrade", "head")
        yield Database(url, engine)
    finally:
        await engine.dispose()
        async with await psycopg.AsyncConnection.connect(
            conninfo, autocommit=True
        ) as admin:
            await admin.execute(
                sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name))
            )


@dataclass
class Api:
    client: httpx.AsyncClient
    editor: str = field(repr=False)
    reader: str = field(repr=False)
    other: str = field(repr=False)

    def headers(self, role: str = "editor", **extra: str) -> dict[str, str]:
        return {"Authorization": "Bearer " + getattr(self, role), **extra}


@pytest_asyncio.fixture
async def api(database):
    async with AsyncSession(database.engine) as session, session.begin():
        editor = await bootstrap(session, "one")
        other = await bootstrap(session, "two")
        workspace = await session.scalar(
            select(Workspace).where(Workspace.name == "one")
        )
        assert workspace is not None
        reader = await issue_token(session, workspace.id, "reader")
        assert editor is not None and other is not None
    app = create_app()
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            yield Api(client, editor, reader, other)
