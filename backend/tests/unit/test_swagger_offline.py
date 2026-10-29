"""/swagger must render without internet access when Swagger UI is vendored.

The production image vendors Swagger UI under /libs/swagger-ui/
(scripts/download-vendor-libs.sh). Air-gapped hosts cannot reach a CDN, so the
page may only reference same-origin assets, and those assets must be served.
"""
import re

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.spa import mount_spa
from app.core.swagger import mount_swagger_ui


def _build_app() -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url="/openapi.json")

    @app.get("/api/ping")
    async def ping():
        return {"ok": True}

    mount_swagger_ui(app)
    mount_spa(app)
    return app


def _asset_urls(html: str) -> list[str]:
    return re.findall(r"""(?:src|href|url)\s*[=:]\s*['"]([^'"]+)['"]""", html)


@pytest.fixture
def vendored_dist(tmp_path, monkeypatch):
    (tmp_path / "index.html").write_text("<html>Application</html>")
    (tmp_path / "favicon.ico").write_bytes(b"\x00")
    lib = tmp_path / "libs" / "swagger-ui"
    lib.mkdir(parents=True)
    (lib / "swagger-ui-bundle.js").write_text("window.SwaggerUIBundle = function () {};")
    (lib / "swagger-ui.css").write_text(".swagger-ui {}")
    monkeypatch.setenv("SERVE_FRONTEND", "true")
    monkeypatch.setenv("FRONTEND_DIST_DIR", str(tmp_path))
    return tmp_path


def test_swagger_page_loads_only_same_origin_assets_when_vendored(vendored_dist):
    with TestClient(_build_app()) as client:
        page = client.get("/swagger")
        assert page.status_code == 200
        urls = _asset_urls(page.text)
        assert any("swagger-ui-bundle.js" in u for u in urls)
        assert any("swagger-ui.css" in u for u in urls)
        for url in urls:
            assert not re.match(r"^(https?:)?//", url), f"external asset on /swagger: {url}"
            asset = client.get(url)
            assert asset.status_code == 200, url
        assert client.get("/openapi.json").json()["paths"]


def test_swagger_page_falls_back_to_cdn_without_vendored_assets(tmp_path, monkeypatch):
    monkeypatch.delenv("SERVE_FRONTEND", raising=False)
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url="/openapi.json")
    mount_swagger_ui(app)
    with TestClient(app) as client:
        page = client.get("/swagger")
        assert page.status_code == 200
        assert any(u.startswith("https://") and "swagger-ui-bundle.js" in u for u in _asset_urls(page.text))
