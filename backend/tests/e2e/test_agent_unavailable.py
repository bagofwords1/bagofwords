"""Agents and connectors a run cannot load, end to end through real runs.

Contract (continues test_agent_signin_required.py):
  * An agent whose every connection is flagged unreachable is listed to the
    planner in <agents_unreachable> and nowhere else: the planner knows it
    exists and is down, and never sees a schema it could try to query. An
    agent with a healthy connection is never listed.
  * A person's own connector (mail, Teams, per-user MCP) they have not signed
    in to is listed in <agents_needing_signin> like any agent, and
    request_agent_signin shows its Connect card.
  * A folder-scoped conversation whose agents all need this user's sign-in
    runs and lists them for sign-in, instead of failing the request.
  * The MCP get_context tool reports agents it leaves out, with the reason.

The LLM boundary is stubbed (records prompts, scripts the planner's tool
calls); routes, services, context building, the tools and the agent loop run
real.
"""
from __future__ import annotations

import asyncio
import json
import re
import uuid

import pytest

from tests.e2e.rbac.conftest import invite_user_to_org  # noqa: F401 (fixture)
from tests.e2e.test_agent_signin_required import (  # noqa: F401 (shared helpers + fixtures)
    _BLOCK, _ask, _member, _signin_blocks, _source, _tool_executions, env, llm,
)

pytestmark = pytest.mark.e2e

_UNREACHABLE = re.compile(r"<agents_unreachable[^>]*>(.*?)</agents_unreachable>", re.S)


def _unreachable_blocks(prompt: str) -> list[str]:
    return [b for b in _UNREACHABLE.findall(prompt) if "<agent " in b]


def _mark_connections(data_source_id: str, *, reachable: bool) -> None:
    """Set the cached health flag on an agent's connections.

    Written directly: the API only clears Connection.is_active after repeated
    failed probes of a real server, which a test cannot stage."""
    from sqlalchemy import select, update

    from app.dependencies import async_session_maker
    from app.models.connection import Connection
    from app.models.domain_connection import domain_connection

    async def _run():
        async with async_session_maker() as db:
            ids = (await db.execute(
                select(domain_connection.c.connection_id)
                .where(domain_connection.c.data_source_id == data_source_id)
            )).scalars().all()
            assert ids, "the agent has no connection to mark"
            await db.execute(update(Connection).where(Connection.id.in_(ids)).values(is_active=reachable))
            await db.commit()

    asyncio.run(_run())


@pytest.fixture
def shared_pair(test_client, env, create_data_source, refresh_schema, bulk_update_tables):
    down = _source(test_client, env, create_data_source, refresh_schema, bulk_update_tables,
                   name=f"Sales {uuid.uuid4().hex[:4]}", auth_policy="system_only")
    up = _source(test_client, env, create_data_source, refresh_schema, bulk_update_tables,
                 name=f"Inventory {uuid.uuid4().hex[:4]}", auth_policy="system_only")
    return down, up


@pytest.mark.parametrize("as_member", [True, False], ids=["member", "admin"])
def test_unreachable_agent_is_listed_as_unreachable_not_as_schema(
    test_client, env, llm, shared_pair, create_user, login_user, as_member,
):
    down, up = shared_pair
    headers = _member(test_client, create_user, login_user, env) if as_member else env["headers"]

    _, before = _ask(test_client, headers, llm, "What were sales last quarter?", script=[])
    assert not _unreachable_blocks(before[0]), "a healthy agent is never listed as unreachable"
    assert down["name"] in before[0]

    _mark_connections(down["id"], reachable=False)
    _, prompts = _ask(test_client, headers, llm, "What were sales last quarter?", script=[])

    blocks = _unreachable_blocks(prompts[0])
    assert len(blocks) == 1, "planner must be told which agents are unreachable"
    assert down["id"] in blocks[0]
    assert up["id"] not in blocks[0]
    outside = _UNREACHABLE.sub("", prompts[0])
    assert down["id"] not in outside and down["name"] not in outside, "no schema or roster line for it"
    assert up["name"] in outside, "the reachable agent keeps its normal context"
    assert not any(_signin_blocks(p) for p in prompts), "being down is not a missing sign-in"

    _mark_connections(down["id"], reachable=True)
    _, after = _ask(test_client, headers, llm, "What were sales last quarter?", script=[])
    assert not _unreachable_blocks(after[0]) and down["name"] in after[0], "it returns once reachable"


def test_only_agent_unreachable_is_still_reported(test_client, env, llm, create_data_source, refresh_schema,
                                                  bulk_update_tables):
    # With no other agent the run has no client at all, which is where the
    # agent used to stay in context with nothing behind it.
    only = _source(test_client, env, create_data_source, refresh_schema, bulk_update_tables,
                   name=f"Sales {uuid.uuid4().hex[:4]}", auth_policy="system_only")
    _mark_connections(only["id"], reachable=False)

    _, prompts = _ask(test_client, env["headers"], llm, "What were sales last quarter?", script=[])

    blocks = _unreachable_blocks(prompts[0])
    assert len(blocks) == 1 and only["id"] in blocks[0]
    assert only["name"] not in _UNREACHABLE.sub("", prompts[0])


@pytest.fixture
def connector(test_client, env, create_data_source, refresh_schema, bulk_update_tables):
    ds = _source(test_client, env, create_data_source, refresh_schema, bulk_update_tables,
                 name=f"Mailbox {uuid.uuid4().hex[:4]}", auth_policy="user_required")
    r = test_client.put(f"/api/data_sources/{ds['id']}", json={"kind": "connector"}, headers=env["headers"])
    assert r.status_code == 200, r.text
    return ds


def test_unsigned_connector_is_listed_for_signin(test_client, env, llm, connector, create_data_source,
                                                 refresh_schema, bulk_update_tables, create_user, login_user):
    shared = _source(test_client, env, create_data_source, refresh_schema, bulk_update_tables,
                     name=f"Inventory {uuid.uuid4().hex[:4]}", auth_policy="system_only")
    member = _member(test_client, create_user, login_user, env)
    message = f"Connect your mailbox to answer this. {uuid.uuid4().hex[:6]}"

    rid, prompts = _ask(test_client, member, llm, "Summarise my unread mail", script=[
        ("request_agent_signin", {"agent_ids": [connector["id"]], "message": message}),
    ])

    blocks = _signin_blocks(prompts[0])
    assert len(blocks) == 1 and connector["id"] in blocks[0], "an unconnected connector is listed for sign-in"
    assert shared["id"] not in blocks[0]
    assert connector["name"] not in _BLOCK.sub("", prompts[0]), "its tools and content stay out of the run"

    assert len(prompts) == 1, "the sign-in request ends the turn"
    result = _tool_executions(test_client, member, rid, "request_agent_signin")[0][1].get("result_json") or {}
    assert result.get("success") is True
    assert [a["id"] for a in result.get("agents", [])] == [connector["id"]]


def test_pinned_agent_still_offers_the_users_connector(test_client, env, llm, connector, create_data_source,
                                                       refresh_schema, bulk_update_tables, create_user, login_user):
    # A manual selection is a hard scope for agents, but a person's own
    # connectors join every run, so the Connect card must be accepted there.
    shared = _source(test_client, env, create_data_source, refresh_schema, bulk_update_tables,
                     name=f"Inventory {uuid.uuid4().hex[:4]}", auth_policy="system_only")
    member = _member(test_client, create_user, login_user, env)
    llm["planner"].clear()
    llm["base"] = None
    llm["script"] = [("request_agent_signin", {"agent_ids": [connector["id"]], "message": "Connect it."})]
    r = test_client.post("/api/reports", headers=member,
                         json={"title": "pinned", "files": [], "data_sources": [shared["id"]]})
    assert r.status_code == 200, r.text
    rid = r.json()["id"]
    r = test_client.post(f"/api/reports/{rid}/completions", headers=member,
                         json={"prompt": {"content": "Summarise my unread mail"}})
    assert r.status_code == 200, r.text

    assert connector["id"] in "".join(_signin_blocks(llm["planner"][0]))
    result = _tool_executions(test_client, member, rid, "request_agent_signin")[0][1].get("result_json") or {}
    assert result.get("success") is True and [a["id"] for a in result["agents"]] == [connector["id"]]


def _folder_with(test_client, env, agent_ids, member_user_id):
    headers, org = env["headers"], env["org_id"]

    def api(method, path, **payload):
        r = test_client.request(method, "/api/" + path, headers=headers, json=payload)
        assert r.is_success, r.text
        return r.json()

    folder = api("POST", "folders", name=f"Team {uuid.uuid4().hex[:4]}")["id"]
    for aid in agent_ids:
        api("PUT", f"folders/items/agent/{aid}/placement", parent_id=folder)
    role = api("POST", f"organizations/{org}/roles", name=f"Readers {uuid.uuid4().hex[:4]}",
               resource_grants=[{"resource_type": "folder", "resource_id": folder, "permissions": ["view"]}])
    api("POST", f"organizations/{org}/role-assignments", role_id=role["id"],
        principal_type="user", principal_id=member_user_id)
    return folder


def test_folder_whose_agents_need_signin_runs_and_asks_for_it(test_client, env, llm, create_data_source,
                                                             refresh_schema, bulk_update_tables,
                                                             invite_user_to_org):
    per_user = _source(test_client, env, create_data_source, refresh_schema, bulk_update_tables,
                       name=f"Sales {uuid.uuid4().hex[:4]}", auth_policy="user_required")
    invited = invite_user_to_org(org_id=env["org_id"], admin_token=env["token"])
    member = {**env["headers"], "Authorization": "Bearer " + invited["token"]}
    folder = _folder_with(test_client, env, [per_user["id"]], invited["user_id"])
    message = f"Connect Sales to answer this. {uuid.uuid4().hex[:6]}"

    llm["planner"].clear()
    llm["base"] = None
    llm["script"] = [("request_agent_signin", {"agent_ids": [per_user["id"]], "message": message})]
    r = test_client.post("/api/reports", headers=member, json={"agent_folder_id": folder})
    assert r.is_success, r.text
    rid = r.json()["id"]
    r = test_client.post(f"/api/reports/{rid}/completions", headers=member,
                         json={"prompt": {"content": "What were sales last quarter?"}})
    assert r.status_code == 200, "a folder whose agents need sign-in must not fail the request: " + r.text

    blocks = _signin_blocks(llm["planner"][0])
    assert len(blocks) == 1 and per_user["id"] in blocks[0]
    assert per_user["name"] not in _BLOCK.sub("", llm["planner"][0])
    result = _tool_executions(test_client, member, rid, "request_agent_signin")[0][1].get("result_json") or {}
    assert result.get("success") is True and [a["id"] for a in result["agents"]] == [per_user["id"]]


def test_mcp_get_context_reports_agents_it_leaves_out(test_client, env, enable_mcp, create_api_key,
                                                      create_data_source, refresh_schema, bulk_update_tables,
                                                      create_user, login_user):
    per_user = _source(test_client, env, create_data_source, refresh_schema, bulk_update_tables,
                       name=f"Sales {uuid.uuid4().hex[:4]}", auth_policy="user_required")
    down = _source(test_client, env, create_data_source, refresh_schema, bulk_update_tables,
                   name=f"Ledger {uuid.uuid4().hex[:4]}", auth_policy="system_only")
    up = _source(test_client, env, create_data_source, refresh_schema, bulk_update_tables,
                 name=f"Inventory {uuid.uuid4().hex[:4]}", auth_policy="system_only")
    _mark_connections(down["id"], reachable=False)
    enable_mcp(user_token=env["token"], org_id=env["org_id"])
    org = env["org_id"]

    # A second admin, who reaches the per-user agent through the system
    # fallback and so can pin it on a conversation of their own.
    email = f"ctx-{uuid.uuid4().hex[:8]}@example.com"
    r = test_client.post(f"/api/organizations/{org}/members", headers=env["headers"],
                         json={"organization_id": org, "email": email, "role": "admin"})
    assert r.status_code == 200, r.text
    membership_id = r.json()["id"]
    user = create_user(email=email)
    token = login_user(user["email"], user["password"])
    headers = {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org)}
    key = create_api_key(user_token=token, org_id=org)["key"]

    def call(name, arguments):
        r = test_client.post("/api/mcp", headers={"X-API-Key": key}, json={
            "jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}})
        assert r.status_code == 200, r.text
        return r.json()["result"]

    def context():
        body = call("get_context", {"report_id": rid})
        assert body["isError"] is False, body
        return json.loads(body["content"][0]["text"])

    r = test_client.post("/api/reports", headers=headers,
                         json={"title": "ctx", "files": [], "data_sources": [per_user["id"], down["id"], up["id"]]})
    assert r.status_code == 200, r.text
    rid = r.json()["id"]

    # While they can use the per-user agent, only the connection that is
    # really down is reported.
    assert {a["id"]: a["reason"] for a in context()["unavailable_agents"]} == {down["id"]: "unreachable"}

    # Without the fallback the same conversation needs their own sign-in.
    r = test_client.put(f"/api/organizations/{org}/members/{membership_id}", headers=env["headers"],
                        json={"role": "member"})
    assert r.status_code == 200, r.text
    ctx = context()
    reasons = {a["id"]: a["reason"] for a in ctx["unavailable_agents"]}
    assert reasons == {per_user["id"]: "sign_in_required", down["id"]: "unreachable"}
    assert all(a["action"] for a in ctx["unavailable_agents"])
    assert per_user["id"] not in {d["id"] for d in ctx["data_sources"]}

    # Picking the agent from the MCP client says what is missing, where it
    # used to answer as if the agent did not exist.
    refused = call("create_report", {"title": "ctx", "data_source_ids": [per_user["id"]]})
    assert refused["isError"] is True
    assert per_user["name"] in refused["content"][0]["text"]
    missing = call("create_report", {"title": "ctx", "data_source_ids": [str(uuid.uuid4())]})
    assert missing["isError"] is True and per_user["name"] not in missing["content"][0]["text"]
