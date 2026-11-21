"""POST /api/tools/session_summary: does a chat session have tools that change
something? The prompt box shows the tool-permission chip only when it does.

The contract: ``has_non_read_tools`` is true exactly when the run would be
offered at least one connection tool that is not read-only — with a report or
without one (home page), pinned or Auto — and false once every such tool is
switched off for the agent. A report the caller can't see is a 404.
"""

import contextlib
import uuid

import pytest
from unittest.mock import patch

from tests.mocks.mock_mcp_server import MockToolProviderClient

READ_TOOL = {
    "name": "list_items",
    "description": "Lists items.",
    "input_schema": {"type": "object", "properties": {}},
    "output_schema": {},
    "metadata": {"annotations": {"readOnlyHint": True}},
}
WRITE_TOOL = {
    "name": "delete_item",
    "description": "Deletes an item.",
    "input_schema": {"type": "object", "properties": {"id": {"type": "string"}}},
    "output_schema": {},
    "metadata": {"annotations": {"readOnlyHint": False, "destructiveHint": True}},
}


@contextlib.contextmanager
def _provider(tools):
    client = MockToolProviderClient(tools=tools)

    async def _construct(self, db, connection, current_user=None, **kwargs):
        return client

    with patch("app.services.connection_service.ConnectionService.construct_client", _construct):
        yield client


def _headers(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


def _agent_with_tools(test_client, create_mcp_connection, create_domain_from_connection, token, org_id, tools):
    conn = create_mcp_connection(
        name=f"MCP {uuid.uuid4().hex[:6]}", server_url="http://mock:3000/mcp",
        user_token=token, org_id=org_id,
    )
    with _provider(tools):
        r = test_client.post(f"/api/connections/{conn['id']}/refresh-tools", headers=_headers(token, org_id))
        assert r.status_code == 200, r.text
    ds = create_domain_from_connection(
        name=f"Agent {uuid.uuid4().hex[:6]}", connection_id=conn["id"],
        user_token=token, org_id=org_id, is_public=True,
    )
    return str(ds["id"])


def _summary(test_client, token, org_id, **body):
    return test_client.post("/api/tools/session_summary", json=body, headers=_headers(token, org_id))


def _report(test_client, token, org_id, ds_ids):
    r = test_client.post(
        "/api/reports", json={"title": "Chip test", "data_sources": ds_ids}, headers=_headers(token, org_id),
    )
    assert r.status_code == 200, r.text
    return r.json()["id"]


@pytest.mark.e2e
def test_read_only_tools_do_not_count(
    create_user, login_user, whoami, create_mcp_connection, create_domain_from_connection, test_client,
):
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    ds_id = _agent_with_tools(test_client, create_mcp_connection, create_domain_from_connection, token, org_id, [READ_TOOL])

    pinned = _summary(test_client, token, org_id, data_source_ids=[ds_id])
    assert pinned.status_code == 200, pinned.text
    assert pinned.json()["has_non_read_tools"] is False

    report_id = _report(test_client, token, org_id, [ds_id])
    in_report = _summary(test_client, token, org_id, report_id=report_id)
    assert in_report.status_code == 200, in_report.text
    assert in_report.json()["has_non_read_tools"] is False


@pytest.mark.e2e
def test_an_active_write_tool_counts_until_it_is_switched_off(
    create_user, login_user, whoami, create_mcp_connection, create_domain_from_connection, test_client,
):
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    ds_id = _agent_with_tools(
        test_client, create_mcp_connection, create_domain_from_connection, token, org_id, [READ_TOOL, WRITE_TOOL],
    )
    report_id = _report(test_client, token, org_id, [ds_id])

    # Every way the prompt box can ask: in the report, pinned without a report,
    # and Auto (no selection) without a report.
    for body in ({"report_id": report_id}, {"data_source_ids": [ds_id]}, {}):
        r = _summary(test_client, token, org_id, **body)
        assert r.status_code == 200, r.text
        assert r.json()["has_non_read_tools"] is True, body

    tools = test_client.get(f"/api/data_sources/{ds_id}/tools", headers=_headers(token, org_id))
    assert tools.status_code == 200, tools.text
    write_id = next(t["id"] for t in tools.json() if t["name"] == WRITE_TOOL["name"])
    off = test_client.put(
        f"/api/data_sources/{ds_id}/tools/{write_id}", json={"is_enabled": False}, headers=_headers(token, org_id),
    )
    assert off.status_code == 200, off.text

    for body in ({"report_id": report_id}, {"data_source_ids": [ds_id]}):
        r = _summary(test_client, token, org_id, **body)
        assert r.json()["has_non_read_tools"] is False, body


@pytest.mark.e2e
def test_a_denied_write_tool_does_not_count(
    create_user, login_user, whoami, create_mcp_connection, create_domain_from_connection, test_client,
):
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    ds_id = _agent_with_tools(
        test_client, create_mcp_connection, create_domain_from_connection, token, org_id, [WRITE_TOOL],
    )
    tools = test_client.get(f"/api/data_sources/{ds_id}/tools", headers=_headers(token, org_id)).json()
    r = test_client.put(
        f"/api/data_sources/{ds_id}/tools/{tools[0]['id']}", json={"policy": "deny"}, headers=_headers(token, org_id),
    )
    assert r.status_code == 200, r.text
    assert _summary(test_client, token, org_id, data_source_ids=[ds_id]).json()["has_non_read_tools"] is False
