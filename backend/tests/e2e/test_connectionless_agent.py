"""Agents with no data connections, end to end.

Contract:
  * An agent can be created with no connection at all (instructions, files and
    a description only). Only a half-configured *new* connection (a type
    without a config, or a config without a type) is still rejected, and the
    org-level create permission still gates it.
  * Such an agent is a first-class participant in a run: pinned alone, pinned
    next to a data agent, or picked up by Auto, the completion succeeds and the
    planner is told the agent exists — with its description — and that it has
    no data to query. It never takes a data agent down with it.
  * The agent's own endpoints (detail, list, schema, refresh, test, mentions,
    onboarding sync, connections) answer without a server error.
  * Losing connections never deletes an agent: unlinking its last connection is
    allowed, and deleting a connection keeps every agent that used it — along
    with the agent's instructions — and leaves it connectionless.

The LLM boundary is stubbed (records prompts, scripts the planner's tool
calls); routes, services, context building and the agent loop run real.

Run:
    cd backend
    TESTING=true uv run pytest tests/e2e/test_connectionless_agent.py -v
"""
from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from tests.e2e.test_smart_instructions_planner_prompt import env, llm  # noqa: F401 (shared planner fixtures)

pytestmark = pytest.mark.e2e

_CHINOOK = (Path(__file__).resolve().parent.parent / "config" / "chinook.sqlite").resolve()


def _uniq(prefix: str) -> str:
    return f"{prefix} {uuid.uuid4().hex[:6]}"


def _connectionless(client, headers, *, name=None, description=None, public=True, extra=None):
    body = {"name": name or _uniq("Policies"), **(extra or {})}
    r = client.post("/api/data_sources", json=body, headers=headers)
    assert r.status_code == 200, r.text
    ds = r.json()
    assert ds["connections"] == []
    update = {"is_public": public}
    if description:
        update["description"] = description
    r = client.put(f"/api/data_sources/{ds['id']}", json=update, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def _sqlite_connection(client, headers, name=None):
    r = client.post(
        "/api/connections",
        json={"name": name or _uniq("warehouse"), "type": "sqlite", "config": {"database": str(_CHINOOK)},
              "credentials": {}, "auth_policy": "system_only"},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _agent_on(client, headers, connection_ids, name=None):
    r = client.post("/api/data_sources", json={"name": name or _uniq("Sales"), "connection_ids": connection_ids},
                    headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def _data_agent(client, env, create_data_source, refresh_schema, bulk_update_tables, name=None):
    ds = create_data_source(name=name or _uniq("Chinook"), type="sqlite", config={"database": str(_CHINOOK)},
                            credentials={}, user_token=env["token"], org_id=env["org_id"])
    refresh_schema(data_source_id=ds["id"], user_token=env["token"], org_id=env["org_id"])
    bulk_update_tables(data_source_id=ds["id"], action="activate", user_token=env["token"], org_id=env["org_id"])
    r = client.put(f"/api/data_sources/{ds['id']}", json={"is_public": True}, headers=env["headers"])
    assert r.status_code == 200, r.text
    return ds


def _member(client, create_user, login_user, env):
    org = env["org_id"]
    email = f"cl-{uuid.uuid4().hex[:8]}@example.com"
    r = client.post(f"/api/organizations/{org}/members", headers=env["headers"],
                    json={"organization_id": org, "email": email, "role": "member"})
    assert r.status_code == 200, r.text
    user = create_user(email=email)
    token = login_user(user["email"], user["password"])
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org)}


def _ask(client, headers, llm, prompt, data_sources):
    """New report pinned to `data_sources` ([] = Auto) + one turn.
    Returns (report_id, planner prompts)."""
    llm["planner"].clear()
    llm["base"] = None
    llm["script"] = []
    r = client.post("/api/reports", headers=headers,
                    json={"title": "connectionless", "files": [], "data_sources": data_sources})
    assert r.status_code == 200, r.text
    rid = r.json()["id"]
    r = client.post(f"/api/reports/{rid}/completions", headers=headers, json={"prompt": {"content": prompt}})
    assert r.status_code == 200, r.text
    assert llm["planner"], "the run never reached the planner"
    return rid, list(llm["planner"])


def _agent_ids(client, headers):
    r = client.get("/api/data_sources", headers=headers)
    assert r.status_code == 200, r.text
    return {d["id"] for d in r.json()}


# ── creating ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("extra", [{}, {"connection_ids": []}, {"connection_ids": None, "connection_id": None}],
                         ids=["name-only", "empty-connection-ids", "explicit-nulls"])
def test_agent_can_be_created_without_any_connection(test_client, env, extra):
    name = _uniq("Handbook")
    r = test_client.post("/api/data_sources", json={"name": name, **extra}, headers=env["headers"])
    assert r.status_code == 200, r.text
    created = r.json()
    assert created["name"] == name and created["connections"] == []

    r = test_client.get(f"/api/data_sources/{created['id']}", headers=env["headers"])
    assert r.status_code == 200, r.text
    assert r.json()["connections"] == []
    assert created["id"] in _agent_ids(test_client, env["headers"])


@pytest.mark.parametrize("body", [
    {"type": "sqlite"},
    {"config": {"database": "x.db"}},
], ids=["type-without-config", "config-without-type"])
def test_half_configured_new_connection_is_still_rejected(test_client, env, body):
    r = test_client.post("/api/data_sources", json={"name": _uniq("Broken"), **body}, headers=env["headers"])
    assert r.status_code == 422, r.text


def test_creating_a_connectionless_agent_needs_the_org_create_permission(test_client, env, create_user, login_user):
    member = _member(test_client, create_user, login_user, env)
    r = test_client.post("/api/data_sources", json={"name": _uniq("Sneaky")}, headers=member)
    assert r.status_code == 403, r.text


def test_agent_tool_can_create_a_connectionless_agent():
    from app.ai.tools.schemas.create_agent import CreateAgentInput

    parsed = CreateAgentInput(name="Handbook")
    assert list(parsed.connection_ids or []) == []


# ── its own endpoints ────────────────────────────────────────────────────

def test_connectionless_agent_endpoints_answer_without_server_errors(test_client, env):
    ds = _connectionless(test_client, env["headers"])
    h = env["headers"]
    base = f"/api/data_sources/{ds['id']}"

    for path in (base, f"{base}/schema", f"{base}/full_schema", f"{base}/connections",
                 f"{base}/file-connections", f"{base}/refresh_schema", f"{base}/test_connection",
                 f"{base}/generate_items", f"{base}/members", f"{base}/inherited_access"):
        r = test_client.get(path, headers=h)
        assert r.status_code < 500, f"GET {path} -> {r.status_code}: {r.text[:300]}"

    r = test_client.get(f"{base}/full_schema", headers=h)
    body = r.json()
    tables = body.get("tables", body.get("items", [])) if isinstance(body, dict) else body
    assert tables == []

    r = test_client.get(f"{base}/test_connection", headers=h)
    assert r.status_code == 200 and r.json().get("success") is True, r.text

    # Connection settings have nowhere to go: refused, not silently dropped.
    r = test_client.put(base, json={"config": {"database": "x.db"}}, headers=h)
    assert r.status_code == 400, r.text

    # Even with onboarding switched on, there is nothing to learn from.
    r = test_client.put(base, json={"use_llm_sync": True}, headers=h)
    assert r.status_code == 200, r.text
    r = test_client.post(f"{base}/llm_sync", headers=h)
    assert r.status_code == 200, r.text
    assert r.json().get("skipped") is True, "nothing to learn from: onboarding must not invent a schema"

    r = test_client.get(f"/api/mentions/available?data_source_ids={ds['id']}", headers=h)
    assert r.status_code == 200, r.text
    assert ds["id"] in {d["id"] for d in r.json().get("data_sources", [])}


# ── runs ─────────────────────────────────────────────────────────────────

def test_pinned_connectionless_agent_runs_and_reaches_the_planner(test_client, env, llm):
    desc = f"Answers HR policy questions {uuid.uuid4().hex[:6]}"
    ds = _connectionless(test_client, env["headers"], description=desc)

    _, prompts = _ask(test_client, env["headers"], llm, "How many vacation days do I get?", [ds["id"]])

    assert ds["name"] in prompts[0] and ds["id"] in prompts[0]
    assert desc in prompts[0], "the planner must see what the agent is for"
    assert "no data connection" in prompts[0], "the planner must know there is nothing to query"


def test_connectionless_agent_and_data_agent_run_together(
    test_client, env, llm, create_data_source, refresh_schema, bulk_update_tables,
):
    desc = f"Company glossary {uuid.uuid4().hex[:6]}"
    cl = _connectionless(test_client, env["headers"], description=desc)
    data = _data_agent(test_client, env, create_data_source, refresh_schema, bulk_update_tables)

    _, prompts = _ask(test_client, env["headers"], llm, "Top customers by revenue?", [cl["id"], data["id"]])

    assert desc in prompts[0], "a data agent with clients must not push the connectionless one out"
    assert data["name"] in prompts[0] and "Invoice" in prompts[0], "the data agent keeps its schema"


@pytest.mark.parametrize("as_member", [False, True], ids=["admin", "member"])
def test_auto_mode_runs_with_a_public_connectionless_agent(
    test_client, env, llm, create_data_source, refresh_schema, bulk_update_tables,
    create_user, login_user, as_member,
):
    desc = f"Travel policy {uuid.uuid4().hex[:6]}"
    cl = _connectionless(test_client, env["headers"], description=desc, public=True)
    data = _data_agent(test_client, env, create_data_source, refresh_schema, bulk_update_tables)
    headers = _member(test_client, create_user, login_user, env) if as_member else env["headers"]

    _, prompts = _ask(test_client, headers, llm, "What is the per diem?", [])

    assert desc in prompts[0]
    assert data["name"] in prompts[0] and "Invoice" in prompts[0]


# ── losing connections ───────────────────────────────────────────────────

def test_the_last_connection_can_be_unlinked_and_the_agent_stays(test_client, env, create_instruction):
    h = env["headers"]
    conn = _sqlite_connection(test_client, h)
    agent = _agent_on(test_client, h, [conn])
    ins = create_instruction(text=f"Always say hi {uuid.uuid4().hex[:4]}", user_token=env["token"],
                             org_id=env["org_id"], data_source_ids=[agent["id"]], status="published")

    r = test_client.delete(f"/api/data_sources/{agent['id']}/connections/{conn}", headers=h)
    assert r.status_code == 200, r.text

    r = test_client.get(f"/api/data_sources/{agent['id']}", headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["connections"] == []
    r = test_client.get(f"/api/instructions/{ins['id']}", headers=h)
    assert r.status_code == 200, r.text
    assert agent["id"] in {d["id"] for d in r.json().get("data_sources", [])}

    # The connection itself is untouched — unlinking is not deleting.
    assert test_client.get(f"/api/connections/{conn}", headers=h).status_code == 200


def _seed_unlinked_rows(agent_id, connection_id, user_id):
    """Table rows the API cannot create: a legacy name-keyed row and a
    delegated user's own discovery (both with no connection_table_id), plus
    that user's overlay on it. Written directly for that reason."""
    import asyncio

    from app.dependencies import async_session_maker
    from app.models.datasource_table import DataSourceTable
    from app.models.user_data_source_overlay import UserDataSourceColumn, UserDataSourceTable

    async def _run():
        async with async_session_maker() as db:
            legacy = DataSourceTable(name="legacy_payroll", datasource_id=agent_id, is_active=True,
                                     columns=[{"name": "salary", "dtype": "int"}])
            mine = DataSourceTable(name="my_private_sheet", datasource_id=agent_id, is_active=True,
                                   columns=[{"name": "ssn", "dtype": "text"}],
                                   metadata_json={"discovered_connection_id": connection_id})
            db.add_all([legacy, mine])
            await db.flush()
            overlay = UserDataSourceTable(data_source_id=agent_id, user_id=user_id, connection_id=None,
                                          table_name="my_private_sheet", data_source_table_id=mine.id)
            db.add(overlay)
            await db.flush()
            db.add(UserDataSourceColumn(user_data_source_table_id=overlay.id, column_name="ssn"))
            await db.commit()

    asyncio.run(_run())


def _agent_rows(agent_id):
    import asyncio

    from sqlalchemy import func, select

    from app.dependencies import async_session_maker
    from app.models.datasource_table import DataSourceTable
    from app.models.user_data_source_overlay import UserDataSourceTable

    async def _run():
        async with async_session_maker() as db:
            tables = await db.scalar(select(func.count()).select_from(DataSourceTable)
                                     .where(DataSourceTable.datasource_id == agent_id))
            overlays = await db.scalar(select(func.count()).select_from(UserDataSourceTable)
                                       .where(UserDataSourceTable.data_source_id == agent_id))
            return tables, overlays

    return asyncio.run(_run())


def _schema_names(client, headers, agent_id):
    r = client.get(f"/api/data_sources/{agent_id}/schema", headers=headers)
    assert r.status_code == 200, r.text
    return {t["name"] for t in r.json()}


@pytest.mark.parametrize("how", ["unlink", "delete-connection"])
def test_losing_the_last_connection_leaves_no_table_behind(test_client, env, how):
    """Rows not tied to the connection (legacy, or a delegated user's own
    discovery) must not outlive the agent's last connection: with no
    connection left to scope them, the schema would serve them to everyone."""
    h = env["headers"]
    conn = _sqlite_connection(test_client, h)
    agent = _agent_on(test_client, h, [conn])
    me = test_client.get("/api/users/whoami", headers=h).json()
    _seed_unlinked_rows(agent["id"], conn, me["id"])
    assert "legacy_payroll" in _schema_names(test_client, h, agent["id"]), "the seeded row is served before"

    if how == "unlink":
        r = test_client.delete(f"/api/data_sources/{agent['id']}/connections/{conn}", headers=h)
    else:
        r = test_client.delete(f"/api/connections/{conn}", headers=h)
    assert r.status_code == 200, r.text

    assert _agent_rows(agent["id"]) == (0, 0)
    assert _schema_names(test_client, h, agent["id"]) == set()


def test_deleting_a_connection_keeps_every_agent_that_used_it(test_client, env, llm, create_instruction):
    h = env["headers"]
    doomed = _sqlite_connection(test_client, h)
    other = _sqlite_connection(test_client, h)
    only_on_doomed = _agent_on(test_client, h, [doomed])
    shared = _agent_on(test_client, h, [doomed, other])
    ins = create_instruction(text=f"Fiscal year starts in April {uuid.uuid4().hex[:4]}", user_token=env["token"],
                             org_id=env["org_id"], data_source_ids=[only_on_doomed["id"]], status="published")

    r = test_client.delete(f"/api/connections/{doomed}", headers=h)
    assert r.status_code == 200, r.text

    remaining = _agent_ids(test_client, h)
    assert only_on_doomed["id"] in remaining and shared["id"] in remaining, "no agent is deleted with a connection"

    r = test_client.get(f"/api/data_sources/{only_on_doomed['id']}", headers=h)
    assert r.status_code == 200 and r.json()["connections"] == []
    r = test_client.get(f"/api/data_sources/{shared['id']}", headers=h)
    assert [c["id"] for c in r.json()["connections"]] == [other]

    r = test_client.get(f"/api/instructions/{ins['id']}", headers=h)
    assert r.status_code == 200, r.text
    assert only_on_doomed["id"] in {d["id"] for d in r.json().get("data_sources", [])}

    # And the agent left behind still works in a conversation.
    _, prompts = _ask(test_client, h, llm, "When does the fiscal year start?", [only_on_doomed["id"]])
    assert only_on_doomed["name"] in prompts[0]


def test_deleting_a_connection_removes_a_connector_that_only_wrapped_it(test_client, env):
    """A connector (someone's own account or tools) is nothing but its
    connection: it goes with it. An agent on the same connection stays."""
    h = env["headers"]
    conn = _sqlite_connection(test_client, h)
    r = test_client.post("/api/data_sources", json={"name": _uniq("Mailbox"), "connection_ids": [conn],
                                                     "kind": "connector"}, headers=h)
    assert r.status_code == 200, r.text
    connector = r.json()
    agent = _agent_on(test_client, h, [conn])

    # The confirmation names what goes: the connector, not the agent.
    r = test_client.get(f"/api/connections/{conn}", headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["connectors_deleted_with_it"] == [connector["name"]]
    assert set(r.json()["agent_names"]) == {connector["name"], agent["name"]}

    r = test_client.delete(f"/api/connections/{conn}", headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["deleted_connectors"] == [connector["name"]]

    assert test_client.get(f"/api/data_sources/{connector['id']}", headers=h).status_code == 404
    kept = test_client.get(f"/api/data_sources/{agent['id']}", headers=h)
    assert kept.status_code == 200 and kept.json()["connections"] == []


def test_a_connector_cannot_lose_its_last_connection_by_unlinking(test_client, env):
    """Unlinking would strand a connector with nothing behind it; the manager
    deletes it instead. An agent on the same connection can still unlink."""
    h = env["headers"]
    conn = _sqlite_connection(test_client, h)
    r = test_client.post("/api/data_sources", json={"name": _uniq("Mailbox"), "connection_ids": [conn],
                                                     "kind": "connector"}, headers=h)
    assert r.status_code == 200, r.text
    connector = r.json()
    agent = _agent_on(test_client, h, [conn])

    r = test_client.delete(f"/api/data_sources/{connector['id']}/connections/{conn}", headers=h)
    assert r.status_code == 409, r.text
    r = test_client.get(f"/api/data_sources/{connector['id']}", headers=h)
    assert [c["id"] for c in r.json()["connections"]] == [conn], "the refused unlink changed nothing"

    r = test_client.delete(f"/api/data_sources/{agent['id']}/connections/{conn}", headers=h)
    assert r.status_code == 200, r.text
