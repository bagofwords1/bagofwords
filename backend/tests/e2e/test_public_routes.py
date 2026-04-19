"""End-to-end coverage for the new single-process serving plumbing.

These tests pin three contracts that the FastAPI-serves-everything PR
introduced (or changed) and that have no other regression coverage:

  * ``GET /health`` — used by k8s probes, the docker HEALTHCHECK, and the
    Playwright CI wait loop. Must always return 200 + ``{"status":"ok"}``.
  * ``GET /mcp`` and ``POST /mcp`` at the *root* — external MCP clients
    (Claude Code, Cursor, Claude Web) connect to ``<base>/mcp`` directly.
    The path used to exist via the Nuxt proxy rewrite ``/mcp → /api/mcp``;
    after the proxy went away we keep it by mounting ``mcp.router`` a
    second time without a prefix. We assert the alias is reachable and
    enforces auth identically to the ``/api/mcp`` variant.
  * ``GET /excel/manifest.xml`` — sideloaded by Office for the BOW Excel
    add-in. Same alias story as ``/mcp``.

The tests exist so a future refactor can't silently drop the alias or
the health endpoint and break deployments / external integrations.
"""

import pytest


@pytest.mark.e2e
def test_health_endpoint(test_client):
    response = test_client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.e2e
def test_mcp_root_alias_exists(test_client):
    """/mcp must respond — auth-required, but routed (i.e. not 404)."""
    # Both verbs are real routes (mcp.router defines GET and POST).
    for method in ("get", "post"):
        response = getattr(test_client, method)("/mcp")
        assert response.status_code != 404, (
            f"/mcp {method.upper()} returned 404 — root alias is missing. "
            "External MCP clients connect to <base>/mcp directly."
        )
        # Without auth, MCP returns 401 — same behaviour as /api/mcp.
        assert response.status_code in (401, 400), (
            f"/mcp {method.upper()} returned {response.status_code}; expected "
            "401/400 (auth required)."
        )


@pytest.mark.e2e
def test_mcp_root_alias_matches_api_mcp(test_client):
    """The two mount points must behave identically (auth-wise)."""
    for path in ("/mcp", "/api/mcp"):
        r = test_client.get(path)
        assert r.status_code != 404, f"{path} returned 404"
    r_root = test_client.get("/mcp")
    r_api = test_client.get("/api/mcp")
    assert r_root.status_code == r_api.status_code, (
        f"/mcp ({r_root.status_code}) and /api/mcp ({r_api.status_code}) "
        "diverged — root alias must mirror /api/mcp behaviour."
    )


@pytest.mark.e2e
def test_excel_manifest_root_alias(test_client):
    """The Office add-in manifest must be reachable at /excel/manifest.xml."""
    response = test_client.get("/excel/manifest.xml")
    assert response.status_code == 200, (
        f"/excel/manifest.xml returned {response.status_code} — Excel add-in "
        "sideload URL is broken."
    )
    body = response.text
    assert "<OfficeApp" in body, "Manifest response is not an Office add-in XML"
    # The same content must also be available via the /api/excel mount.
    api_response = test_client.get("/api/excel/manifest.xml")
    assert api_response.status_code == 200
