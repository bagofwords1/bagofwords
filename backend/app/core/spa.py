"""SPA static file serving for the bundled Nuxt build.

When SERVE_FRONTEND=1 (set in the production Docker image), mount the
generated Nuxt output at root so a single uvicorn process serves both
the API and the client-side app. In dev this is disabled; the Nuxt dev
server on :3000 still proxies to uvicorn on :8000.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse


IMMUTABLE_ASSET_CACHE_CONTROL = "public, max-age=31536000, immutable"
_LIB_HASH_RE = re.compile(r"[0-9a-f]{8,64}")
SPA_INDEX_CACHE_CONTROL = "no-cache"

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


# Artifact/MCP shell pages that run host-supplied code. Nothing frames them by
# URL: MCP hosts receive them as ui:// resource text (routes/mcp.py), the in-app
# artifact iframe is a srcdoc built by frontend/utils/artifactIframe.ts, and
# artifact-sandbox.html is only read from disk for headless previews. So no
# page may frame the app-origin copy — that is what stops an attacker page from
# becoming its parent and feeding it artifact code. Scoped to these files on
# purpose; this is not the site-wide header rollout. Mirrored for the Nuxt dev
# server in frontend/nuxt.config.ts routeRules.
ARTIFACT_SHELL_FILES = frozenset({"mcp-artifact-app.html", "mcp-visualization-app.html", "artifact-sandbox.html"})
ARTIFACT_SHELL_HEADERS = {
    "Content-Security-Policy": "frame-ancestors 'none'",
    "X-Frame-Options": "DENY",
}


def _cache_headers(
    spa_path: str, resolved: str, index_file: str, query: Mapping[str, str] | None = None,
) -> dict[str, str]:
    # Match on the resolved file, not the request path, so path variants
    # ("/mcp-artifact-app.html/", "/./mcp-artifact-app.html") get it too.
    if os.path.basename(resolved) in ARTIFACT_SHELL_FILES:
        return dict(ARTIFACT_SHELL_HEADERS)

    if resolved == index_file:
        return {"Cache-Control": SPA_INDEX_CACHE_CONTROL}

    if spa_path.lstrip("/").startswith("_nuxt/"):
        return {"Cache-Control": IMMUTABLE_ASSET_CACHE_CONTROL}

    # These are public, immutable-font assets, never API/user content. Opaque
    # artifact frames require CORS even when hosted by this same server.
    if spa_path.lstrip('/').startswith('libs/fonts/') and Path(resolved).suffix in {'.woff', '.woff2', '.ttf', '.otf'}:
        return {"Access-Control-Allow-Origin": "*"}

    # Vendored artifact libraries. The app references them with a build-time
    # content hash (`?h=`, frontend/utils/artifactIframe.ts libUrl): that URL
    # changes whenever the file does, so it can be cached for good. Anything
    # else (no hash: MCP shells, older bundles) must revalidate — the files are
    # replaced in place on upgrade, and a heuristically cached old runtime
    # paired with new artifact code breaks dashboards.
    if spa_path.lstrip('/').startswith('libs/') and Path(resolved).suffix in {'.js', '.css'}:
        if query and _LIB_HASH_RE.fullmatch(query.get('h') or ''):
            return {"Cache-Control": IMMUTABLE_ASSET_CACHE_CONTROL}
        return {"Cache-Control": "no-cache"}
    return {}


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

    dist_dir_str = str(dist_dir)
    index_file_str = str(index_file)

    @app.get("/{spa_path:path}", include_in_schema=False)
    async def spa_fallback(spa_path: str, request: Request):
        if _is_api_path(spa_path):
            raise HTTPException(status_code=404)

        # Inline path-traversal guard at the sink (Snyk python/PT). Reject
        # absolute paths, parent refs, and backslashes before joining; then
        # verify the resolved path stays under the dist dir.
        if (
            ".." in spa_path
            or spa_path.startswith("/")
            or "\\" in spa_path
            or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in spa_path)
        ):
            raise HTTPException(status_code=404)

        resolved = os.path.realpath(os.path.join(dist_dir_str, spa_path))
        if not resolved.startswith(dist_dir_str + os.sep) and resolved != dist_dir_str:
            raise HTTPException(status_code=404)

        if os.path.isfile(resolved):
            return FileResponse(
                resolved,
                headers=_cache_headers(spa_path, resolved, index_file_str, request.query_params),
            )

        return FileResponse(
            index_file_str,
            headers={"Cache-Control": SPA_INDEX_CACHE_CONTROL},
        )
