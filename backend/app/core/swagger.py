"""Swagger UI page that works without internet access.

FastAPI's built-in /swagger page loads its JS and CSS from cdn.jsdelivr.net,
so it renders blank in air-gapped deployments. The production image vendors
Swagger UI into the frontend build (scripts/download-vendor-libs.sh →
/libs/swagger-ui/), and this page uses those files when they exist. Without
them (local dev, where the backend does not serve the frontend) it falls back
to the same CDN FastAPI uses.
"""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import HTMLResponse

# Keep in sync with SWAGGER_UI_VERSION in scripts/download-vendor-libs.sh.
SWAGGER_UI_VERSION = "5.33.2"
LOCAL_DIR = "libs/swagger-ui"
CDN_BASE = f"https://cdn.jsdelivr.net/npm/swagger-ui-dist@{SWAGGER_UI_VERSION}"


def _local_assets_available() -> bool:
    if os.environ.get("SERVE_FRONTEND", "").lower() not in ("1", "true", "yes"):
        return False
    dist_dir = Path(os.environ.get("FRONTEND_DIST_DIR", "/app/frontend/dist"))
    local = dist_dir / LOCAL_DIR
    return (local / "swagger-ui-bundle.js").is_file() and (local / "swagger-ui.css").is_file()


def swagger_asset_urls() -> dict[str, str]:
    if _local_assets_available():
        return {
            "swagger_js_url": f"/{LOCAL_DIR}/swagger-ui-bundle.js?v={SWAGGER_UI_VERSION}",
            "swagger_css_url": f"/{LOCAL_DIR}/swagger-ui.css?v={SWAGGER_UI_VERSION}",
            "swagger_favicon_url": "/favicon.ico",
        }
    return {
        "swagger_js_url": f"{CDN_BASE}/swagger-ui-bundle.js",
        "swagger_css_url": f"{CDN_BASE}/swagger-ui.css",
        "swagger_favicon_url": "https://fastapi.tiangolo.com/img/favicon.png",
    }


def mount_swagger_ui(app: FastAPI, path: str = "/swagger") -> None:
    """Serve Swagger UI at ``path``. Create the app with ``docs_url=None``."""
    assets = swagger_asset_urls()

    @app.get(path, include_in_schema=False)
    async def swagger_ui_html() -> HTMLResponse:
        return get_swagger_ui_html(
            openapi_url=app.openapi_url,
            title=f"{app.title} - Swagger UI",
            oauth2_redirect_url=app.swagger_ui_oauth2_redirect_url,
            init_oauth=app.swagger_ui_init_oauth,
            swagger_ui_parameters=app.swagger_ui_parameters,
            **assets,
        )
