"""Regression test for the connection-delete FK race (CI flaky e2e failure).

Creating a connection kicks off background schema indexing that inserts
`connection_tables` rows in a *separate* session. If such a row commits after
`delete_connection` has eager-loaded the (delete-orphan) collection, the parent
DELETE leaves an orphan child and Postgres rejects it with
`connection_tables_connection_id_fkey`. SQLite doesn't enforce the FK, so the
postgres CI leg failed while sqlite silently orphaned the row.

`delete_connection` now drains in-flight indexing first and, as a safety net,
retries after re-fetching if a concurrent writer still slips a row in. This test
deterministically forces that concurrent-write window and asserts the delete
still succeeds and removes every child row.
"""
import uuid
import pytest
from sqlalchemy.future import select

from app.dependencies import async_session_maker
from app.models.connection import Connection
from app.models.connection_table import ConnectionTable
from app.models.organization import Organization
from app.models.user import User
from app.services.connection_service import ConnectionService


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_delete_connection_survives_concurrent_child_insert(monkeypatch):
    org_id = str(uuid.uuid4())
    conn_id = str(uuid.uuid4())
    user_id = str(uuid.uuid4())

    async with async_session_maker() as s:
        s.add(Organization(id=org_id, name=f"org-{org_id[:8]}"))
        await s.commit()
        org = (await s.execute(
            select(Organization).where(Organization.id == org_id))).scalar_one()
        s.add(User(id=user_id, email=f"u-{user_id[:8]}@e.x", name="u", hashed_password="x"))
        s.add(Connection(id=conn_id, name=f"c-{conn_id[:8]}", type="sqlite",
                         config={}, organization_id=org_id))
        await s.commit()

    # Wrap get_connection so that, on its FIRST call inside delete_connection, a
    # concurrent session commits a connection_tables row AFTER the eager-load
    # snapshot is taken — exactly the indexer race. Only inject once so the
    # retry's re-fetch sees a clean, fully-loaded collection.
    original_get = ConnectionService.get_connection
    state = {"injected": False}

    async def racing_get_connection(self, db, cid, organization):
        conn = await original_get(self, db, cid, organization)
        if not state["injected"] and str(cid) == conn_id:
            state["injected"] = True
            async with async_session_maker() as other:
                other.add(ConnectionTable(
                    id=str(uuid.uuid4()), name="late_table", connection_id=conn_id,
                    columns=[], pks=[], fks=[], no_rows=0))
                await other.commit()
        return conn

    monkeypatch.setattr(ConnectionService, "get_connection", racing_get_connection)

    async with async_session_maker() as db:
        org = (await db.execute(
            select(Organization).where(Organization.id == org_id))).scalar_one()
        user = (await db.execute(
            select(User).where(User.id == user_id))).scalar_one()
        result = await ConnectionService().delete_connection(
            db=db, connection_id=conn_id, organization=org, current_user=user)
        assert result["message"] == "Connection deleted successfully"

    assert state["injected"], "test did not exercise the concurrent-insert window"

    async with async_session_maker() as s:
        # The connection must be gone regardless of backend.
        assert (await s.execute(
            select(Connection).where(Connection.id == conn_id))).scalar_one_or_none() is None
        leftover = (await s.execute(
            select(ConnectionTable).where(
                ConnectionTable.connection_id == conn_id))).scalars().all()

    # On Postgres the FK is enforced: the first DELETE raises, the retry
    # re-fetches and the cascade removes the straggler child — no orphan left.
    # SQLite doesn't enforce the FK, so the first DELETE succeeds and the
    # concurrently-inserted row is orphaned (harmless there, and exactly why the
    # bug was invisible on the sqlite CI leg) — so we only assert the strict
    # no-orphan guarantee on FK-enforcing backends.
    import os
    if "postgres" in (os.environ.get("TEST_DATABASE_URL") or ""):
        assert leftover == [], "retry safety net should have removed the orphan child"
