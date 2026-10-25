"""Vendored /libs files are cached for good only when the URL carries a hash.

The app references artifact libraries with a build-time content hash
(frontend/utils/artifactIframe.ts libUrl → `?h=<hex>`, hashes computed in
nuxt.config.ts). Such a URL changes whenever the file does, so the SPA handler
serves it immutable. Every other request for a lib (MCP shells, an older
bundle, a hand-typed URL) must revalidate: lib files are replaced in place on
upgrade, and a cached old runtime paired with new artifact code breaks
dashboards.
"""
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.spa import IMMUTABLE_ASSET_CACHE_CONTROL, mount_spa

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def spa_client(tmp_path, monkeypatch):
    (tmp_path / "index.html").write_text("<html>Application</html>")
    (tmp_path / "libs" / "fonts").mkdir(parents=True)
    (tmp_path / "libs" / "echarts-5.min.js").write_text("// lib")
    (tmp_path / "libs" / "artifact-fonts.css").write_text("/* css */")
    (tmp_path / "libs" / "fonts" / "inter.woff2").write_bytes(b"\x00")
    (tmp_path / "_nuxt").mkdir()
    (tmp_path / "_nuxt" / "entry.abc.js").write_text("// app")
    monkeypatch.setenv("SERVE_FRONTEND", "true")
    monkeypatch.setenv("FRONTEND_DIST_DIR", str(tmp_path))
    app = FastAPI()
    mount_spa(app)
    with TestClient(app) as client:
        yield client


@pytest.mark.parametrize("path", [
    "/libs/echarts-5.min.js?h=3f2a9c0d1e4b5a6c",
    "/libs/artifact-fonts.css?v=11&h=0123456789abcdef",
    "/libs/echarts-5.min.js?v=2&revision=x&h=deadbeefcafe",
])
def test_hashed_lib_urls_are_immutable(spa_client, path):
    resp = spa_client.get(path)
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == IMMUTABLE_ASSET_CACHE_CONTROL


@pytest.mark.parametrize("path", [
    "/libs/echarts-5.min.js",
    "/libs/echarts-5.min.js?v=11",
    "/libs/echarts-5.min.js?h=",
    "/libs/echarts-5.min.js?h=not-a-hash",
    "/libs/echarts-5.min.js?h=ABCDEF0123456789",  # the build emits lowercase hex only
    "/libs/artifact-fonts.css",
])
def test_unhashed_lib_urls_must_revalidate(spa_client, path):
    resp = spa_client.get(path)
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "no-cache"


def test_other_static_policies_are_unchanged(spa_client):
    assert spa_client.get("/").headers["cache-control"] == "no-cache"
    assert spa_client.get("/_nuxt/entry.abc.js").headers["cache-control"] == IMMUTABLE_ASSET_CACHE_CONTROL
    font = spa_client.get("/libs/fonts/inter.woff2?h=0123456789abcdef")
    assert font.headers.get("access-control-allow-origin") == "*"


def test_frontend_emits_the_hash_parameter_the_backend_honors():
    """Both halves of the contract: the URL builder appends `h=<hash>` and the
    build computes lowercase hex hashes."""
    iframe = (ROOT / "frontend" / "utils" / "artifactIframe.ts").read_text(encoding="utf-8")
    assert "`h=${hash}`" in iframe
    cfg = (ROOT / "frontend" / "nuxt.config.ts").read_text(encoding="utf-8")
    assert '__BOW_LIB_HASHES__' in cfg and 'digest("hex")' in cfg
