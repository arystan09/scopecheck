"""HTTP liveness without database or provider dependencies."""

from typing import Literal

from fastapi import FastAPI
from pydantic import BaseModel

from scopecheck.settings import Settings


class Liveness(BaseModel):
    status: Literal["ok"] = "ok"


def create_app() -> FastAPI:
    Settings()
    app = FastAPI(title="ScopeCheck", version="0.1.0")

    @app.get("/health/live", response_model=Liveness)
    async def live() -> Liveness:
        return Liveness()

    return app
