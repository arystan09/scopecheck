"""Async PostgreSQL connections; no implicit schema creation."""

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from scopecheck.settings import Settings

SCHEMA_REVISION = "0002"


def create_engine(settings: Settings) -> AsyncEngine:
    if settings.database_url is None:
        raise ValueError("SCOPECHECK_DATABASE_URL is required")
    return create_async_engine(
        settings.database_url.get_secret_value(),
        pool_pre_ping=True,
        hide_parameters=True,
        connect_args={"connect_timeout": 5, "options": "-c timezone=UTC"},
    )
