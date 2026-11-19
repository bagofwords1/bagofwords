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
    # The row is closed by a separate thread once the source call has exited
    # (query_executions.finish_after), so it can still be listed right after
    # the kill returns: wait for it, within a bound.
    assert _wait(lambda: _running(test_client, hdr, conn_id)["items"] == [])
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


@pytest.mark.e2e
def test_a_kill_is_refused_where_bow_cannot_interrupt_the_query(test_client, world):
    """Clients that enforce their own deadline run in line: a kill could not
    reach them, so it is refused instead of audited as if it had happened."""
    conn_id = world["conn"]["id"]
    hdr = _hdr(world["conn_admin"]["token"], world["org_id"])
    release = threading.Event()

    class SelfTimedSource:
        _bow_connection_id = conn_id
        _manages_query_deadline = True

        def execute_query(self, query):
            release.wait(30)
            return pd.DataFrame({"x": [1]})

    wrapper = QueryCapturingClientWrapper(SelfTimedSource(), [], [], query_timeout_seconds=60, max_concurrent_queries=4)
    t = threading.Thread(target=lambda: wrapper.execute_query("SELECT 'self timed'"), daemon=True)
    t.start()
    try:
        deadline = time.monotonic() + 10
        items = []
        while time.monotonic() < deadline and not items:
            items = _running(test_client, hdr, conn_id)["items"]
            time.sleep(0.05)
        [item] = items
        assert item["killable"] is False
        r = test_client.post(f"/api/connections/{conn_id}/queries/{item['id']}/kill", headers=hdr)
        assert r.status_code == 409
        assert r.json()["error_code"] == "connection.query_not_killable"
        [still] = _running(test_client, hdr, conn_id)["items"]
        assert still["kill_requested_at"] is None and still["killed_by"] is None
    finally:
        release.set()
        t.join(10)


@pytest.mark.e2e
def test_a_kill_that_loses_the_race_with_completion_is_reported_as_finished(test_client, world, monkeypatch):
    from sqlalchemy import update

    from app.services import query_execution_service as svc

    conn_id = world["conn"]["id"]
    hdr = _hdr(world["conn_admin"]["token"], world["org_id"])
    source, thread, outcome = _start_query(conn_id, monkeypatch)
    [item] = _running(test_client, hdr, conn_id)["items"]

    real = svc.effective_status
    calls = {"n": 0}

    def finishes_mid_request(row, now=None):
        # The query completes between the kill's read and its update.
        calls["n"] += 1
        if calls["n"] == 1:
            table = query_executions._table()
            with query_executions.get_engine().begin() as conn:
                conn.execute(update(table).where(table.c.id == item["id"]).values(
                    status="success", lease_expires_at=None))
            return "running"
        return real(row, now)

    monkeypatch.setattr(svc, "effective_status", finishes_mid_request)
    r = test_client.post(f"/api/connections/{conn_id}/queries/{item['id']}/kill", headers=hdr)
    monkeypatch.setattr(svc, "effective_status", real)
    assert r.status_code == 409, r.text
    assert r.json()["error_code"] == "connection.query_finished"
    source.cancelled.set()
    thread.join(10)
    [done] = [i for i in test_client.get(f"/api/connections/{conn_id}/queries", headers=hdr).json()["items"]
              if i["id"] == item["id"]]
    assert done["kill_requested_at"] is None and done["killed_by"] is None


@pytest.mark.e2e
def test_history_search_matches_underscores_and_percent_literally(test_client, world):
    conn_id = world["conn"]["id"]
    hdr = _hdr(world["admin"]["token"], world["org_id"])

    class Source:
        _bow_connection_id = conn_id

        def execute_query(self, query):
            return pd.DataFrame({"x": [1]})

    wrapper = QueryCapturingClientWrapper(Source(), [], [], query_timeout_seconds=30, max_concurrent_queries=4)
    for sql in ("SELECT * FROM order_items", "SELECT * FROM orderXitems", "SELECT 100 AS pct"):
        wrapper.execute_query(sql)

    def search(term):
        body = test_client.get(f"/api/connections/{conn_id}/queries", params={"search": term}, headers=hdr).json()
        return [i["query_text"] for i in body["items"]]

    assert search("order_items") == ["SELECT * FROM order_items"]
    assert search("%") == []


# --------------------------------------------------------------------------
# Review round 2
# --------------------------------------------------------------------------


def _wait(predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError("condition not met in time")


@pytest.mark.e2e
def test_the_live_queue_is_paged_so_every_query_stays_reachable(test_client, world, monkeypatch):
    conn_id = world["conn"]["id"]
    hdr = _hdr(world["conn_admin"]["token"], world["org_id"])
    started = [_start_query(conn_id, monkeypatch, sql=f"SELECT {i} /* slow */") for i in range(3)]
    try:
        first = test_client.get(f"/api/connections/{conn_id}/queries",
                                params={"scope": "running", "limit": 2}, headers=hdr).json()
        assert len(first["items"]) == 2 and first["total"] == 3 and first["has_more"] is True
        rest = test_client.get(f"/api/connections/{conn_id}/queries",
                               params={"scope": "running", "limit": 2, "offset": 2}, headers=hdr).json()
        assert len(rest["items"]) == 1 and rest["has_more"] is False
        ids = {i["id"] for i in first["items"]} | {i["id"] for i in rest["items"]}
        assert len(ids) == 3
    finally:
        for source, thread, _ in started:
            source.cancelled.set()
            thread.join(10)


@pytest.mark.e2e
def test_history_only_names_reports_the_manager_may_open(test_client, world, create_data_source, create_report):
    """Managing a connection does not grant its reports: a report built on a
    data source the manager cannot see keeps its title and link to itself."""
    from app.data_sources import query_tag

    conn_id = world["conn"]["id"]
    admin = world["admin"]
    ds = create_data_source(name="private agent", type="sqlite", config={"database": str(CHINOOK)},
                            credentials={}, user_token=admin["token"], org_id=world["org_id"])
    report = create_report(title="Board pay review", user_token=admin["token"], org_id=world["org_id"],
                           data_sources=[ds["id"]])

    class Source:
        _bow_connection_id = conn_id

        def execute_query(self, query):
            return pd.DataFrame({"x": [1]})

    with query_tag.query_tag_defaults(report_id=report["id"], source="agent"):
        wrapper = QueryCapturingClientWrapper(Source(), [], [], query_timeout_seconds=30, max_concurrent_queries=4)
    wrapper.execute_query("SELECT salary FROM pay")

    def item(token):
        body = test_client.get(f"/api/connections/{conn_id}/queries", headers=_hdr(token, world["org_id"])).json()
        [only] = body["items"]
        return only

    as_admin = item(admin["token"])
    assert as_admin["report"]["title"] == "Board pay review" and as_admin["report_id"] == report["id"]
    as_manager = item(world["conn_admin"]["token"])
    assert as_manager["report"] is None and as_manager["report_id"] is None
    assert as_manager["query_text"] == "SELECT salary FROM pay"  # the query itself stays visible


@pytest.mark.e2e
def test_a_queued_query_bow_cannot_interrupt_can_still_be_killed_while_it_waits(test_client, world, monkeypatch):
    """Clients that enforce their own deadline cannot be stopped once running,
    but while they wait for a slot a kill simply ends the wait."""
    conn_id = world["conn"]["id"]
    hdr = _hdr(world["conn_admin"]["token"], world["org_id"])
    release = threading.Event()

    class SelfTimed:
        _bow_connection_id = conn_id
        _manages_query_deadline = True

        def execute_query(self, query):
            release.wait(30)
            return pd.DataFrame({"x": [1]})

    outcomes = {}

    def run(name):
        wrapper = QueryCapturingClientWrapper(SelfTimed(), [], [], query_timeout_seconds=60, max_concurrent_queries=1)
        try:
            wrapper.execute_query(f"SELECT '{name}'")
            outcomes[name] = "ok"
        except BaseException as e:  # noqa: BLE001 - asserted below
            outcomes[name] = e

    holder = threading.Thread(target=run, args=("holder",), daemon=True)
    holder.start()
    _wait(lambda: _running(test_client, hdr, conn_id)["items"])
    waiter = threading.Thread(target=run, args=("waiter",), daemon=True)
    waiter.start()
    try:
        queued = _wait(lambda: [i for i in _running(test_client, hdr, conn_id)["items"] if i["status"] == "queued"])
        r = test_client.post(f"/api/connections/{conn_id}/queries/{queued[0]['id']}/kill", headers=hdr)
        assert r.status_code == 200, r.text
        waiter.join(10)
        assert isinstance(outcomes.get("waiter"), QueryKilledError)
        running = [i for i in _running(test_client, hdr, conn_id)["items"] if i["status"] == "running"]
        r = test_client.post(f"/api/connections/{conn_id}/queries/{running[0]['id']}/kill", headers=hdr)
        assert r.status_code == 409 and r.json()["error_code"] == "connection.query_not_killable"
    finally:
        release.set()
        holder.join(10)


@pytest.mark.e2e
def test_only_the_kill_that_requests_the_cancel_is_audited(test_client, world, monkeypatch):
    from app.routes import connection as connection_routes

    calls = []

    async def record(**kwargs):
        calls.append(kwargs.get("action"))

    monkeypatch.setattr(connection_routes.audit_service, "log", record)
    conn_id = world["conn"]["id"]
    hdr = _hdr(world["conn_admin"]["token"], world["org_id"])
    release = threading.Event()

    class Stubborn:
        _bow_connection_id = conn_id

        def execute_query(self, query):
            release.wait(30)  # ignores the cancel, so the row stays live a while
            return pd.DataFrame({"x": [1]})

    from app.data_sources import query_cancellation

    monkeypatch.setattr(query_cancellation, "cancel_thread", lambda client, ident: "not_running")
    wrapper = QueryCapturingClientWrapper(Stubborn(), [], [], query_timeout_seconds=60, max_concurrent_queries=4)
    t = threading.Thread(target=lambda: pytest.raises(QueryKilledError, wrapper.execute_query, "SELECT 1"), daemon=True)
    t.start()
    try:
        [item] = _wait(lambda: _running(test_client, hdr, conn_id)["items"])
        first = test_client.post(f"/api/connections/{conn_id}/queries/{item['id']}/kill", headers=hdr)
        again = test_client.post(f"/api/connections/{conn_id}/queries/{item['id']}/kill", headers=hdr)
        assert first.status_code == 200 and again.status_code == 200
        assert calls == ["connection.query_killed"]  # one kill, one entry
    finally:
        release.set()
        t.join(10)


@pytest.mark.e2e
def test_closing_a_dead_workers_row_never_overwrites_a_query_that_finished(test_client, world, monkeypatch):
    """The kill sees a lapsed lease (dead worker), but the slow worker finishes
    the query before the close: the finished row must stay as it ended."""
    from datetime import datetime, timedelta

    from sqlalchemy import insert, update

    from app.services import query_execution_service as svc

    conn_id = world["conn"]["id"]
    hdr = _hdr(world["conn_admin"]["token"], world["org_id"])
    table = query_executions._table()
    now = datetime.utcnow()
    row_id = "11111111-2222-3333-4444-555555555555"
    with query_executions.get_engine().begin() as conn:
        conn.execute(insert(table).values(
            id=row_id, connection_id=conn_id, organization_id=world["org_id"], status="running",
            queued_at=now - timedelta(minutes=2), started_at=now - timedelta(minutes=2),
            lease_expires_at=now - timedelta(seconds=40), worker_id="slow:1:x", query_text="SELECT 1",
            query_truncated=0, killable=1, created_at=now, updated_at=now,
        ))

    real = svc.effective_status
    calls = {"n": 0}

    def finishes_mid_request(row, now=None):
        calls["n"] += 1
        if calls["n"] == 1:
            with query_executions.get_engine().begin() as conn:
                conn.execute(update(table).where(table.c.id == row_id).values(status="success", lease_expires_at=None))
            return "stale"
        return real(row, now)

    monkeypatch.setattr(svc, "effective_status", finishes_mid_request)
    r = test_client.post(f"/api/connections/{conn_id}/queries/{row_id}/kill", headers=hdr)
    monkeypatch.setattr(svc, "effective_status", real)
    assert r.status_code == 409 and r.json()["error_code"] == "connection.query_finished"
    with query_executions.get_engine().connect() as conn:
        from sqlalchemy import select

        assert conn.execute(select(table.c.status).where(table.c.id == row_id)).scalar() == "success"


@pytest.mark.e2e
def test_query_text_is_encrypted_at_rest_and_still_searchable(test_client, world, monkeypatch):
    """With data encryption on, the rendered SQL (and any error quoting its
    values) is ciphertext in the database, and the tab still shows and finds it."""
    from sqlalchemy import Text, cast, select

    from app.ee.encryption import types as enc

    monkeypatch.setattr(enc, "encryption_active", lambda: True)
    conn_id = world["conn"]["id"]
    hdr = _hdr(world["admin"]["token"], world["org_id"])

    class Source:
        _bow_connection_id = conn_id

        def execute_query(self, query):
            if "missing" in query:
                raise ValueError("no rows for customer 'acme_42'")
            return pd.DataFrame({"x": [1]})

    wrapper = QueryCapturingClientWrapper(Source(), [], [], query_timeout_seconds=30, max_concurrent_queries=4)
    wrapper.execute_query("SELECT * FROM orders WHERE customer = 'acme_42'")
    wrapper.execute_query("SELECT * FROM orders WHERE customer = 'other'")
    with pytest.raises(ValueError):
        wrapper.execute_query("SELECT * FROM missing WHERE customer = 'acme_42'")

    table = query_executions._table()
    with query_executions.get_engine().connect() as conn:
        # Cast to plain TEXT: read what is stored, without the column type decrypting it.
        raw = conn.execute(
            select(cast(table.c.query_text, Text), cast(table.c.error, Text)).where(table.c.connection_id == conn_id)
        ).all()
    assert raw and all(text.startswith(enc.TEXT_ENVELOPE_PREFIX) for text, _ in raw)
    assert all(err is None or err.startswith(enc.TEXT_ENVELOPE_PREFIX) for _, err in raw)

    found = test_client.get(f"/api/connections/{conn_id}/queries", params={"search": "acme_42"}, headers=hdr).json()
    assert sorted(i["query_text"] for i in found["items"]) == [
        "SELECT * FROM missing WHERE customer = 'acme_42'",
        "SELECT * FROM orders WHERE customer = 'acme_42'",
    ]
    assert found["total"] == 2 and found["has_more"] is False
    failed = [i for i in found["items"] if i["status"] == "error"]
    assert "acme_42" in failed[0]["error"]  # decrypted for the manager
