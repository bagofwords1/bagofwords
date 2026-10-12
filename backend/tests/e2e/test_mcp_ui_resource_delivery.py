"""MCP ui:// resources are delivered as host content (Aikido 828759199).

Invariant: every UI resource advertised by resources/list is readable through
resources/read as inline HTML text carrying the host-delivery marker — the
only copy of the shells that starts their postMessage runtime. (The app-origin
URL copy lacks it and is unframeable; see tests/unit/test_artifact_shell_framing.py.)
"""
import uuid

import pytest

from app.routes.mcp import MCP_HOST_DELIVERED_MARKER


def _call(test_client, token, org_id, method, params=None):
    resp = test_client.post(
        "/api/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
        headers={"Authorization": f"Bearer {token}", "X-Organization-Id": org_id},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "error" not in body, body
    return body["result"]


@pytest.mark.e2e
def test_every_ui_resource_is_inline_host_delivered_html(test_client, create_user, login_user, whoami):
    email = f"mcpui_{uuid.uuid4().hex[:8]}@test.com"
    create_user(email=email, password="test123")
    token = login_user(email=email, password="test123")
    org_id = whoami(token)["organizations"][0]["id"]

    resources = _call(test_client, token, org_id, "resources/list")["resources"]
    ui = [r for r in resources if r["uri"].startswith("ui://")]
    assert ui, "server advertises no UI resources"
    for res in ui:
        contents = _call(test_client, token, org_id, "resources/read", {"uri": res["uri"]})["contents"]
        assert len(contents) == 1
        text = contents[0]["text"]
        assert "not found" not in text[:200].lower(), f"{res['uri']} bundle missing"
        assert MCP_HOST_DELIVERED_MARKER in text, f"{res['uri']} lacks the host-delivery marker"
        # Stamped as the first script, before any of the shell's own code runs.
        assert text.lower().index("<script") == text.index(MCP_HOST_DELIVERED_MARKER)
