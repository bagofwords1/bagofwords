"""SPA static file serving for the bundled Nuxt build.

When SERVE_FRONTEND=1 (set in the production Docker image), mount the
generated Nuxt output at root so a single uvicorn process serves both
the API and the client-side app. In dev this is disabled; the Nuxt dev
server on :3000 still proxies to uvicorn on :8000.
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse


API_PREFIXES = (
    "api/",
    "ws/",
    "mcp",
    "excel",
    "scim/",
    ".well-known/",
    "slack_webhook",
    "teams_webhook",
    "whatsapp_webhook",
    "swagger",
    "openapi.json",
    "_nuxt_icon",
    "health",
)


def _is_api_path(path: str) -> bool:
    p = path.lstrip("/")
    for prefix in API_PREFIXES:
        if prefix.endswith("/"):
            if p.startswith(prefix) or p == prefix.rstrip("/"):
                return True
        else:
            if p == prefix or p.startswith(prefix + "/"):
                return True
    return False


def mount_spa(app: FastAPI) -> None:
    """Attach a SPA fallback GET handler to the app.

    Must be called AFTER all API routers are registered — the catch-all
    route is matched last by FastAPI, but registration order determines
    priority for overlapping paths.
    """
    if os.environ.get("SERVE_FRONTEND", "").lower() not in ("1", "true", "yes"):
        return

    dist_dir = Path(os.environ.get("FRONTEND_DIST_DIR", "/app/frontend/dist")).resolve()
    index_file = dist_dir / "index.html"

    if not index_file.is_file():
        raise RuntimeError(
            f"SERVE_FRONTEND is set but {index_file} does not exist. "
            "Did `nuxt generate` run during the image build?"
        )

    @app.get("/{spa_path:path}", include_in_schema=False)
    async def spa_fallback(spa_path: str, request: Request):
        if _is_api_path(spa_path):
            raise HTTPException(status_code=404)

        candidate = (dist_dir / spa_path).resolve()
        try:
            candidate.relative_to(dist_dir)
        except ValueError:
            raise HTTPException(status_code=404)

        if candidate.is_file():
            return FileResponse(candidate)

        return FileResponse(index_file)
