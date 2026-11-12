import asyncio
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError

from scopecheck.api.bounds import RequestBounds
from scopecheck.app import create_app
from scopecheck.providers.openai import client_for_worker
from scopecheck.runs.budget import reserve_call
from scopecheck.settings import Settings

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("name", ["SCOPECHECK_OPENAI_API_KEY", "OPENAI_API_KEY"])
def test_public_mode_rejects_credentials_without_exposure(monkeypatch, name):
    monkeypatch.setenv("SCOPECHECK_PUBLIC_DEMO", "true")
    monkeypatch.setenv(name, "synthetic-private-key")
    with pytest.raises(ValidationError) as error:
        Settings()
    assert "synthetic-private-key" not in str(error.value)
    assert "synthetic-private-key" not in repr(error.value)
    with pytest.raises(ValidationError):
        create_app()


@pytest.mark.asyncio
async def test_public_cannot_construct_live_client_or_reserve(monkeypatch, live_config):
    monkeypatch.setenv("SCOPECHECK_PUBLIC_DEMO", "true")
    monkeypatch.delenv("SCOPECHECK_OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(ValueError, match="disabled"):
        client_for_worker("synthetic-unused")
    with pytest.raises(ValueError, match="disabled"):
        await reserve_call(None, None, str(uuid4()), 1, 1, live_config)


@pytest.mark.asyncio
async def test_body_deadline_before_parser(monkeypatch):
    entered = False

    async def app(scope, receive, send):
        nonlocal entered
        entered = True

    async def receive():
        await asyncio.sleep(0)
        raise TimeoutError

    messages = []

    async def send(message):
        messages.append(message)

    await RequestBounds(app)(
        {"type": "http", "method": "POST", "path": "/demo/runs"}, receive, send
    )
    assert not entered
    assert messages[0]["status"] == 408


@pytest.mark.asyncio
async def test_oversized_json_is_rejected_before_auth_or_database(monkeypatch):
    monkeypatch.delenv("SCOPECHECK_DATABASE_URL", raising=False)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/v1/configs", content=b'{"arbitrary":"' + b"x" * 17000
        )
        assert response.status_code == 413
        assert response.json()["code"] == "body_too_large"
        assert response.headers["X-Request-ID"] == response.json()["request_id"]
