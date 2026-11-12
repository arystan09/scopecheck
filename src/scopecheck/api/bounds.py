"""Bound bodies before FastAPI parses JSON, including chunked/false-length input."""

import asyncio
from uuid import uuid4

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send


class RequestBounds:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        if scope["type"] != "http" or scope["method"] not in {"POST", "PUT", "PATCH"}:
            await self.app(scope, receive, send)
            return
        limit = 64 if path == "/demo/runs" else 16_384
        if path == "/api/v1/datasets":
            limit = 1024 * 1024
        body = bytearray()
        status = 0
        try:
            async with asyncio.timeout(5):
                while True:
                    message = await receive()
                    if message["type"] == "http.disconnect":
                        return
                    chunk = message.get("body", b"")
                    if len(body) + len(chunk) > limit:
                        status = 413
                        break
                    body.extend(chunk)
                    if not message.get("more_body", False):
                        break
        except TimeoutError:
            status = 408
        if status:
            request_id = scope.get("state", {}).get("request_id", str(uuid4()))
            response = JSONResponse(
                {
                    "code": "body_too_large" if status == 413 else "body_timeout",
                    "message": "Request body exceeds limits",
                    "request_id": request_id,
                },
                status_code=status,
                headers={"X-Request-ID": request_id, "Cache-Control": "no-store"},
            )
            await response(scope, receive, send)
            return
        delivered = False

        async def bounded_receive() -> Message:
            nonlocal delivered
            if delivered:
                return await receive()
            delivered = True
            return {"type": "http.request", "body": bytes(body), "more_body": False}

        await self.app(scope, bounded_receive, send)
