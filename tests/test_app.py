import httpx
import pytest
from pydantic import ValidationError

from scopecheck.app import create_app

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_liveness(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCOPECHECK_MODE", "fake")
    transport = httpx.ASGITransport(app=create_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health/live")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}
        assert response.headers["content-type"] == "application/json"
        assert (await client.post("/health/live")).status_code == 405
        assert (await client.get("/missing")).status_code == 404


def test_app_rejects_invalid_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCOPECHECK_MODE", "provider")
    with pytest.raises(ValidationError):
        create_app()
