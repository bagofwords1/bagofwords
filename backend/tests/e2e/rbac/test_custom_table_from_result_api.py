"""Saving a result as a custom table: who may read the query behind a result,
and the two lookups the result card and the saving dialog rely on.

The query behind a result belongs to a conversation. Being an admin of the
connection that answered it is required but not sufficient: the caller must
also be someone who could read that conversation. Two people sharing an agent
do not get to read each other's queries through this route.
"""
import os
import sqlite3
import uuid

import pytest
from sqlalchemy import create_engine, text

QUERY = "SELECT id, total FROM orders WHERE total > 10"


def _hdr(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


def _tool_execution_id(report_id):
    """The id of the tool execution seeded into a report. Read from the test
    database because agent runs have no API that creates or lists them (see
    the `seed_agent_executions` fixture)."""
    url = os.environ["TEST_DATABASE_URL"]
    sync = url.replace("sqlite+aiosqlite:", "sqlite:").replace("postgresql+asyncpg:", "postgresql:")
    engine = create_engine(sync)
    try:
        with engine.connect() as con:
            return con.execute(text(
                "SELECT t.id FROM tool_executions t JOIN agent_executions a ON a.id = t.agent_execution_id "
                "WHERE a.report_id = :r"), {"r": str(report_id)}).scalar_one()
    finally:
        engine.dispose()


@pytest.fixture
def world(tmp_path, test_client, bootstrap_admin, invite_user_to_org, create_role, assign_role,
          create_report, seed_agent_executions):
    admin = bootstrap_admin("admin")
    org = admin["org_id"]
    h = _hdr(admin["token"], org)

    def member(perms):
        m = invite_user_to_org(org_id=org, admin_token=admin["token"])
        if perms:
            role = create_role(name=f"r-{m['user_id'][:8]}", permissions=perms,
                               user_token=admin["token"], org_id=org)
            assert role.status_code == 200, role.json()
            r = assign_role(role_id=role.json()["id"], principal_type="user",
                            principal_id=m["user_id"], user_token=admin["token"], org_id=org)
            assert r.status_code in (200, 201), r.json()
        return m

    db_path = tmp_path / f"shop_{uuid.uuid4().hex[:8]}.sqlite"
    con = sqlite3.connect(db_path)
    con.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, total INTEGER)")
    con.executemany("INSERT INTO orders VALUES (?, ?)", [(i, i) for i in range(1, 41)])
    con.commit()
    con.close()

    r = test_client.post("/api/connections", headers=h, json={
        "name": f"Shop {uuid.uuid4().hex[:6]}", "type": "sqlite",
        "config": {"database": str(db_path)}, "credentials": {}})
    assert r.status_code == 200, r.text
    conn_id = r.json()["id"]
    a = test_client.post("/api/data_sources", headers=h, json={
        "name": f"Shop agent {uuid.uuid4().hex[:6]}", "connection_ids": [conn_id], "is_public": True,
        "generate_summary": False, "generate_conversation_starters": False, "generate_ai_rules": False})
    assert a.status_code == 200, a.text
    agent_id = a.json()["id"]
    assert test_client.put("/api/organization/settings", headers=h,
                           json={"config": {"enable_custom_queries": {"value": True}}}).status_code == 200

    conn_admin = member(["manage_connections", "view_code"])
    author = member(["view_code"])

    def report_with_result(owner, result_json=None):
        report = create_report(title="Orders", user_token=owner["token"], org_id=org, data_sources=[agent_id])
        seed_agent_executions(org, report["id"], [{
            "user_id": owner["user_id"],
            "tools": [{"name": "create_data", "result_json": result_json or {
                "executed_queries": [QUERY],
                "query_timings": [{"index": 0, "rows": 30, "connection_id": conn_id,
                                   "data_source_id": agent_id, "client_key": "Shop agent:shop"}],
            }}],
        }])
        return _tool_execution_id(report["id"])

    return {"org": org, "admin": admin, "conn_admin": conn_admin, "author": author,
            "conn_id": conn_id, "agent_id": agent_id, "report_with_result": report_with_result,
            "base": f"/api/connections/{conn_id}/custom-queries"}


def _from_result(test_client, world, who, tool_execution_id):
    return test_client.get(f"{world['base']}/from-result/{tool_execution_id}",
                           headers=_hdr(world[who]["token"], world["org"]))


# -- who may read the query behind a result ---------------------------------

@pytest.mark.e2e
def test_a_connection_admin_gets_the_query_of_their_own_result(test_client, world):
    te = world["report_with_result"](world["conn_admin"])
    res = _from_result(test_client, world, "conn_admin", te)
    assert res.status_code == 200, res.text
    assert res.json() == {"definition_sql": QUERY}


@pytest.mark.e2e
def test_a_connection_admin_cannot_read_the_query_of_someone_elses_conversation(test_client, world):
    """Both use the same agent, so the connection admin can see that the report
    exists. They still cannot read its conversation, so they do not get its
    query either: the answer is the one an unknown id gets."""
    te = world["report_with_result"](world["author"])
    res = _from_result(test_client, world, "conn_admin", te)
    assert res.status_code == 404, res.text
    assert QUERY not in res.text
    unknown = _from_result(test_client, world, "conn_admin", str(uuid.uuid4()))
    assert unknown.status_code == 404
    assert res.json() == unknown.json()


@pytest.mark.e2e
def test_an_organization_admin_can_read_it_as_they_can_read_the_conversation(test_client, world):
    te = world["report_with_result"](world["author"])
    res = _from_result(test_client, world, "admin", te)
    assert res.status_code == 200, res.text
    assert res.json()["definition_sql"] == QUERY


@pytest.mark.e2e
def test_the_results_own_author_needs_to_manage_the_connection(test_client, world):
    te = world["report_with_result"](world["author"])
    assert _from_result(test_client, world, "author", te).status_code == 403


@pytest.mark.e2e
def test_a_result_with_no_single_query_is_refused(test_client, world):
    te = world["report_with_result"](world["conn_admin"], {
        "executed_queries": [QUERY, "SELECT 1"],
        "query_timings": [{"index": 0, "connection_id": world["conn_id"]},
                          {"index": 1, "connection_id": world["conn_id"]}],
    })
    res = _from_result(test_client, world, "conn_admin", te)
    assert res.status_code == 422, res.text


# -- can this connection host a custom table --------------------------------

@pytest.mark.e2e
def test_capability_is_answered_for_a_connection_admin_who_does_not_manage_the_agent(test_client, world):
    """The result card asks the connection, not the agent: a connection admin
    with ordinary access to the agent must still get an answer."""
    h = _hdr(world["conn_admin"]["token"], world["org"])
    agent = test_client.get(f"/api/data_sources/{world['agent_id']}/connections", headers=h)
    assert agent.status_code == 403, "precondition: this user does not manage the agent"
    res = test_client.get(f"{world['base']}/capability", headers=h)
    assert res.status_code == 200, res.text
    assert res.json()["supported"] is True
    assert res.json()["id"] == world["conn_id"]


@pytest.mark.e2e
def test_capability_says_no_when_custom_tables_are_switched_off(test_client, world):
    admin_h = _hdr(world["admin"]["token"], world["org"])
    assert test_client.put("/api/organization/settings", headers=admin_h,
                           json={"config": {"enable_custom_queries": {"value": False}}}).status_code == 200
    res = test_client.get(f"{world['base']}/capability", headers=_hdr(world["conn_admin"]["token"], world["org"]))
    assert res.status_code == 200, res.text
    assert res.json()["supported"] is False


@pytest.mark.e2e
def test_capability_is_not_answered_for_someone_who_cannot_manage_the_connection(test_client, world):
    res = test_client.get(f"{world['base']}/capability", headers=_hdr(world["author"]["token"], world["org"]))
    assert res.status_code == 403


# -- one table's load state --------------------------------------------------

@pytest.mark.e2e
def test_progress_reports_one_table_by_name(test_client, world):
    h = _hdr(world["conn_admin"]["token"], world["org"])
    before = test_client.get(f"{world['base']}/progress", headers=h, params={"name": "orders_copy"})
    assert before.status_code == 200, before.text
    assert before.json()["exists"] is False

    created = test_client.post(world["base"], headers=h,
                               json={"name": "orders_copy", "definition_sql": "SELECT * FROM orders"})
    assert created.status_code == 200, created.text

    after = test_client.get(f"{world['base']}/progress", headers=h, params={"name": "orders_copy"}).json()
    assert after["exists"] is True
    assert after["id"] == created.json()["id"]
    assert after["last_refresh_status"] == "ok"
    assert after["last_refreshed_at"]
    # Nothing is running any more, so there is no progress to report.
    assert after["refresh_progress"] is None
    other = test_client.get(f"{world['base']}/progress", headers=h, params={"name": "something_else"}).json()
    assert other == {**other, "exists": False}


@pytest.mark.e2e
def test_progress_is_for_connection_admins(test_client, world):
    res = test_client.get(f"{world['base']}/progress", headers=_hdr(world["author"]["token"], world["org"]),
                          params={"name": "orders_copy"})
    assert res.status_code == 403
