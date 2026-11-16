"""The connection Queries view: managers see and kill BOW's queries; nobody else does.

GET  /api/connections/{id}/queries               running (scope=running) or history
POST /api/connections/{id}/queries/{qid}/kill     cancel a queued or running query

World:
    admin       — full admin; owns a SQLite connection and a second connection
    conn_admin  — member with `manage_connection` on the first connection only
    member      — member with no connection grant

A query is issued through the real query wrapper (the same path generated code
uses) against a source that blocks until the database is told to cancel it.
"""
import threading
import time
from pathlib import Path

import pandas as pd
import pytest

from app.ai.code_execution.code_execution import QueryCapturingClientWrapper
from app.data_sources import query_executions
from app.data_sources.query_executions import QueryKilledError

CHINOOK = (Path(__file__).resolve().parents[2] / "config" / "chinook.sqlite").resolve()


def _hdr(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


@pytest.fixture
def world(test_client, bootstrap_admin, invite_user_to_org, create_connection, grant_resource):
    if not CHINOOK.exists():
        pytest.skip(f"Missing SQLite fixture at {CHINOOK}")
    query_executions.reset_engine()
    admin = bootstrap_admin("admin")
    org_id = admin["org_id"]
    conn = create_connection(name="warehouse", type="sqlite", config={"database": str(CHINOOK)},
                             credentials={}, user_token=admin["token"], org_id=org_id)
    other = create_connection(name="other", type="sqlite", config={"database": str(CHINOOK)},
                              credentials={}, user_token=admin["token"], org_id=org_id)
    conn_admin = invite_user_to_org(org_id=org_id, admin_token=admin["token"])
    g = grant_resource(resource_type="connection", resource_id=conn["id"], principal_type="user",
                       principal_id=conn_admin["user_id"], permissions=["manage_connection"],
                       user_token=admin["token"], org_id=org_id)
    assert g.status_code == 200, g.json()
    member = invite_user_to_org(org_id=org_id, admin_token=admin["token"])
    yield {"org_id": org_id, "admin": admin, "conn": conn, "other": other,
           "conn_admin": conn_admin, "member": member}
    query_executions.reset_engine()


class _BlockingSource:
    def __init__(self, connection_id):
        self._bow_connection_id = connection_id
        self._bow_connection_name = "warehouse"
        self.started = threading.Event()
        self.cancelled = threading.Event()

    def execute_query(self, query):
        self.started.set()
        if self.cancelled.wait(timeout=30):
            raise RuntimeError("canceling statement due to user request")
        return pd.DataFrame({"x": [1]})


def _start_query(connection_id, monkeypatch, sql="SELECT * FROM Track /* slow */"):
    from app.data_sources import query_cancellation

    source = _BlockingSource(connection_id)
    monkeypatch.setattr(query_cancellation, "cancel_thread",
                        lambda client, ident: (source.cancelled.set(), "cancelled")[1])
    wrapper = QueryCapturingClientWrapper(source, [], [], query_timeout_seconds=60, max_concurrent_queries=4)
    outcome = {}

    def run():
        try:
            outcome["result"] = wrapper.execute_query(sql)
        except BaseException as e:  # noqa: BLE001 - asserted by the test
            outcome["error"] = e

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    assert source.started.wait(10)
    return source, thread, outcome


def _running(test_client, hdr, connection_id):
    r = test_client.get(f"/api/connections/{connection_id}/queries", params={"scope": "running"}, headers=hdr)
    assert r.status_code == 200, r.text
    return r.json()


@pytest.mark.e2e
def test_a_connection_manager_sees_the_running_query_and_kills_it(test_client, world, monkeypatch):
    conn_id = world["conn"]["id"]
    hdr = _hdr(world["conn_admin"]["token"], world["org_id"])
    source, thread, outcome = _start_query(conn_id, monkeypatch)

    body = _running(test_client, hdr, conn_id)
    assert body["summary"]["running"] == 1
    assert body["summary"]["limit"] >= 1
    [item] = body["items"]
    assert item["status"] == "running"
    assert item["query_text"] == "SELECT * FROM Track /* slow */"  # the query itself, not code
    assert item["duration_ms"] is not None

    r = test_client.post(f"/api/connections/{conn_id}/queries/{item['id']}/kill", headers=hdr)
    assert r.status_code == 200, r.text
    assert r.json()["kill_requested_at"]
    thread.join(10)

    assert isinstance(outcome.get("error"), QueryKilledError)
    assert source.cancelled.is_set()
    assert _running(test_client, hdr, conn_id)["items"] == []
    history = test_client.get(f"/api/connections/{conn_id}/queries", headers=hdr).json()
    [done] = history["items"]
    assert done["status"] == "cancelled"
    assert done["error_code"] == "killed"  # localized by the UI, not raw text
    assert done["killed_by"]["id"] == world["conn_admin"]["user_id"]
    # A second kill of a finished query is refused, not silently accepted.
    again = test_client.post(f"/api/connections/{conn_id}/queries/{item['id']}/kill", headers=hdr)
    assert again.status_code == 409
    assert again.json()["error_code"] == "connection.query_finished"


@pytest.mark.e2e
def test_members_without_manage_connection_cannot_see_or_kill(test_client, world, monkeypatch):
    conn_id = world["conn"]["id"]
    source, thread, outcome = _start_query(conn_id, monkeypatch)
    admin_hdr = _hdr(world["admin"]["token"], world["org_id"])
    [item] = _running(test_client, admin_hdr, conn_id)["items"]

    member_hdr = _hdr(world["member"]["token"], world["org_id"])
    assert test_client.get(f"/api/connections/{conn_id}/queries", headers=member_hdr).status_code == 403
    assert test_client.get(f"/api/connections/{conn_id}/queries", params={"scope": "running"},
                           headers=member_hdr).status_code == 403
    r = test_client.post(f"/api/connections/{conn_id}/queries/{item['id']}/kill", headers=member_hdr)
    assert r.status_code == 403
    # The connection admin of ANOTHER connection has no reach here either.
    conn_admin_hdr = _hdr(world["conn_admin"]["token"], world["org_id"])
    other_id = world["other"]["id"]
    assert test_client.get(f"/api/connections/{other_id}/queries", headers=conn_admin_hdr).status_code == 403

    time.sleep(0.5)
    assert thread.is_alive() and "error" not in outcome  # nothing was cancelled
    source.cancelled.set()
    thread.join(10)


@pytest.mark.e2e
def test_a_query_id_from_another_connection_is_not_found(test_client, world, monkeypatch):
    conn_id, other_id = world["conn"]["id"], world["other"]["id"]
    admin_hdr = _hdr(world["admin"]["token"], world["org_id"])
    source, thread, outcome = _start_query(conn_id, monkeypatch)
    [item] = _running(test_client, admin_hdr, conn_id)["items"]

    # Managing connection B does not let you kill A's query by putting it under B.
    r = test_client.post(f"/api/connections/{other_id}/queries/{item['id']}/kill", headers=admin_hdr)
    assert r.status_code == 404
    assert r.json()["error_code"] == "connection.query_not_found"
    assert thread.is_alive()
    source.cancelled.set()
    thread.join(10)


@pytest.mark.e2e
def test_history_lists_finished_queries_newest_first_with_filters(test_client, world):
    conn_id = world["conn"]["id"]
    hdr = _hdr(world["admin"]["token"], world["org_id"])

    class Source:
        _bow_connection_id = conn_id

        def execute_query(self, query):
            if "missing" in query:
                raise ValueError("no such table: missing")
            return pd.DataFrame({"x": [1, 2]})

    wrapper = QueryCapturingClientWrapper(Source(), [], [], query_timeout_seconds=30, max_concurrent_queries=4)
    wrapper.execute_query("SELECT Name FROM Artist")
    with pytest.raises(ValueError):
        wrapper.execute_query("SELECT * FROM missing")
    wrapper.execute_query("SELECT Title FROM Album")

    body = test_client.get(f"/api/connections/{conn_id}/queries", headers=hdr).json()
    assert [i["query_text"] for i in body["items"]] == [
        "SELECT Title FROM Album", "SELECT * FROM missing", "SELECT Name FROM Artist",
    ]
    assert body["total"] == 3 and body["has_more"] is False

    errors = test_client.get(f"/api/connections/{conn_id}/queries", params={"status": "error"}, headers=hdr).json()
    assert [i["query_text"] for i in errors["items"]] == ["SELECT * FROM missing"]
    assert "no such table" in errors["items"][0]["error"]

    found = test_client.get(f"/api/connections/{conn_id}/queries", params={"search": "artist"}, headers=hdr).json()
    assert [i["query_text"] for i in found["items"]] == ["SELECT Name FROM Artist"]

    page = test_client.get(f"/api/connections/{conn_id}/queries", params={"limit": 2}, headers=hdr).json()
    assert len(page["items"]) == 2 and page["has_more"] is True

    # Another connection's history stays separate.
    other = test_client.get(f"/api/connections/{world['other']['id']}/queries", headers=hdr).json()
    assert other["items"] == []
