"""Bound artifact request bodies before JSON parsing or multipart spooling."""

import asyncio
import os
from starlette.responses import JSONResponse


class ArtifactBodyLimit:
    def __init__(self, app):
        self.app = app
        self.stream_capacity = asyncio.Semaphore(max(1, min(16, int(os.environ.get("BOW_ARTIFACT_MAX_STREAMS", "4")))))
        self.capacity = asyncio.Semaphore(max(1, min(64, int(os.environ.get("BOW_ARTIFACT_MAX_INFLIGHT", "16")))))

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        if scope["type"] != "http" or not path.startswith("/api/artifacts/") or "/runtime/" not in path:
            return await self.app(scope, receive, send)
        # No waiting request retains a large buffered body. Per-worker capacity
        # is transport backpressure, not persisted execution state.
        capacity = self.stream_capacity if path.endswith("/stream") else self.capacity
        if capacity.locked():
            return await JSONResponse(
                {
                    "detail": "Artifact service is busy; retry explicitly",
                    "error_code": "ARTIFACT_RESOURCE_RATE_LIMITED",
                },
                status_code=429,
            )(scope, receive, send)
        async with capacity:
            return await self._bounded(scope, receive, send)

    async def _bounded(self, scope, receive, send):
        path = scope.get("path", "")
        if (
            scope["type"] != "http"
            or not path.startswith("/api/artifacts/")
            or "/runtime/" not in path
            or scope.get("method") not in ("POST", "PUT", "PATCH")
        ):
            return await self.app(scope, receive, send)
        limit = 10 * 1024 * 1024 + 65536 if path.endswith("/upload") else 524288
        headers = dict(scope.get("headers", []))
        try:
            too_large = int(headers.get(b"content-length", b"0")) > limit
        except ValueError:
            too_large = True
        body = bytearray()
        if not too_large:
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                body.extend(message.get("body", b""))
                if len(body) > limit:
                    too_large = True
                    break
                if not message.get("more_body"):
                    break
        if too_large:
            response = JSONResponse(
                {"detail": "Artifact request exceeds its size limit", "error_code": "ARTIFACT_RESOURCE_QUOTA_EXCEEDED"},
                status_code=413,
            )
            return await response(scope, receive, send)
        consumed = False

        async def bounded_receive():
            nonlocal consumed
            if not consumed:
                consumed = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        await self.app(scope, bounded_receive, send)
