"""Public shell only; every data read/write still uses authenticated API routes."""

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from scopecheck.version import PACKAGE_VERSION

ROOT = Path(__file__).parent
TEMPLATES = Jinja2Templates(directory=ROOT / "templates")
router = APIRouter()


@router.get("/", include_in_schema=False)
async def home() -> RedirectResponse:
    return RedirectResponse("/ui")


@router.get("/ui", response_class=HTMLResponse, include_in_schema=False)
async def shell(request: Request) -> HTMLResponse:
    return TEMPLATES.TemplateResponse(
        request=request,
        name="app.html",
        context={
            "title": "ScopeCheck",
            "package_version": PACKAGE_VERSION,
        },
    )
