"""Built-in connector actions (Outlook, Teams) end to end through the API.

Contracts:
  * an Outlook / Teams connection lists its write actions as tools, each with
    an effect, and every one starts switched off;
  * a connection admin turns them on (members cannot); policies the effect
    does not allow are refused (no ``auto`` for external, no ``allow`` for
    destructive);
  * turning an action on adds its scope to the next Microsoft sign-in;
  * in a run, only switched-on actions are offered, a call reaches Microsoft
    exactly once through the agent it goes through, and a switched-off action
    is refused even when called by name;
  * the permissions endpoint names what the caller's token is missing.

The LLM and Microsoft Graph are faked at the boundary (scripted planner,
``FakeM365`` behind httpx.MockTransport); routes, services, the agent loop and
the tool policy run real.
"""
from __future__ import annotations

import contextlib
import uuid
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from tests.e2e.rbac.test_mcp_tools_cap import _Planner, _name
from tests.mocks.fake_microsoft365 import FakeM365, make_token

pytestmark = pytest.mark.e2e

ENTRA_APP = {
    "tenant_id": "11111111-1111-1111-1111-111111111111",
    "client_id": "22222222-2222-2222-2222-222222222222",
    "client_secret": "test-secret",
}


@pytest.fixture
def planner(monkeypatch):
    from app.ai.llm.llm import LLM
    from app.ai.tools.mcp_tool_registry import clear_usage_cache

    clear_usage_cache()
    p = _Planner()

    async def _stream(self, prompt, **kwargs):
        yield "ok"

    monkeypatch.setattr(LLM, "inference_stream_v2", p.stream())
    monkeypatch.setattr(LLM, "inference_stream", _stream)
    monkeypatch.setattr(LLM, "inference", lambda self, prompt, *a, **k: "[]")
    yield p
    clear_usage_cache()


@contextlib.contextmanager
def _graph(scopes, fake=None):
    """Every client the app builds talks to one FakeM365 with a token carrying ``scopes``."""
    fake = fake or FakeM365()
    from app.data_sources.clients.graph_mail_client import GraphMailClient
    from app.data_sources.clients.graph_teams_client import GraphTeamsClient

    async def _construct(self, db, connection, current_user=None, **kwargs):
        cls = GraphTeamsClient if connection.type == "teams_messages" else GraphMailClient
        import json
        cfg = json.loads(connection.config) if isinstance(connection.config, str) else dict(connection.config or {})
        extra = {"allowed_post_channels": cfg.get("allowed_post_channels")} if connection.type == "teams_messages" else {}
        c = cls(access_token=make_token(scopes), **extra)
        c._user_token_provided = True
        c._http = httpx.Client(transport=httpx.MockTransport(fake))
        return c

    with patch("app.services.connection_service.ConnectionService.construct_client", _construct):
        yield fake


@pytest.fixture
def org(test_client, bootstrap_admin, invite_user_to_org, create_llm_provider_and_models, enable_mcp, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY_TEST", "dummy-local-provider-boundary")
    admin = bootstrap_admin("actions")
    token, org_id = admin["token"], admin["org_id"]
    create_llm_provider_and_models(user_token=token, org_id=org_id)
    enable_mcp(user_token=token, org_id=org_id)
    h = {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}
    member = invite_user_to_org(org_id=org_id, admin_token=token)
    return {"admin": admin, "member": member, "org_id": org_id, "h": h,
            "mh": {"Authorization": f"Bearer {member['token']}", "X-Organization-Id": str(org_id)}}


def _connector(test_client, h, conn_type, config=None):
    name = f"{conn_type}-{uuid.uuid4().hex[:4]}"
    conn = test_client.post("/api/connections", headers=h, json={
        "name": name, "type": conn_type, "credentials": ENTRA_APP, "config": config or {},
        "auth_policy": "user_required", "allowed_user_auth_modes": ["oauth"],
    })
    assert conn.status_code == 200, conn.text
    ds = test_client.post("/api/data_sources", headers=h, json={
        "name": f"{name} agent", "type": conn_type, "connection_ids": [conn.json()["id"]],
        "is_public": True, "auth_policy": "user_required", "allowed_user_auth_modes": ["oauth"],
    })
    assert ds.status_code == 200, ds.text
    tools = {t["name"]: t for t in test_client.get(f"/api/connections/{conn.json()['id']}/tools", headers=h).json()}
    return {"conn_id": conn.json()["id"], "name": name, "ds_id": ds.json()["id"], "tools": tools}


def _set_tool(test_client, h, c, tool, **body):
    return test_client.put(f"/api/connections/{c['conn_id']}/tools/{c['tools'][tool]['id']}", headers=h, json=body)


def _run(test_client, h, ds_ids, planner, queue):
    planner.queue = list(queue)
    r = test_client.post("/api/reports", headers=h, json={"title": "act", "files": [], "data_sources": ds_ids})
    assert r.status_code == 200, r.text
    r = test_client.post(f"/api/reports/{r.json()['id']}/completions", headers=h,
                         params={"background": False}, json={"prompt": {"content": "Do it."}})
    assert r.status_code == 200, r.text


@pytest.mark.parametrize("conn_type, expected", [
    ("outlook_mail", {"flag_email": "modify", "move_email": "modify", "create_draft": "modify", "send_email": "external"}),
    ("teams_messages", {"post_message": "external", "send_chat_message": "external"}),
])
def test_a_connector_lists_its_actions_switched_off_with_their_effect(test_client, org, conn_type, expected):
    c = _connector(test_client, org["h"], conn_type)
    native = {n: t for n, t in c["tools"].items() if t["source"] == "native"}
    assert {n: t["effect"] for n, t in native.items()} == expected
    assert all(t["is_enabled"] is False for t in native.values())
    # Listing again does not duplicate or reset anything.
    assert _set_tool(test_client, org["h"], c, next(iter(expected)), is_enabled=True).status_code == 200
    again = test_client.get(f"/api/connections/{c['conn_id']}/tools", headers=org["h"]).json()
    assert len([t for t in again if t["source"] == "native"]) == len(expected)
    assert next(t for t in again if t["name"] == next(iter(expected)))["is_enabled"] is True


def test_members_cannot_turn_actions_on(test_client, org):
    c = _connector(test_client, org["h"], "outlook_mail")
    r = _set_tool(test_client, org["mh"], c, "send_email", is_enabled=True)
    assert r.status_code in (403, 404), r.text


@pytest.mark.parametrize("tool, policy", [("send_email", "auto")])
def test_a_policy_the_effect_does_not_allow_is_refused(test_client, org, tool, policy):
    c = _connector(test_client, org["h"], "outlook_mail")
    assert _set_tool(test_client, org["h"], c, tool, policy=policy).status_code == 400
    r = test_client.put(f"/api/data_sources/{c['ds_id']}/tools/{c['tools'][tool]['id']}", headers=org["h"],
                        json={"policy": policy})
    assert r.status_code == 400, r.text
    # The same policy is fine on a modify action.
    assert _set_tool(test_client, org["h"], c, "flag_email", policy="auto").status_code == 200


def test_turning_an_action_on_adds_its_scope_to_the_microsoft_sign_in(test_client, org):
    c = _connector(test_client, org["h"], "outlook_mail")

    def scopes():
        r = test_client.get(f"/api/connections/{c['conn_id']}/oauth/authorize", headers=org["h"])
        assert r.status_code == 200, r.text
        return set(parse_qs(urlparse(r.json()["authorization_url"]).query)["scope"][0].split())

    assert "Mail.Send" not in scopes() and "Mail.ReadWrite" not in scopes()
    assert _set_tool(test_client, org["h"], c, "send_email", is_enabled=True).status_code == 200
    assert {"Mail.Read", "Mail.Send"} <= scopes() and "Mail.ReadWrite" not in scopes()
    assert _set_tool(test_client, org["h"], c, "move_email", is_enabled=True).status_code == 200
    assert {"Mail.Send", "Mail.ReadWrite"} <= scopes()
    # Saving the connection's settings keeps them, and a client cannot set them.
    r = test_client.put(f"/api/connections/{c['conn_id']}", headers=org["h"],
                        json={"config": {"action_scopes": ["Mail.ReadWrite.All", "Files.ReadWrite.All"]}})
    assert r.status_code == 200, r.text
    assert {"Mail.Send", "Mail.ReadWrite"} <= scopes() and "Files.ReadWrite.All" not in scopes()
    assert _set_tool(test_client, org["h"], c, "send_email", is_enabled=False).status_code == 200
    assert "Mail.Send" not in scopes()


def test_only_switched_on_actions_are_offered_and_a_call_reaches_microsoft_once(test_client, org, planner):
    c = _connector(test_client, org["h"], "outlook_mail")
    assert _set_tool(test_client, org["h"], c, "flag_email", is_enabled=True, policy="allow").status_code == 200
    with _graph(["Mail.Read", "Mail.ReadWrite"]) as fake:
        mid = next(m["id"] for m in fake.messages.values() if m["parentFolderId"] == "fld-inbox")
        _run(test_client, org["h"], [c["ds_id"]], planner, [
            (_name(c["name"], "flag_email"), {"message_id": mid, "flag": "flagged"}),
            ("execute_mcp", {"connection_id": c["conn_id"], "tool_name": "move_email",
                             "arguments": {"message_id": mid, "destination": "archive"}, "title": "Move"}),
        ])
    offered = planner.mcp_tools(0)
    assert _name(c["name"], "flag_email") in offered
    assert not {_name(c["name"], t) for t in ("move_email", "create_draft", "send_email")} & set(offered), offered
    assert fake.writes == [{"op": "update", "id": mid, "changes": {"flag": {"flagStatus": "flagged"}}}]
    assert "disabled" in planner.results[-1].lower(), planner.results[-1]


def test_a_teams_post_follows_the_connections_allowed_channels(test_client, org, planner):
    c = _connector(test_client, org["h"], "teams_messages", config={"allowed_post_channels": "Alerts"})
    assert _set_tool(test_client, org["h"], c, "post_message", is_enabled=True, policy="allow").status_code == 200
    with _graph(["ChannelMessage.Send", "Chat.Read"]) as fake:
        _run(test_client, org["h"], [c["ds_id"]], planner, [
            (_name(c["name"], "post_message"), {"channel": "Alerts", "text": "3 detections closed"}),
            (_name(c["name"], "post_message"), {"channel": "General", "text": "should not post"}),
        ])
    assert [(w["op"], w["channel"]) for w in fake.writes] == [("channel_post", "Alerts")]
    assert "not allowed" in planner.results[-1], planner.results[-1]


def test_the_permissions_endpoint_names_missing_scopes(test_client, org):
    c = _connector(test_client, org["h"], "outlook_mail")
    with _graph(["Mail.Read", "Mail.ReadWrite"]):
        r = test_client.get(f"/api/connections/{c['conn_id']}/tools/permissions", headers=org["h"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["known"] is True
    assert body["missing"]["send_email"] == ["Mail.Send"] and body["missing"]["flag_email"] == []
