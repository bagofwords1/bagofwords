"""RBAC + per-user policy tests for agent (data source) MCP/custom-API tools.

Covers the three policy layers and who may write each one:

  * ConnectionTool.policy            — org default, admin ('manage_connections')
  * DataSourceConnectionTool.policy  — per-agent overlay, agent admin ('manage')
  * UserConnectionToolPreference     — per-user, any member with 'view'

plus the resolution invariants (user preference wins, admin deny is absolute),
gateway enforcement of the user layer, the 'ask' confirmation endpoint's
authorization, and the refresh-tools empty-result guard.
"""

import asyncio
import contextlib
import json
import uuid

import pytest
from unittest.mock import patch

from tests.mocks.mock_mcp_server import MockToolProviderClient


def _headers(token: str, org_id: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "X-Organization-Id": str(org_id),
    }


@contextlib.contextmanager
def _patch_provider(mock=None):
    """Patch ConnectionService.construct_client to return an in-process mock."""
    client = mock or MockToolProviderClient()

    async def _construct(self, db, connection, current_user=None, **kwargs):
        return client

    with patch(
        "app.services.connection_service.ConnectionService.construct_client",
        _construct,
    ):
        yield client


def _setup_agent_with_tools(
    *, create_user, login_user, whoami, enable_mcp,
    create_mcp_connection, create_domain_from_connection, test_client,
):
    """Admin + org + MCP connection with discovered tools + public agent."""
    user = create_user()
    token = login_user(user["email"], user["password"])
    info = whoami(token)
    org_id = info["organizations"][0]["id"]
    enable_mcp(user_token=token, org_id=org_id)

    conn = create_mcp_connection(
        name=f"Mock MCP {uuid.uuid4().hex[:6]}",
        server_url="http://mock:3000/mcp",
        user_token=token, org_id=org_id,
    )
    with _patch_provider():
        r = test_client.post(
            f"/api/connections/{conn['id']}/refresh-tools",
            headers=_headers(token, org_id),
        )
        assert r.status_code == 200, r.text

    ds = create_domain_from_connection(
        name=f"Mock Agent {uuid.uuid4().hex[:6]}", connection_id=conn["id"],
        user_token=token, org_id=org_id, is_public=True,
    )
    return {
        "admin_token": token, "admin_user_id": info["id"], "org_id": org_id,
        "conn_id": conn["id"], "ds_id": str(ds["id"]),
    }


def _get_tools(test_client, ds_id, token, org_id):
    r = test_client.get(f"/api/data_sources/{ds_id}/tools", headers=_headers(token, org_id))
    assert r.status_code == 200, r.text
    return {t["name"]: t for t in r.json()}


# ────────────────────────────────────────────────────────────────────────
# Admin layers: only admins may write; values are validated + normalized
# ────────────────────────────────────────────────────────────────────────

@pytest.mark.e2e
def test_admin_tool_policy_write_is_admin_only_and_normalized(
    create_user, login_user, whoami, enable_mcp,
    create_mcp_connection, create_domain_from_connection,
    invite_user_to_org, test_client,
):
    ctx = _setup_agent_with_tools(
        create_user=create_user, login_user=login_user, whoami=whoami,
        enable_mcp=enable_mcp, create_mcp_connection=create_mcp_connection,
        create_domain_from_connection=create_domain_from_connection,
        test_client=test_client,
    )
    admin, org_id, ds_id, conn_id = ctx["admin_token"], ctx["org_id"], ctx["ds_id"], ctx["conn_id"]
    member = invite_user_to_org(org_id=org_id, admin_token=admin)

    tools = _get_tools(test_client, ds_id, admin, org_id)
    echo = tools["echo"]

    # Admin sets the per-agent overlay to 'ask'.
    r = test_client.put(
        f"/api/data_sources/{ds_id}/tools/{echo['id']}",
        json={"policy": "ask"}, headers=_headers(admin, org_id),
    )
    assert r.status_code == 200, r.text
    assert r.json()["policy"] == "ask"
    assert r.json()["effective_policy"] == "ask"

    # Legacy 'confirm' is accepted and normalized to 'ask'.
    r = test_client.put(
        f"/api/data_sources/{ds_id}/tools/{echo['id']}",
        json={"policy": "confirm"}, headers=_headers(admin, org_id),
    )
    assert r.status_code == 200, r.text
    assert r.json()["policy"] == "ask"

    # 'auto' is a valid admin policy.
    r = test_client.put(
        f"/api/data_sources/{ds_id}/tools/{echo['id']}",
        json={"policy": "auto"}, headers=_headers(admin, org_id),
    )
    assert r.status_code == 200, r.text
    assert r.json()["policy"] == "auto"

    # Garbage is rejected at both admin layers.
    r = test_client.put(
        f"/api/data_sources/{ds_id}/tools/{echo['id']}",
        json={"policy": "yolo"}, headers=_headers(admin, org_id),
    )
    assert r.status_code == 400
    r = test_client.put(
        f"/api/connections/{conn_id}/tools/{echo['id']}",
        json={"policy": "yolo"}, headers=_headers(admin, org_id),
    )
    assert r.status_code == 400

    # A regular member can READ the agent's tools (read-only view)...
    member_tools = _get_tools(test_client, ds_id, member["token"], org_id)
    assert "echo" in member_tools

    # ...but cannot write the overlay, reset it, or touch connection defaults.
    r = test_client.put(
        f"/api/data_sources/{ds_id}/tools/{echo['id']}",
        json={"is_enabled": False}, headers=_headers(member["token"], org_id),
    )
    assert r.status_code == 403
    r = test_client.put(
        f"/api/data_sources/{ds_id}/tools/{echo['id']}",
        json={"policy": "allow"}, headers=_headers(member["token"], org_id),
    )
    assert r.status_code == 403
    r = test_client.delete(
        f"/api/data_sources/{ds_id}/tools/{echo['id']}",
        headers=_headers(member["token"], org_id),
    )
    assert r.status_code == 403
    r = test_client.put(
        f"/api/connections/{conn_id}/tools/{echo['id']}",
        json={"policy": "deny"}, headers=_headers(member["token"], org_id),
    )
    assert r.status_code == 403


# ────────────────────────────────────────────────────────────────────────
# Per-user layer: any member manages their own; deny stays absolute
# ────────────────────────────────────────────────────────────────────────

@pytest.mark.e2e
def test_member_sets_own_policy_and_admin_deny_is_absolute(
    create_user, login_user, whoami, enable_mcp,
    create_mcp_connection, create_domain_from_connection,
    invite_user_to_org, test_client,
):
    ctx = _setup_agent_with_tools(
        create_user=create_user, login_user=login_user, whoami=whoami,
        enable_mcp=enable_mcp, create_mcp_connection=create_mcp_connection,
        create_domain_from_connection=create_domain_from_connection,
        test_client=test_client,
    )
    admin, org_id, ds_id = ctx["admin_token"], ctx["org_id"], ctx["ds_id"]
    member = invite_user_to_org(org_id=org_id, admin_token=admin)

    tools = _get_tools(test_client, ds_id, member["token"], org_id)
    echo = tools["echo"]
    assert echo["user_policy"] is None
    assert echo["effective_policy"] == echo["policy"]

    # Member sets their own policy (view permission is enough).
    r = test_client.put(
        f"/api/data_sources/{ds_id}/tools/{echo['id']}/my_policy",
        json={"policy": "ask"}, headers=_headers(member["token"], org_id),
    )
    assert r.status_code == 200, r.text
    assert r.json()["user_policy"] == "ask"
    assert r.json()["effective_policy"] == "ask"
    # The admin policy itself is untouched.
    assert r.json()["policy"] == echo["policy"]

    # Preference is scoped to the member — the admin still sees no user_policy.
    admin_view = _get_tools(test_client, ds_id, admin, org_id)
    assert admin_view["echo"]["user_policy"] is None
    assert admin_view["echo"]["effective_policy"] == admin_view["echo"]["policy"]

    # Member can relax an admin 'ask' to 'allow' for themselves...
    r = test_client.put(
        f"/api/data_sources/{ds_id}/tools/{echo['id']}",
        json={"policy": "ask"}, headers=_headers(admin, org_id),
    )
    assert r.status_code == 200
    r = test_client.put(
        f"/api/data_sources/{ds_id}/tools/{echo['id']}/my_policy",
        json={"policy": "allow"}, headers=_headers(member["token"], org_id),
    )
    assert r.status_code == 200
    assert r.json()["policy"] == "ask"
    assert r.json()["effective_policy"] == "allow"

    # ...but an admin 'deny' is absolute regardless of the user preference.
    r = test_client.put(
        f"/api/data_sources/{ds_id}/tools/{echo['id']}",
        json={"policy": "deny"}, headers=_headers(admin, org_id),
    )
    assert r.status_code == 200
    member_view = _get_tools(test_client, ds_id, member["token"], org_id)
    assert member_view["echo"]["user_policy"] == "allow"
    assert member_view["echo"]["effective_policy"] == "deny"

    # Reset reverts to the admin policy.
    r = test_client.delete(
        f"/api/data_sources/{ds_id}/tools/{echo['id']}/my_policy",
        headers=_headers(member["token"], org_id),
    )
    assert r.status_code == 200
    assert r.json()["user_policy"] is None
    assert r.json()["effective_policy"] == "deny"

    # Garbage user policy is rejected.
    r = test_client.put(
        f"/api/data_sources/{ds_id}/tools/{echo['id']}/my_policy",
        json={"policy": "whatever"}, headers=_headers(member["token"], org_id),
    )
    assert r.status_code == 400


# ────────────────────────────────────────────────────────────────────────
# Gateway enforcement of the user layer (BOW-as-MCP-server path)
# ────────────────────────────────────────────────────────────────────────

@pytest.mark.e2e
def test_gateway_enforces_user_preference(
    create_user, login_user, whoami, create_api_key, enable_mcp,
    create_mcp_connection, create_domain_from_connection, test_client,
):
    ctx = _setup_agent_with_tools(
        create_user=create_user, login_user=login_user, whoami=whoami,
        enable_mcp=enable_mcp, create_mcp_connection=create_mcp_connection,
        create_domain_from_connection=create_domain_from_connection,
        test_client=test_client,
    )
    admin, org_id, ds_id = ctx["admin_token"], ctx["org_id"], ctx["ds_id"]
    api_key = create_api_key(user_token=admin, org_id=org_id)["key"]

    def gateway_call(name, arguments):
        payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                   "params": {"name": name, "arguments": arguments}}
        resp = test_client.post("/api/mcp", json=payload, headers={"X-API-Key": api_key})
        assert resp.status_code == 200, resp.text
        return json.loads(resp.json()["result"]["content"][0]["text"])

    tools = _get_tools(test_client, ds_id, admin, org_id)
    echo = tools["echo"]
    assert echo["policy"] == "allow"

    # Baseline: the call goes through.
    with _patch_provider():
        out = gateway_call("execute_mcp", {
            "data_source_id": ds_id, "tool_name": "echo",
            "arguments": {"message": "hi"},
        })
    assert out["success"] is True

    # The caller's own 'deny' preference blocks the call even though the
    # admin policy is 'allow'.
    r = test_client.put(
        f"/api/data_sources/{ds_id}/tools/{echo['id']}/my_policy",
        json={"policy": "deny"}, headers=_headers(admin, org_id),
    )
    assert r.status_code == 200
    with _patch_provider():
        out = gateway_call("execute_mcp", {
            "data_source_id": ds_id, "tool_name": "echo",
            "arguments": {"message": "hi"},
        })
    assert out["success"] is False
    assert "policy" in out["error_message"].lower()

    # 'auto' fails closed when no LLM model is configured for the judge.
    r = test_client.put(
        f"/api/data_sources/{ds_id}/tools/{echo['id']}/my_policy",
        json={"policy": "auto"}, headers=_headers(admin, org_id),
    )
    assert r.status_code == 200
    with _patch_provider():
        out = gateway_call("execute_mcp", {
            "data_source_id": ds_id, "tool_name": "echo",
            "arguments": {"message": "hi"},
        })
    assert out["success"] is False


# ────────────────────────────────────────────────────────────────────────
# 'ask' confirmation endpoint: authorization + remembered preference
# ────────────────────────────────────────────────────────────────────────

@pytest.mark.e2e
def test_mcp_confirmation_endpoint_authz_and_remember(
    create_user, login_user, whoami, enable_mcp,
    create_mcp_connection, create_domain_from_connection,
    invite_user_to_org, test_client,
):
    from app.ai.tools.confirmation import (
        PENDING_CONFIRMATIONS,
        discard_confirmation,
        register_confirmation,
    )

    ctx = _setup_agent_with_tools(
        create_user=create_user, login_user=login_user, whoami=whoami,
        enable_mcp=enable_mcp, create_mcp_connection=create_mcp_connection,
        create_domain_from_connection=create_domain_from_connection,
        test_client=test_client,
    )
    admin, admin_user_id = ctx["admin_token"], ctx["admin_user_id"]
    org_id, ds_id = ctx["org_id"], ctx["ds_id"]
    member = invite_user_to_org(org_id=org_id, admin_token=admin)

    tools = _get_tools(test_client, ds_id, admin, org_id)
    echo = tools["echo"]
    completion_id = str(uuid.uuid4())
    confirmation_id = str(uuid.uuid4())

    # Register a pending confirmation the way execute_mcp does mid-run.
    loop = asyncio.new_event_loop()
    try:
        future = loop.run_until_complete(_register(
            register_confirmation, confirmation_id, {
                "kind": "mcp_tool_policy",
                "user_id": admin_user_id,
                "connection_tool_id": echo["id"],
                "completion_ids": [completion_id],
                "tool_name": "echo",
            },
        ))

        url = f"/api/completions/{completion_id}/mcp_tool_confirmations/{confirmation_id}"

        # Another user cannot resolve someone else's approval.
        r = test_client.post(url, json={"approved": True, "remember": True},
                             headers=_headers(member["token"], org_id))
        assert r.status_code == 403

        # A mismatched completion id is rejected.
        r = test_client.post(
            f"/api/completions/{uuid.uuid4()}/mcp_tool_confirmations/{confirmation_id}",
            json={"approved": True}, headers=_headers(admin, org_id),
        )
        assert r.status_code == 404

        # The run owner approves with remember → resolves the future AND
        # persists their preference.
        r = test_client.post(url, json={"approved": True, "remember": True},
                             headers=_headers(admin, org_id))
        assert r.status_code == 200, r.text
        assert future.result() == {"approved": True, "remember": True}
        assert confirmation_id not in PENDING_CONFIRMATIONS or PENDING_CONFIRMATIONS[confirmation_id].done()

        admin_view = _get_tools(test_client, ds_id, admin, org_id)
        assert admin_view["echo"]["user_policy"] == "allow"

        # An unknown confirmation id 404s.
        r = test_client.post(
            f"/api/completions/{completion_id}/mcp_tool_confirmations/{uuid.uuid4()}",
            json={"approved": False}, headers=_headers(admin, org_id),
        )
        assert r.status_code == 404
    finally:
        discard_confirmation(confirmation_id)
        loop.close()


async def _register(register_confirmation, confirmation_id, meta):
    return register_confirmation(confirmation_id, meta)


# ────────────────────────────────────────────────────────────────────────
# refresh-tools guard: an empty provider response must not wipe tools
# ────────────────────────────────────────────────────────────────────────

@pytest.mark.e2e
def test_refresh_tools_empty_result_keeps_existing_tools_and_preferences(
    create_user, login_user, whoami, enable_mcp,
    create_mcp_connection, create_domain_from_connection,
    get_connection_tools, test_client,
):
    ctx = _setup_agent_with_tools(
        create_user=create_user, login_user=login_user, whoami=whoami,
        enable_mcp=enable_mcp, create_mcp_connection=create_mcp_connection,
        create_domain_from_connection=create_domain_from_connection,
        test_client=test_client,
    )
    admin, org_id, ds_id, conn_id = ctx["admin_token"], ctx["org_id"], ctx["ds_id"], ctx["conn_id"]

    tools_before = get_connection_tools(connection_id=conn_id, user_token=admin, org_id=org_id)
    assert len(tools_before) > 0
    echo = next(t for t in tools_before if t["name"] == "echo")

    # A user preference exists before the flaky refresh.
    r = test_client.put(
        f"/api/data_sources/{ds_id}/tools/{echo['id']}/my_policy",
        json={"policy": "ask"}, headers=_headers(admin, org_id),
    )
    assert r.status_code == 200

    # The provider comes back empty (flake/misconfiguration) — nothing is deleted.
    empty_provider = MockToolProviderClient()
    empty_provider.set_tools([])
    with _patch_provider(empty_provider):
        r = test_client.post(
            f"/api/connections/{conn_id}/refresh-tools",
            headers=_headers(admin, org_id),
        )
        assert r.status_code == 200, r.text

    tools_after = get_connection_tools(connection_id=conn_id, user_token=admin, org_id=org_id)
    assert {t["name"] for t in tools_after} == {t["name"] for t in tools_before}
    # The preference (and its FK target) survived.
    view = _get_tools(test_client, ds_id, admin, org_id)
    assert view["echo"]["user_policy"] == "ask"
    assert view["echo"]["id"] == echo["id"]
