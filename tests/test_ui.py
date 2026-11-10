from pathlib import Path

import httpx
import pytest

from scopecheck.app import create_app
from scopecheck.ui import TEMPLATES
from scopecheck.version import PACKAGE_VERSION

pytestmark = pytest.mark.unit


def test_jinja_escapes_hostile_string() -> None:
    hostile = '<script>alert("owned")</script><img src=x onerror=alert(1)>'
    page = TEMPLATES.env.get_template("app.html").render(
        title=hostile, package_version=PACKAGE_VERSION
    )
    assert hostile not in page
    assert "&lt;script&gt;" in page and "&lt;img" in page


def test_js_uses_text_dom_and_memory_only() -> None:
    js = Path("src/scopecheck/static/app.js").read_text()
    for forbidden in (
        "innerHTML",
        "outerHTML",
        "insertAdjacentHTML",
        "localStorage",
        "sessionStorage",
        "document.cookie",
        "eval(",
    ):
        assert forbidden not in js
    assert "textContent" in js and 'el("token").value = ""' in js


@pytest.mark.asyncio
async def test_public_shell_does_not_allow_public_writes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SCOPECHECK_DATABASE_URL", raising=False)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()), base_url="http://test"
    ) as client:
        page = await client.get("/ui")
        assert page.status_code == 200 and "SYNTHETIC" in page.text
        assert "script-src 'self'" in page.headers["content-security-policy"]
        assert "set-cookie" not in page.headers
        assert (await client.get("/static/app.js")).status_code == 200
        assert (await client.get("/static/app.css")).status_code == 200
        assert (await client.post("/api/v1/runs", json={})).status_code == 401
        assert (await client.get("/health/version")).json()[
            "package_version"
        ] == PACKAGE_VERSION
