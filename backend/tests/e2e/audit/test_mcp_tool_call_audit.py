"""Every MCP tools/call is in the audit log (docs/design/audit-event-contract.md).

Contract: a call through /api/mcp leaves one ``tool.mcp_server_call`` event
naming the tool, the outcome (success / failure / denied) and the transport,
with the caller as actor. The event never carries the call's arguments. The
event is queued in the background, so the test drains the queue on the app's
own event loop before reading the rows back through the audit API.
"""
import json
import uuid

import pytest

from tests.e2e.audit.conftest import h

pytestmark = pytest.mark.e2e

BASE = "/api/enterprise/audit"
ACTION = "tool.mcp_server_call"


@pytest.fixture
def live_client():
    """One event loop for the test, so the audit worker outlives each request."""
    from fastapi.testclient import TestClient

    from main import app

    with TestClient(app) as client:
        yield client


def _drain(client):
    from app.core import fire_and_forget
    from app.ee.audit.tool_audit import drain_tool_audit_queue

    async def _run():
        import asyncio

        pending = [t for t in fire_and_forget._pending_tasks if not t.done()]
        if pending:
            await asyncio.wait(pending, timeout=30)
        await drain_tool_audit_queue(timeout=30)

    client.portal.call(_run)


def _mcp_caller(client, api_key, *, sse=False):
    headers = {"X-API-Key": api_key}
    if sse:
        headers["Accept"] = "application/json, text/event-stream"

    def call(name, arguments):
        payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                   "params": {"name": name, "arguments": arguments}}
        r = client.post("/api/mcp", json=payload, headers=headers)
        assert r.status_code == 200, r.text
        if sse:
            data = [line[len("data: "):] for line in r.text.splitlines() if line.startswith("data: ")]
            assert data, r.text
            return json.loads(data[-1])["result"]
        return r.json()["result"]

    return call


def _calls(client, who, tool):
    r = client.get(BASE, params={"action": ACTION, "page_size": 100}, headers=h(who["token"], who["org_id"]))
    assert r.status_code == 200, r.text
    return [row for row in r.json()["items"] if (row.get("details") or {}).get("tool") == f"mcp.{tool}"]


def _mcp_user(who, create_api_key, enable_mcp):
    enable_mcp(user_token=who["token"], org_id=who["org_id"])
    return create_api_key(user_token=who["token"], org_id=who["org_id"])["key"]


def test_successful_call_is_audited_without_its_arguments(
    live_client, bootstrap_admin, create_api_key, enable_mcp,
):
    admin = bootstrap_admin()
    call = _mcp_caller(live_client, _mcp_user(admin, create_api_key, enable_mcp))
    marker = f"title-{uuid.uuid4().hex}"

    result = call("create_report", {"title": marker})
    report_id = json.loads(result["content"][0]["text"])["report_id"]
    _drain(live_client)

    rows = _calls(live_client, admin, "create_report")
    assert len(rows) == 1, rows
    row = rows[0]
    assert row["outcome"] == "success"
    assert row["user_id"] == admin["user_id"]
    assert row["actor_snapshot"]["email"] == admin["email"]
    assert row["resource_id"] == report_id
    assert row["details"]["transport"] == "json"
    assert marker not in json.dumps(row), "the call's arguments must not reach the audit log"


def test_failed_call_over_sse_is_audited_as_a_failure(
    live_client, bootstrap_admin, create_api_key, enable_mcp,
):
    admin = bootstrap_admin()
    call = _mcp_caller(live_client, _mcp_user(admin, create_api_key, enable_mcp), sse=True)
    missing = f"no-such-entity-{uuid.uuid4().hex}"

    result = call("describe_entity", {"name_or_id": missing})
    assert json.loads(result["content"][0]["text"])["success"] is False
    _drain(live_client)

    rows = _calls(live_client, admin, "describe_entity")
    assert len(rows) == 1, rows
    assert rows[0]["outcome"] == "failure"
    assert rows[0]["details"]["transport"] == "sse"
    assert missing not in json.dumps(rows[0])


def test_call_refused_for_a_member_without_the_permission_is_audited_as_denied(
    live_client, bootstrap_admin, invite_user_to_org, create_api_key, enable_mcp,
):
    admin = bootstrap_admin()
    enable_mcp(user_token=admin["token"], org_id=admin["org_id"])
    member = invite_user_to_org(org_id=admin["org_id"], admin_token=admin["token"])
    member_key = create_api_key(user_token=member["token"], org_id=admin["org_id"])["key"]
    call = _mcp_caller(live_client, member_key)

    result = call("create_instruction", {"text": f"rule-{uuid.uuid4().hex}"})
    assert result["isError"] is True
    _drain(live_client)

    rows = _calls(live_client, admin, "create_instruction")
    assert len(rows) == 1, rows
    assert rows[0]["outcome"] == "denied"
    assert rows[0]["user_id"] == member["user_id"]
    assert rows[0]["details"]["error_type"] == "permission_denied"


def test_execute_mcp_records_the_external_tool_it_ran(
    live_client, create_user, login_user, whoami, create_api_key, enable_mcp,
    create_mcp_connection, create_domain_from_connection,
):
    """The MCP server's execute_mcp leaves the same tool.mcp_executed event as
    the in-app agent's, beside the generic call event."""
    from tests.e2e.test_mcp_agent_tools import _full_setup, _patch_provider, _tool_call

    ctx = _full_setup(
        create_user=create_user, login_user=login_user, whoami=whoami,
        create_api_key=create_api_key, enable_mcp=enable_mcp,
        create_mcp_connection=create_mcp_connection,
        create_domain_from_connection=create_domain_from_connection,
        test_client=live_client,
    )
    marker = f"msg-{uuid.uuid4().hex}"
    with _patch_provider(ctx["client"]):
        out, is_err = _tool_call(ctx["call"], "execute_mcp", {
            "data_source_id": ctx["ds_id"], "tool_name": "echo", "arguments": {"message": marker},
        })
    assert is_err is False and out["success"] is True
    _drain(live_client)

    who = {"token": ctx["token"], "org_id": ctx["org_id"]}
    r = live_client.get(BASE, params={"action": "tool.mcp_executed", "page_size": 100},
                        headers=h(who["token"], who["org_id"]))
    assert r.status_code == 200, r.text
    executed = [row for row in r.json()["items"] if (row.get("details") or {}).get("tool") == "mcp.execute_mcp"]
    assert len(executed) == 1, executed
    assert executed[0]["outcome"] == "success"
    assert executed[0]["resource_id"] == ctx["ds_id"]
    assert executed[0]["details"]["tool_name"] == "echo"
    assert marker not in json.dumps(executed[0])
    assert len(_calls(live_client, who, "execute_mcp")) == 1


def test_describe_entity_run_is_audited_as_a_data_read(
    live_client, bootstrap_admin, create_api_key, enable_mcp,
):
    """Running a saved query through MCP is a data read, recorded like an
    in-app query: which entity, how many rows, which parameters by name."""
    from tests.e2e.test_describe_entity_params import _create_entity

    admin = bootstrap_admin()
    call = _mcp_caller(live_client, _mcp_user(admin, create_api_key, enable_mcp))
    entity = _create_entity(live_client, admin)

    result = call("describe_entity", {"name_or_id": entity["id"], "params": {"year": 2022}})
    assert json.loads(result["content"][0]["text"])["success"] is True
    _drain(live_client)

    r = live_client.get(BASE, params={"action": "tool.data_queried", "page_size": 100},
                        headers=h(admin["token"], admin["org_id"]))
    assert r.status_code == 200, r.text
    reads = [row for row in r.json()["items"] if (row.get("details") or {}).get("tool") == "mcp.describe_entity"]
    assert len(reads) == 1, reads
    assert reads[0]["resource_id"] == entity["id"]
    assert reads[0]["details"]["parameter_names"] == ["year"]
    assert reads[0]["details"]["row_count"] == 1
