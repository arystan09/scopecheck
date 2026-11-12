"""Opt-in anonymous admission of exactly one release-fixed fake workload."""

from fastapi import APIRouter, Request

from scopecheck.api.errors import ApiError
from scopecheck.api.routes import Session
from scopecheck.datasets.demo import admit_demo

router = APIRouter()


@router.post("/demo/runs", status_code=202)
async def demo_run(request: Request, session: Session) -> dict[str, str | bool]:
    if not request.app.state.settings.public_demo:
        raise ApiError(404, "not_found", "Resource not found")
    # Empty body or exactly {} only. No query, token, prompt, endpoint or model knobs.
    if request.query_params or (await request.body()).strip() not in (b"", b"{}"):
        raise ApiError(422, "invalid_request", "Public demo accepts no overrides")
    run = await admit_demo(session, request.app.state.settings)
    return {"id": str(run.id), "status": run.status, "mode": "fake", "synthetic": True}
