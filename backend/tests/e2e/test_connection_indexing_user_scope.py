"""Per-user catalog indexing runs are tracked separately from the shared run.

Signing in to a per-user source (OneDrive, personal Drive) used to build that
user's catalog INLINE in the OAuth callback — a full recursive drive walk while
the browser sat on the redirect. It now runs as a background
`ConnectionIndexing` row scoped to the user, which is what the UI polls.

The two scopes must not shadow each other: a user's sync must not block (or be
mistaken for) the org-shared run, and the connection list has to surface the run
that is actually doing work for the viewer.
"""
import asyncio
import time
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.connection_indexing import ConnectionIndexing
from app.settings.database import create_async_database_engine

CONNECTION_TEST_DB_PATH = (
    Path(__file__).resolve().parent.parent / "config" / "chinook.sqlite"
).resolve()


def _skip_if_no_chinook():
    if not CONNECTION_TEST_DB_PATH.exists():
        pytest.skip(f"SQLite test database missing at {CONNECTION_TEST_DB_PATH}")


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _seed_user_indexing_row(connection_id: str, user_id: str, **fields):
    """Insert a per-user indexing row the way the OAuth callback's background
    job would, without needing a live Microsoft tenant."""

    async def _seed():
        engine = create_async_database_engine()
        try:
            async with AsyncSession(engine, expire_on_commit=False) as db:
                row = ConnectionIndexing(
                    connection_id=str(connection_id),
                    user_id=str(user_id),
                    status=fields.get("status", "running"),
                    phase=fields.get("phase", "listing folders"),
                    current_item=fields.get("current_item", "Documents/2026"),
                    progress_done=fields.get("progress_done", 12),
                    progress_total=fields.get("progress_total", 40),
                )
                db.add(row)
                await db.commit()
                return str(row.id)
        finally:
            await engine.dispose()

    return _run(_seed())


def _org_rows(connection_id: str):
    async def _fetch():
        engine = create_async_database_engine()
        try:
            async with AsyncSession(engine, expire_on_commit=False) as db:
                result = await db.execute(
                    select(ConnectionIndexing).where(
                        ConnectionIndexing.connection_id == str(connection_id),
                        ConnectionIndexing.user_id.is_(None),
                    )
                )
                return list(result.scalars().all())
        finally:
            await engine.dispose()

    return _run(_fetch())


def _wait_for_terminal_org_run(connection_id: str, timeout_s: float = 15.0):
    """The connection create kicks off the shared run; let it finish so it can't
    race the assertions below."""
    deadline = time.perf_counter() + timeout_s
    while time.perf_counter() < deadline:
        rows = _org_rows(connection_id)
        if rows and all(r.status in ("completed", "failed", "cancelled") for r in rows):
            return rows
        time.sleep(0.1)
    return _org_rows(connection_id)


@pytest.mark.e2e
def test_user_scoped_indexing_is_reported_separately(
    create_connection,
    test_client,
    create_user,
    login_user,
    whoami,
):
    _skip_if_no_chinook()

    user = create_user()
    token = login_user(user["email"], user["password"])
    me = whoami(token)
    org_id = me["organizations"][0]["id"]
    user_id = me["id"]
    headers = {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}

    connection = create_connection(
        name="Scoped Indexing",
        type="sqlite",
        config={"database": str(CONNECTION_TEST_DB_PATH)},
        credentials={},
        user_token=token,
        org_id=org_id,
    )
    conn_id = connection["id"]
    org_run = _wait_for_terminal_org_run(conn_id)
    assert org_run, "creating a connection starts the shared indexing run"

    # The shared run is reported as scope=org and is what ?scope=org returns.
    r = test_client.get(f"/api/connections/{conn_id}/indexing?scope=org", headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["scope"] == "org"

    # No per-user run yet.
    r = test_client.get(f"/api/connections/{conn_id}/indexing?scope=user", headers=headers)
    assert r.status_code == 404

    # A per-user sync lands (as the OAuth callback's background job would)...
    user_row_id = _seed_user_indexing_row(conn_id, user_id)

    # ...and now wins the default (`auto`) view: for a per-user catalog the
    # shared run has nothing to do, so showing it would leave the UI idle while
    # the user's own drive is being indexed.
    r = test_client.get(f"/api/connections/{conn_id}/indexing", headers=headers)
    assert r.status_code == 200, r.text
    payload = r.json()
    assert payload["id"] == user_row_id
    assert payload["scope"] == "user"
    assert payload["status"] == "running"
    # Progress is the signal the sign-in flow lost when the walk was inline.
    assert payload["phase"] == "listing folders"
    assert (payload["progress_done"], payload["progress_total"]) == (12, 40)

    # ?scope=org still reports the shared run — the two never shadow each other.
    r = test_client.get(f"/api/connections/{conn_id}/indexing?scope=org", headers=headers)
    assert r.json()["id"] != user_row_id

    # The connection list inlines the viewer's own run, so the card polls it.
    r = test_client.get("/api/connections", headers=headers)
    assert r.status_code == 200, r.text
    listed = next(c for c in r.json() if c["id"] == conn_id)
    assert listed["indexing"]["id"] == user_row_id
    assert listed["indexing"]["scope"] == "user"


@pytest.mark.e2e
def test_another_users_run_is_not_visible_as_yours(
    create_connection,
    test_client,
    create_user,
    login_user,
    whoami,
):
    """One member's catalog sync must never be reported as another member's."""
    _skip_if_no_chinook()

    owner = create_user()
    owner_token = login_user(owner["email"], owner["password"])
    owner_me = whoami(owner_token)
    org_id = owner_me["organizations"][0]["id"]
    headers = {"Authorization": f"Bearer {owner_token}", "X-Organization-Id": str(org_id)}

    connection = create_connection(
        name="Scoped Indexing Isolation",
        type="sqlite",
        config={"database": str(CONNECTION_TEST_DB_PATH)},
        credentials={},
        user_token=owner_token,
        org_id=org_id,
    )
    conn_id = connection["id"]
    _wait_for_terminal_org_run(conn_id)

    # A row belonging to somebody else entirely.
    _seed_user_indexing_row(conn_id, "00000000-0000-0000-0000-000000000000")

    r = test_client.get(f"/api/connections/{conn_id}/indexing?scope=user", headers=headers)
    assert r.status_code == 404, r.text

    r = test_client.get(f"/api/connections/{conn_id}/indexing", headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["scope"] == "org", "another member's sync is not yours"


@pytest.mark.e2e
def test_scopes_do_not_block_each_other(
    create_connection,
    test_client,
    create_user,
    login_user,
    whoami,
):
    """`start()` is idempotent per scope. An in-flight per-user sync must not
    make the admin's /reindex a no-op that returns the user's row."""
    _skip_if_no_chinook()

    user = create_user()
    token = login_user(user["email"], user["password"])
    me = whoami(token)
    org_id = me["organizations"][0]["id"]
    headers = {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}

    connection = create_connection(
        name="Scoped Indexing Concurrency",
        type="sqlite",
        config={"database": str(CONNECTION_TEST_DB_PATH)},
        credentials={},
        user_token=token,
        org_id=org_id,
    )
    conn_id = connection["id"]
    _wait_for_terminal_org_run(conn_id)

    user_row_id = _seed_user_indexing_row(conn_id, me["id"], status="running")

    r = test_client.post(f"/api/connections/{conn_id}/reindex", headers=headers)
    assert r.status_code == 200, r.text
    started = r.json()["indexing"]
    assert started["id"] != user_row_id
    assert started["scope"] == "org"
