"""An unsuccessful discovery/publication must preserve the last successful snapshot."""
import asyncio
from datetime import datetime
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.dependencies import async_session_maker
from app.models.connection import Connection
from app.models.connection_table import ConnectionTable
from app.models.data_source import DataSource
from app.models.datasource_table import DataSourceTable
from app.models.domain_connection import domain_connection
from app.services.connection_service import ConnectionService
from app.data_sources.clients.discovery import DiscoveryResult, IncompleteDiscoveryError
from app.data_sources.clients.progress import IndexingCancelled
from app.data_sources.clients.sqlite_client import SqliteClient

pytestmark = pytest.mark.e2e


def table(name, column="id"):
    return {"name": name, "columns": [{"name": column, "dtype": "int"}], "pks": [], "fks": []}


async def seed(org_id):
    # A persisted last-successful snapshot, including selections and its exact
    # timestamp, is the recovery baseline. No public endpoint can restore an
    # arbitrary historical snapshot for fault injection.
    async with async_session_maker() as db:
        c = Connection(name=str(uuid4()), type="sqlite", config={"database": ":memory:"},
                       organization_id=org_id, auth_policy="system_only",
                       last_synced_at=datetime(2025, 1, 1))
        db.add(c)
        await db.flush()
        for i in range(2):
            ds = DataSource(name=str(uuid4()), organization_id=org_id, is_active=True)
            db.add(ds)
            await db.flush()
            await db.execute(domain_connection.insert().values(data_source_id=ds.id, connection_id=c.id))
            for name in ("alpha", "beta"):
                ct = (await db.execute(select(ConnectionTable).where(
                    ConnectionTable.connection_id == c.id, ConnectionTable.name == name))).scalar_one_or_none()
                if ct is None:
                    ct = ConnectionTable(connection_id=c.id, no_rows=0, **table(name))
                    db.add(ct)
                    await db.flush()
                db.add(DataSourceTable(datasource_id=ds.id, connection_table_id=ct.id,
                                       is_active=(name == "alpha"), no_rows=0, **table(name)))
        await db.commit()
        return c.id


async def snapshot(cid):
    async with async_session_maker() as db:
        c = await db.get(Connection, cid)
        tables = (await db.execute(select(ConnectionTable).where(ConnectionTable.connection_id == cid))).scalars().all()
        agents = (await db.execute(select(DataSourceTable).join(ConnectionTable).where(
            ConnectionTable.connection_id == cid))).scalars().all()
        return (c.last_synced_at,
                sorted((t.id, t.name, str(t.columns)) for t in tables),
                sorted((t.id, t.datasource_id, t.connection_table_id, t.name, t.is_active, str(t.columns)) for t in agents))


async def refresh(cid):
    async with async_session_maker() as db:
        try:
            await ConnectionService().refresh_schema(db, await db.get(Connection, cid))
        except BaseException:
            # Even a caller that attempts to commit after catching a refresh
            # failure must not be able to publish its half-written catalog.
            await db.commit()
            raise


@pytest.mark.parametrize("signal", ["unreadable_models", "unread_prefixes", "unread_types", "exception", "cancel", "explicit_partial", "no_snapshot", "task_cancel"])
def test_incomplete_discovery_preserves_catalog_and_all_agent_selections(
    monkeypatch, create_user, login_user, whoami, signal,
):
    user = create_user()
    org = whoami(login_user(user["email"], user["password"]))["organizations"][0]["id"]
    cid = asyncio.run(seed(org))
    before = asyncio.run(snapshot(cid))

    async def discover(self, **kwargs):
        if signal == "exception":
            raise TimeoutError("catalog page timed out")
        if signal == "task_cancel":
            raise asyncio.CancelledError()
        if signal == "cancel":
            from app.data_sources.clients.progress import IndexingCancelled
            raise IndexingCancelled()
        if signal == "explicit_partial":
            return DiscoveryResult([table("beta", "changed")], complete=False)
        if signal == "no_snapshot":
            return None
        return [table("beta", "changed"), table("new_table")]

    monkeypatch.setattr(SqliteClient, "aget_schemas", discover)
    if signal == "unreadable_models":
        monkeypatch.setattr(SqliteClient, "index_stats", lambda self: {
            "unreadable_datasets": [{"datasetId": "unavailable-model", "reason": "timeout"}]})
    if signal == "unread_prefixes":
        monkeypatch.setattr(SqliteClient, "unindexed_table_prefixes", lambda self: ["unavailable"])
    if signal == "unread_types":
        monkeypatch.setattr(SqliteClient, "unindexed_entry_types", lambda self: ["routine"])
    error = asyncio.CancelledError if signal == "task_cancel" else IndexingCancelled if signal == "cancel" else (
        HTTPException if signal == "exception" else IncompleteDiscoveryError)
    with pytest.raises(error):
        asyncio.run(refresh(cid))
    assert asyncio.run(snapshot(cid)) == before



@pytest.mark.parametrize("fault", ["database", "cancel_between_agents", "cancel_before_commit"])
def test_publication_failure_rolls_back_every_agent_and_catalog(
    monkeypatch, create_user, login_user, whoami, fault,
):
    from sqlalchemy import event
    from sqlalchemy.engine import Engine
    from app.data_sources.clients.progress import IndexingCancelled

    user = create_user()
    org = whoami(login_user(user["email"], user["password"]))["organizations"][0]["id"]
    cid = asyncio.run(seed(org))
    before = asyncio.run(snapshot(cid))

    async def discover(self, **kwargs):
        return [table("alpha", "changed"), table("new_table")]

    monkeypatch.setattr(SqliteClient, "aget_schemas", discover)
    inserts = 0
    syncs = 0

    def fail_write(conn, cursor, statement, parameters, context, executemany):
        nonlocal inserts
        if statement.lstrip().startswith("INSERT INTO datasource_tables"):
            inserts += 1
            if inserts == 2:
                raise RuntimeError("injected database write failure")

    def progress(phase, *args):
        nonlocal syncs
        if phase == "syncing_agents":
            syncs += 1
            if fault == "cancel_between_agents" and syncs == 2:
                raise IndexingCancelled()
        if fault == "cancel_before_commit" and phase == "saving" and syncs == 2:
            raise IndexingCancelled()

    async def run():
        async with async_session_maker() as db:
            try:
                await ConnectionService().refresh_schema(db, await db.get(Connection, cid), progress_callback=progress)
            except BaseException:
                await db.commit()
                raise

    if fault == "database":
        event.listen(Engine, "before_cursor_execute", fail_write)
    try:
        with pytest.raises((RuntimeError, IndexingCancelled)):
            asyncio.run(run())
    finally:
        if fault == "database":
            event.remove(Engine, "before_cursor_execute", fail_write)
    assert asyncio.run(snapshot(cid)) == before


@pytest.mark.parametrize("empty", [False, True])
def test_complete_discovery_publishes_to_all_agents_and_removes_genuine_deletions(
    monkeypatch, create_user, login_user, whoami, empty,
):
    user = create_user()
    org = whoami(login_user(user["email"], user["password"]))["organizations"][0]["id"]
    cid = asyncio.run(seed(org))
    before = asyncio.run(snapshot(cid))

    async def discover(self, **kwargs):
        return DiscoveryResult([], complete=True) if empty else [table("alpha", "changed"), table("new_table")]

    monkeypatch.setattr(SqliteClient, "aget_schemas", discover)
    asyncio.run(refresh(cid))
    after = asyncio.run(snapshot(cid))
    assert after[0] != before[0]
    if empty:
        assert after[1:] == ([], [])
    else:
        assert {t[1] for t in after[1]} == {"alpha", "new_table"}
        assert len(after[2]) == 4
        old_active = {t[0] for t in before[2] if t[4]}
        assert {t[0] for t in after[2] if t[4]} == old_active
        assert all("changed" in t[5] for t in after[2] if t[3] == "alpha")
        # A subsequent identical refresh preserves IDs and activation too.
        asyncio.run(refresh(cid))
        assert asyncio.run(snapshot(cid))[1:] == after[1:]


@pytest.mark.parametrize("fault", ["partial", "database"])
def test_personal_connection_refresh_is_atomic_across_linked_agents(
    monkeypatch, create_user, login_user, whoami, fault,
):
    from sqlalchemy import event
    from sqlalchemy.engine import Engine
    from app.models.user import User
    from app.models.user_data_source_overlay import UserDataSourceTable, UserDataSourceColumn
    from app.services.data_source_service import DataSourceService

    account = create_user()
    profile = whoami(login_user(account["email"], account["password"]))
    cid = asyncio.run(seed(profile["organizations"][0]["id"]))
    changed = False

    async def discover(self, **kwargs):
        if changed and fault == "partial":
            return DiscoveryResult([table("beta", "changed")], complete=False)
        return [table("alpha"), table("beta")] if not changed else [table("beta", "changed"), table("new_table")]

    monkeypatch.setattr(SqliteClient, "aget_schemas", discover)

    async def run():
        async with async_session_maker() as db:
            try:
                await DataSourceService().refresh_user_connection_catalog(
                    db, await db.get(Connection, cid), await db.get(User, profile["id"]))
            except BaseException:
                await db.commit()
                raise

    async def overlays():
        async with async_session_maker() as db:
            rows = (await db.execute(select(UserDataSourceTable).where(
                UserDataSourceTable.connection_id == cid))).scalars().all()
            columns = (await db.execute(select(UserDataSourceColumn).join(
                UserDataSourceTable, UserDataSourceColumn.user_data_source_table_id == UserDataSourceTable.id
            ).where(UserDataSourceTable.connection_id == cid))).scalars().all()
            return (sorted((r.id, r.table_name, r.data_source_table_id, r.status, r.is_accessible) for r in rows),
                    sorted((r.id, r.column_name, r.is_accessible) for r in columns))

    asyncio.run(run())
    before, overlay_before = asyncio.run(snapshot(cid)), asyncio.run(overlays())
    changed = True
    inserts = 0

    def fail_write(conn, cursor, statement, parameters, context, executemany):
        nonlocal inserts
        if statement.lstrip().startswith("INSERT INTO user_data_source_tables"):
            inserts += 1
            if inserts == 2:
                raise RuntimeError("injected overlay write failure")

    if fault == "database":
        event.listen(Engine, "before_cursor_execute", fail_write)
    try:
        with pytest.raises(IncompleteDiscoveryError if fault == "partial" else RuntimeError):
            asyncio.run(run())
    finally:
        if fault == "database":
            event.remove(Engine, "before_cursor_execute", fail_write)
    assert asyncio.run(snapshot(cid)) == before
    assert asyncio.run(overlays()) == overlay_before


def test_background_incomplete_discovery_is_failed_and_records_connection_error(
    monkeypatch, test_client, create_user, login_user, whoami, create_connection,
):
    import threading
    import time
    from pathlib import Path
    from sqlalchemy import event
    from sqlalchemy.orm import Session

    broken = False
    def discover(self, **kwargs):
        items = [table("alpha"), table("beta")]
        return DiscoveryResult(items[:1], complete=False) if broken else items
    monkeypatch.setattr(SqliteClient, "get_schemas", discover)
    user = create_user()
    token = login_user(user["email"], user["password"])
    org = whoami(token)["organizations"][0]["id"]
    headers = {"Authorization": f"Bearer {token}", "X-Organization-Id": org}
    committed = threading.Event()
    def on_commit(session):
        committed.set()
    event.listen(Session, "after_commit", on_commit)

    def terminal(cid):
        deadline = time.monotonic() + 15
        while True:
            committed.clear()
            response = test_client.get(f"/api/connections/{cid}/indexing", headers=headers)
            if response.status_code == 200 and response.json()["status"] in {"completed", "failed", "cancelled"}:
                return response.json()
            remaining = deadline - time.monotonic()
            assert remaining > 0 and committed.wait(remaining), "Indexing did not finish"

    try:
        connection = create_connection(
            name=str(uuid4()), type="sqlite",
            config={"database": str(Path("tests/config/chinook.sqlite").resolve())},
            user_token=token, org_id=org,
        )
        cid = connection["id"]
        assert terminal(cid)["status"] == "completed"
        before = asyncio.run(snapshot(cid))
        broken = True
        response = test_client.post(f"/api/connections/{cid}/reindex", headers=headers)
        assert response.status_code == 200, response.text
        job = terminal(cid)
        assert job["status"] == "failed"
        assert job["error"]
        assert asyncio.run(snapshot(cid)) == before
        async def connection_error():
            async with async_session_maker() as db:
                return (await db.get(Connection, cid)).last_reindex_error
        assert asyncio.run(connection_error())
    finally:
        event.remove(Session, "after_commit", on_commit)


def test_unattested_empty_legacy_result_preserves_existing_catalog(
    monkeypatch, create_user, login_user, whoami,
):
    account = create_user()
    org = whoami(login_user(account["email"], account["password"]))["organizations"][0]["id"]
    cid = asyncio.run(seed(org))
    before = asyncio.run(snapshot(cid))
    async def discover(self, **kwargs):
        return []
    monkeypatch.setattr(SqliteClient, "aget_schemas", discover)
    with pytest.raises(IncompleteDiscoveryError):
        asyncio.run(refresh(cid))
    assert asyncio.run(snapshot(cid)) == before


def test_requested_auto_selection_participates_in_callers_transaction(
    create_user, login_user, whoami,
):
    from app.services.data_source_service import DataSourceService
    from sqlalchemy import update
    account = create_user()
    org = whoami(login_user(account["email"], account["password"]))["organizations"][0]["id"]
    cid = asyncio.run(seed(org))

    async def run():
        async with async_session_maker() as db:
            # Historical baseline with no selected tables; request a positive
            # onboarding limit through the public sync service.
            agent = (await db.execute(select(DataSource).join(domain_connection,
                domain_connection.c.data_source_id == DataSource.id).where(
                domain_connection.c.connection_id == cid))).scalars().first()
            agent_id = str(agent.id)
            await db.execute(update(DataSourceTable).where(
                DataSourceTable.datasource_id == agent_id).values(is_active=False))
            await db.commit()
            await DataSourceService().sync_domain_tables_from_connection(
                db, agent, await db.get(Connection, cid), max_auto_select=1, commit=False)
            selected = (await db.execute(select(DataSourceTable.id).where(
                DataSourceTable.datasource_id == agent_id, DataSourceTable.is_active.is_(True)))).scalars().all()
            assert len(selected) == 1
            await db.rollback()
        async with async_session_maker() as db:
            selected = (await db.execute(select(DataSourceTable.id).where(
                DataSourceTable.datasource_id == agent_id, DataSourceTable.is_active.is_(True)))).scalars().all()
            assert selected == []
    asyncio.run(run())
