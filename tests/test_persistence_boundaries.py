import httpx
import pytest
from pydantic import ValidationError

from scopecheck.app import create_app
from scopecheck.settings import Settings

pytestmark = pytest.mark.unit


def test_database_secret_not_exposed(monkeypatch: pytest.MonkeyPatch) -> None:
    password = "synthetic-database-password"
    monkeypatch.setenv(
        "SCOPECHECK_DATABASE_URL",
        f"postgresql+psycopg://user:{password}@localhost/test",
    )
    settings = Settings()
    assert password not in repr(settings)
    assert password not in settings.model_dump_json()
    assert "database_url" not in settings.model_dump()


@pytest.mark.parametrize(
    "url", ["sqlite:///test.db", "postgresql://user@host/test", "not-a-url"]
)
def test_database_url_validation(monkeypatch: pytest.MonkeyPatch, url: str) -> None:
    monkeypatch.setenv("SCOPECHECK_DATABASE_URL", url)
    with pytest.raises(ValidationError) as caught:
        Settings()
    assert url not in str(caught.value)


@pytest.mark.asyncio
async def test_liveness_without_database_and_safe_readiness_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SCOPECHECK_DATABASE_URL", raising=False)
    app = create_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (await client.get("/health/live")).status_code == 200
        ready = await client.get("/health/ready")
        assert ready.status_code == 503
        assert set(ready.json()) == {"code", "message", "request_id"}
        assert (await client.get("/api/v1/datasets")).status_code == 401
