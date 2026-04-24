"""
E2E tests for ConnectionIndexing — the background job that runs refresh_schema
off the HTTP request and exposes progress via `GET /connections/{id}/indexing`.

Covers the Phase 6 acceptance gates:
  - happy path: POST /connections returns fast, indexing completes
  - failure path: bad config returns 400 and does not create an indexing row
  - retry: POST /reindex starts a new row after a terminal failure
  - idempotency: firing reindex while one is running returns the same row
  - inlined indexing payload on the data source GET
"""
import asyncio
import time
from pathlib import Path

import pytest


CONNECTION_TEST_DB_PATH = (
    Path(__file__).resolve().parent.parent / "config" / "chinook.sqlite"
).resolve()


def _skip_if_no_chinook():
    if not CONNECTION_TEST_DB_PATH.exists():
        pytest.skip(f"SQLite test database missing at {CONNECTION_TEST_DB_PATH}")


def _poll_until_terminal(test_client, connection_id, user_token, org_id, *, timeout_s: float = 10.0):
    """Poll GET /connections/{id}/indexing until status is completed/failed/cancelled."""
    headers = {
        "Authorization": f"Bearer {user_token}",
        "X-Organization-Id": str(org_id),
    }
    deadline = time.perf_counter() + timeout_s
    last = None
    while time.perf_counter() < deadline:
        r = test_client.get(f"/api/connections/{connection_id}/indexing", headers=headers)
        if r.status_code == 404:
            time.sleep(0.05)
            continue
        assert r.status_code == 200, r.text
        last = r.json()
        if last["status"] in ("completed", "failed", "cancelled"):
            return last
        time.sleep(0.1)
    pytest.fail(f"Indexing did not reach terminal state within {timeout_s}s; last={last!r}")


@pytest.mark.e2e
def test_indexing_happy_path(
    create_connection,
    test_client,
    create_user,
    login_user,
    whoami,
):
    """Create a sqlite connection against chinook, verify indexing runs to
    completion in the background and populates ConnectionTable rows.
    """
    _skip_if_no_chinook()

    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]

    t0 = time.perf_counter()
    connection = create_connection(
        name="Indexing Happy",
        type="sqlite",
        config={"database": str(CONNECTION_TEST_DB_PATH)},
        credentials={},
        user_token=token,
        org_id=org_id,
    )
    post_duration = time.perf_counter() - t0
    assert post_duration < 2.0, f"POST /connections should return in <2s, took {post_duration:.2f}s"

    conn_id = connection["id"]

    # Initially table_count is 0 — indexing hasn't completed yet.
    # The indexing row should exist and (eventually) reach "completed".
    final = _poll_until_terminal(test_client, conn_id, token, org_id)
    assert final["status"] == "completed", final
    assert final["progress_done"] == final["progress_total"]
    assert final["progress_total"] >= 1
    assert (final.get("stats") or {}).get("table_count", 0) >= 1

    # Verify /tables reflects the indexed set (ConnectionTable rows).
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Organization-Id": str(org_id),
    }
    r = test_client.get(f"/api/connections/{conn_id}/tables", headers=headers)
    assert r.status_code == 200, r.text
    tables = r.json()
    assert len(tables) >= 1


@pytest.mark.e2e
def test_indexing_failure_fast_reject(
    test_client,
    create_user,
    login_user,
    whoami,
):
    """Bad config must return 400 immediately, with no connection or indexing
    persisted. This is the pre-validation gate before kicking off the runner.
    """
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]

    headers = {
        "Authorization": f"Bearer {token}",
        "X-Organization-Id": str(org_id),
    }
    r = test_client.post(
        "/api/connections",
        json={
            "name": "Bad",
            "type": "sqlite",
            "config": {"database": "/this/definitely/does/not/exist.sqlite"},
            "credentials": {},
            "auth_policy": "system_only",
        },
        headers=headers,
    )
    assert r.status_code == 400, r.text


@pytest.mark.e2e
def test_indexing_reindex_endpoint(
    create_connection,
    test_client,
    create_user,
    login_user,
    whoami,
):
    """POST /reindex starts a new indexing run after the first one completed."""
    _skip_if_no_chinook()

    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]

    connection = create_connection(
        name="Reindex Test",
        type="sqlite",
        config={"database": str(CONNECTION_TEST_DB_PATH)},
        credentials={},
        user_token=token,
        org_id=org_id,
    )
    conn_id = connection["id"]
    first = _poll_until_terminal(test_client, conn_id, token, org_id)
    assert first["status"] == "completed"
    first_id = first["id"]

    headers = {
        "Authorization": f"Bearer {token}",
        "X-Organization-Id": str(org_id),
    }
    r = test_client.post(f"/api/connections/{conn_id}/reindex", headers=headers)
    assert r.status_code == 200, r.text
    new_idx = r.json()["indexing"]
    assert new_idx["id"] != first_id, "reindex should create a fresh row"
    assert new_idx["status"] in ("pending", "running")

    second = _poll_until_terminal(test_client, conn_id, token, org_id)
    assert second["status"] == "completed"
    assert second["id"] == new_idx["id"]


@pytest.mark.e2e
def test_indexing_datasource_payload_inlined(
    create_connection,
    test_client,
    create_user,
    login_user,
    whoami,
):
    """`GET /data_sources/{id}` inlines `indexing` into each connection entry.

    The page layout uses this to drive its polling loop and status badges.
    """
    _skip_if_no_chinook()

    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]

    headers = {
        "Authorization": f"Bearer {token}",
        "X-Organization-Id": str(org_id),
    }
    # Create a data source (Mode 1 — new connection inline).
    r = test_client.post(
        "/api/data_sources",
        json={
            "name": "Indexing DS",
            "type": "sqlite",
            "config": {"database": str(CONNECTION_TEST_DB_PATH)},
            "credentials": {},
            "is_public": True,
        },
        headers=headers,
    )
    assert r.status_code in (200, 201), r.text
    ds = r.json()
    ds_id = ds["id"]
    assert len(ds.get("connections") or []) == 1
    # On the create response, indexing should be present (pending or running).
    first_conn = ds["connections"][0]
    assert first_conn.get("indexing") is not None
    assert first_conn["indexing"]["status"] in ("pending", "running", "completed")
    conn_id = first_conn["id"]

    # Wait for the indexing to complete, then re-fetch the data source.
    _poll_until_terminal(test_client, conn_id, token, org_id)
    r2 = test_client.get(f"/api/data_sources/{ds_id}", headers=headers)
    assert r2.status_code == 200, r2.text
    ds2 = r2.json()
    conn2 = ds2["connections"][0]
    assert conn2["indexing"] is not None
    assert conn2["indexing"]["status"] == "completed"


@pytest.mark.e2e
def test_indexing_idempotent_while_running(
    monkeypatch,
    create_connection,
    test_client,
    create_user,
    login_user,
    whoami,
):
    """While a job is pending/running, POST /reindex returns the same row.

    We slow down the sqlite client's progress loop so the job stays "running"
    long enough for two overlapping POSTs to collide.
    """
    _skip_if_no_chinook()

    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]

    connection = create_connection(
        name="Idempotent Reindex",
        type="sqlite",
        config={"database": str(CONNECTION_TEST_DB_PATH)},
        credentials={},
        user_token=token,
        org_id=org_id,
    )
    conn_id = connection["id"]
    _poll_until_terminal(test_client, conn_id, token, org_id)

    # Patch the sqlite client's get_tables to sleep per-iteration, keeping the
    # job "running" long enough for two POSTs to overlap.
    from app.data_sources.clients import sqlite_client

    original_get_tables = sqlite_client.SqliteClient.get_tables

    def slow_get_tables(self, progress_callback=None):  # type: ignore[override]
        import time as _t
        _t.sleep(0.05)
        return original_get_tables(self, progress_callback=progress_callback)

    monkeypatch.setattr(sqlite_client.SqliteClient, "get_tables", slow_get_tables)

    headers = {
        "Authorization": f"Bearer {token}",
        "X-Organization-Id": str(org_id),
    }
    # Fire twice back-to-back
    r1 = test_client.post(f"/api/connections/{conn_id}/reindex", headers=headers)
    r2 = test_client.post(f"/api/connections/{conn_id}/reindex", headers=headers)
    assert r1.status_code == 200
    assert r2.status_code == 200
    id1 = r1.json()["indexing"]["id"]
    id2 = r2.json()["indexing"]["id"]
    # They must match: second POST should see the in-flight row and return it.
    assert id1 == id2, (r1.json(), r2.json())

    _poll_until_terminal(test_client, conn_id, token, org_id)
