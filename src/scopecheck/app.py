"""Liveness remains independent; readiness checks the migrated database."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Literal
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException
from starlette.middleware.base import RequestResponseEndpoint
from starlette.responses import Response

from scopecheck.api.errors import ApiError
from scopecheck.api.routes import router
from scopecheck.db import create_engine
from scopecheck.settings import Settings


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

    app = FastAPI(title="ScopeCheck", version="0.1.0", lifespan=lifespan)
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

    app.include_router(router)
    return app
