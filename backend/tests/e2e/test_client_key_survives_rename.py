"""Saved code keeps reaching its source after the agent or connection is renamed.

Saved step code names its source in the client key,
`ds_clients["<agent>:<connection>"]`. Renaming the agent (or the connection)
used to strand every saved query that spelled the old name: the run failed on
a missing key until each query was edited by hand.

Run:
    cd backend
    uv run pytest tests/e2e/test_client_key_survives_rename.py -v
"""
import asyncio
import uuid
from datetime import datetime

import pytest

from app.dependencies import async_session_maker
from app.models.query import Query
from app.models.report import Report
from app.models.step import Step
from app.models.widget import Widget


def _run(coro):
    return asyncio.run(coro)


def _code(key: str, sql: str = "SELECT 7 AS n") -> str:
    return (
        "def generate_df(ds_clients, excel_files):\n"
        f"    return ds_clients[{key!r}].execute_query({sql!r})\n"
    )


async def _seed_query(report_id, code):
    """The widget/query/step graph a saved dashboard query has. There is no
    public CRUD API for it, so it is written directly (as
    test_query_viewer_run_with_data_source.py does)."""
    suffix = uuid.uuid4().hex[:8]
    async with async_session_maker() as db:
        report = await db.get(Report, report_id)
        widget = Widget(title=f"W {suffix}", slug=f"w-{suffix}", report_id=report_id)
        db.add(widget)
        await db.flush()
        query = Query(title=f"Q {suffix}", report_id=report_id, widget_id=widget.id,
                      organization_id=report.organization_id, user_id=report.user_id)
        db.add(query)
        await db.flush()
        step = Step(title="Step", slug=f"s-{suffix}", status="success", widget_id=widget.id,
                    query_id=query.id, code=code, data={"rows": [], "columns": []},
                    created_at=datetime.utcnow())
        db.add(step)
        await db.flush()
        query.default_step_id = step.id
        await db.commit()
        return str(query.id)


@pytest.fixture
def world(dynamic_sqlite_db, create_data_source, create_report, create_user, login_user, whoami, test_client):
    """An admin, one sqlite agent with one connection, and a report on it."""
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    headers = {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}
    name = f"Warehouse DWH {uuid.uuid4().hex[:6]}"
    ds = create_data_source(name=name, type="sqlite", config={"database": dynamic_sqlite_db},
                            credentials={}, user_token=token, org_id=org_id)
    detail = test_client.get(f"/api/data_sources/{ds['id']}", headers=headers).json()
    connection = detail["connections"][0]
    report = create_report(title="Saved dashboard", user_token=token, org_id=org_id, data_sources=[ds["id"]])

    class World:
        pass

    w = World()
    w.token, w.org_id, w.headers = token, org_id, headers
    w.ds_id, w.ds_name = ds["id"], name
    w.connection_id, w.connection_name = connection["id"], connection["name"]
    w.report_id = report["id"]
    w.create_data_source, w.create_report, w.sqlite_db = create_data_source, create_report, dynamic_sqlite_db

    def run(code, report_id=None):
        query_id = _run(_seed_query(report_id or w.report_id, code))
        response = test_client.post(f"/api/queries/{query_id}/run",
                                    json={"mode": "viewer", "force_refresh": True, "params": {}},
                                    headers=headers)
        return response

    def rename_agent(new_name, ds_id=None):
        r = test_client.put(f"/api/data_sources/{ds_id or w.ds_id}", json={"name": new_name}, headers=headers)
        assert r.status_code == 200, r.json()

    def rename_connection(new_name):
        r = test_client.put(f"/api/connections/{w.connection_id}", json={"name": new_name}, headers=headers)
        assert r.status_code == 200, r.json()

    w.run, w.rename_agent, w.rename_connection = run, rename_agent, rename_connection
    return w


def _assert_rows(response):
    assert response.status_code == 200, response.json()
    body = response.json()
    assert body["status"] == "success", body
    rows = (body.get("data") or {}).get("rows") or []
    assert rows and int(rows[0]["n"]) == 7, body


def _assert_no_rows(response):
    body = response.json()
    assert response.status_code != 200 or body.get("status") != "success", body


@pytest.mark.e2e
@pytest.mark.parametrize("key_form", ["agent_and_connection", "bare_agent"])
def test_saved_code_runs_after_the_agent_is_renamed(world, key_form):
    old_key = f"{world.ds_name}:{world.connection_name}" if key_form == "agent_and_connection" else world.ds_name
    code = _code(old_key)
    _assert_rows(world.run(code))  # the key is right to begin with

    world.rename_agent(f"Warehouse {uuid.uuid4().hex[:6]}")

    _assert_rows(world.run(code))


@pytest.mark.e2e
def test_saved_code_runs_after_the_connection_is_renamed(world):
    code = _code(f"{world.ds_name}:{world.connection_name}")
    world.rename_connection(f"conn-{uuid.uuid4().hex[:6]}")
    _assert_rows(world.run(code))


@pytest.mark.e2e
def test_saved_code_runs_after_both_are_renamed_more_than_once(world):
    first_key = f"{world.ds_name}:{world.connection_name}"
    second_agent = f"Warehouse {uuid.uuid4().hex[:6]}"
    world.rename_agent(second_agent)
    second_key = f"{second_agent}:{world.connection_name}"
    world.rename_connection(f"conn-{uuid.uuid4().hex[:6]}")
    world.rename_agent(f"Lake {uuid.uuid4().hex[:6]}")

    # Code saved under every name the pair ever had still runs.
    _assert_rows(world.run(_code(first_key)))
    _assert_rows(world.run(_code(second_key)))


@pytest.mark.e2e
def test_the_current_name_keeps_working_and_renaming_back_restores_it(world, test_client):
    new_name = f"Warehouse {uuid.uuid4().hex[:6]}"
    world.rename_agent(new_name)
    _assert_rows(world.run(_code(f"{new_name}:{world.connection_name}")))

    world.rename_agent(world.ds_name)
    _assert_rows(world.run(_code(f"{world.ds_name}:{world.connection_name}")))
    _assert_rows(world.run(_code(f"{new_name}:{world.connection_name}")))


@pytest.mark.e2e
def test_a_name_never_held_still_fails(world):
    world.rename_agent(f"Warehouse {uuid.uuid4().hex[:6]}")
    _assert_no_rows(world.run(_code(f"Never {uuid.uuid4().hex[:6]}:{world.connection_name}")))


@pytest.mark.e2e
def test_an_old_name_taken_by_another_agent_does_not_reach_the_renamed_one(world, tmp_path):
    """Once a different agent carries the old name, the old key is that
    agent's: code spelling it must not be answered by the renamed agent."""
    import sqlite3

    old_key = f"{world.ds_name}:{world.connection_name}"
    world.rename_agent(f"Warehouse {uuid.uuid4().hex[:6]}")

    other_db = tmp_path / "other.db"
    con = sqlite3.connect(other_db)
    con.execute("CREATE TABLE users (id INTEGER)")
    con.commit()
    con.close()
    other = world.create_data_source(name=world.ds_name, type="sqlite", config={"database": str(other_db)},
                                     credentials={}, user_token=world.token, org_id=world.org_id)
    # A report that runs on both agents, as an org-wide chat report does.
    report = world.create_report(title="Both agents", user_token=world.token, org_id=world.org_id,
                                 data_sources=[world.ds_id, other["id"]])

    _assert_no_rows(world.run(_code(old_key), report_id=report["id"]))
