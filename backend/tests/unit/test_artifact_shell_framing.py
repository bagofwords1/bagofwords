"""Artifact/MCP shell pages can't be framed from the app origin (Aikido 828759199).

Invariants:
  - Every serving path of the three shell pages in production (the SPA static
    handler) answers with ``frame-ancestors 'none'`` + ``X-Frame-Options: DENY``,
    whatever spelling of the path reaches the file.
  - The header is scoped to those shells: the SPA index and other static files
    are unchanged (the site-wide header rollout is deliberately separate).
  - The dev server (Nuxt routeRules) declares the same policy for each shell.
  - Each MCP shell gates its postMessage runtime on the marker that only the
    ui:// resource delivery stamps in, so the URL-served copy stays inert.
"""
import re
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.spa import mount_spa

ROOT = Path(__file__).resolve().parents[3]
SHELLS = ["mcp-artifact-app.html", "mcp-visualization-app.html", "artifact-sandbox.html"]


@pytest.fixture
def spa_client(tmp_path, monkeypatch):
    (tmp_path / "index.html").write_text("<html>Application</html>")
    for name in SHELLS:
        (tmp_path / name).write_text(f"<html><head></head><body>{name}</body></html>")
    (tmp_path / "libs").mkdir()
    (tmp_path / "libs" / "react.js").write_text("// lib")
    (tmp_path / "favicon.ico").write_bytes(b"\x00")
    monkeypatch.setenv("SERVE_FRONTEND", "true")
    monkeypatch.setenv("FRONTEND_DIST_DIR", str(tmp_path))
    app = FastAPI()
    mount_spa(app)
    with TestClient(app) as client:
        yield client


def _frame_ancestors(headers) -> str | None:
    csp = headers.get("content-security-policy") or ""
    m = re.search(r"frame-ancestors\s+([^;]+)", csp)
    return m.group(1).strip() if m else None


@pytest.mark.parametrize("name", SHELLS)
@pytest.mark.parametrize("variant", ["/{n}", "/{n}/", "/./{n}", "/libs/../{n}"])
def test_shell_pages_refuse_framing_on_every_path_spelling(spa_client, name, variant):
    resp = spa_client.get(variant.format(n=name))
    if resp.status_code == 404:
        pytest.skip("this spelling is rejected outright, which is also safe")
    assert resp.status_code == 200
    assert name in resp.text, "the shell itself must be what was served"
    assert _frame_ancestors(resp.headers) == "'none'"
    assert resp.headers.get("x-frame-options", "").upper() == "DENY"


@pytest.mark.parametrize("path", ["/", "/reports/123", "/libs/react.js", "/favicon.ico"])
def test_frame_policy_is_scoped_to_shell_pages(spa_client, path):
    resp = spa_client.get(path)
    assert resp.status_code == 200
    assert _frame_ancestors(resp.headers) is None
    assert "x-frame-options" not in resp.headers


@pytest.mark.parametrize("name", SHELLS)
def test_nuxt_dev_server_declares_same_frame_policy(name):
    cfg = (ROOT / "frontend" / "nuxt.config.ts").read_text(encoding="utf-8")
    line = next((ln for ln in cfg.splitlines() if f"'/{name}'" in ln), None)
    assert line is not None, f"routeRules has no entry for /{name}"
    assert "frame-ancestors 'none'" in line
    assert "'X-Frame-Options': 'DENY'" in line


@pytest.mark.parametrize("name", ["mcp-artifact-app.html", "mcp-visualization-app.html"])
def test_mcp_shell_runtime_is_gated_on_host_delivery_marker(name):
    from app.routes.mcp import MCP_HOST_DELIVERED_MARKER

    flag = re.search(r"window\.(\w+)\s*=\s*true", MCP_HOST_DELIVERED_MARKER).group(1)
    html = (ROOT / "frontend" / "public" / name).read_text(encoding="utf-8")
    assert f"window.{flag}=" not in html.replace(" ", ""), "static copy must never set the marker"
    gate = html.find(f"if (!window.{flag})")
    listener = html.find("addEventListener('message'")
    assert gate != -1, "shell must check the host-delivery marker"
    assert listener != -1 and gate < listener, "the gate must run before the message listener is registered"
