"""Liveness remains independent; readiness checks the migrated database."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Literal
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException
from starlette.middleware.base import RequestResponseEndpoint
from starlette.responses import Response

from scopecheck.api.bounds import RequestBounds
from scopecheck.api.demo import router as demo_router
from scopecheck.api.errors import ApiError
from scopecheck.api.routes import router
from scopecheck.db import create_engine
from scopecheck.settings import Settings
from scopecheck.ui import ROOT
from scopecheck.ui import router as ui_router
from scopecheck.version import PACKAGE_VERSION


class Liveness(BaseModel):
    status: Literal["ok"] = "ok"


def error_response(
    request: Request, status: int, code: str, message: str
) -> JSONResponse:
    headers = {"WWW-Authenticate": "Bearer"} if status == 401 else None
    return JSONResponse(
        status_code=status,
        content={
            "code": code,
            "message": message,
            "request_id": request.state.request_id,
        },
        headers=headers,
    )


def create_app() -> FastAPI:
    settings = Settings()
    engine = create_engine(settings) if settings.database_url is not None else None

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        yield
        if engine is not None:
            await engine.dispose()

    app = FastAPI(
        title="ScopeCheck",
        version=PACKAGE_VERSION,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
    )
    app.add_middleware(RequestBounds)
    app.state.engine = engine
    app.state.settings = settings

    @app.middleware("http")
    async def safe_errors(
        request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        request.state.request_id = str(uuid4())
        try:
            response = await call_next(request)
        except SQLAlchemyError:
            response = error_response(
                request, 503, "database_unavailable", "Database is unavailable"
            )
        except Exception:
            response = error_response(
                request, 500, "internal_error", "Internal server error"
            )
        response.headers["X-Request-ID"] = request.state.request_id
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "connect-src 'self'; object-src 'none'; base-uri 'none'; "
            "frame-ancestors 'none'"
        )
        return response

    @app.exception_handler(ApiError)
    async def api_error(request: Request, error: ApiError) -> JSONResponse:
        return error_response(request, error.status, error.code, error.message)

    @app.exception_handler(RequestValidationError)
    async def validation_error(
        request: Request, error: RequestValidationError
    ) -> JSONResponse:
        return error_response(
            request, 422, "invalid_request", "Request validation failed"
        )

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, error: HTTPException) -> JSONResponse:
        return error_response(
            request, error.status_code, "http_error", "Request could not be processed"
        )

    @app.get("/health/live", response_model=Liveness)
    async def live() -> Liveness:
        return Liveness()

    @app.get("/health/version")
    async def package_version() -> dict[str, str]:
        return {"package_version": PACKAGE_VERSION, "mode": "fake"}

    app.include_router(ui_router)
    app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
    app.include_router(router)
    app.include_router(demo_router)
    return app
