"""An agent's table selection is enforced on the clients it is handed.

`construct_clients` is the one place every agent path (chat, MCP, scheduled
runs) gets its data-source clients. Each SQL client must carry the agent's
activated tables, so generated code can read exactly those: an inactive table
on the same connection is refused, and toggling a table's activation changes
what can be queried without any other step.

Seeded with direct model writes, like the Power BI query-metadata test: the
API has no route that creates a connection over a local SQLite file together
with an indexed, partly-activated catalog.
"""
import asyncio
import sqlite3
import uuid

import pytest
from sqlalchemy import select, update
from sqlalchemy.orm import selectinload

from app.ai.code_execution.code_execution import StreamingCodeExecutor, TableAccessError
from app.dependencies import async_session_maker
from app.models.connection import Connection
from app.models.connection_table import ConnectionTable
from app.models.data_source import DataSource
from app.models.datasource_table import DataSourceTable
from app.models.domain_connection import domain_connection
from app.models.organization import Organization
from app.models.user import User
from app.services.data_source_service import DataSourceService

pytestmark = pytest.mark.e2e


def _run(coro):
    return asyncio.run(coro)


def _make_db(path, tables):
    con = sqlite3.connect(path)
    for name in tables:
        con.execute(f"CREATE TABLE {name} (id INTEGER PRIMARY KEY)")
        con.executemany(f"INSERT INTO {name} VALUES (?)", [(i,) for i in range(1, 4)])
    con.commit()
    con.close()


async def _seed(db_path, active, inactive):
    suffix = uuid.uuid4().hex[:8]
    async with async_session_maker() as db:
        org = Organization(name=f"Scope Org {suffix}")
        db.add(org)
        await db.flush()
        owner = User(name="Owner", email=f"owner-{suffix}@example.com", hashed_password="x",
                     is_active=True, is_superuser=False, is_verified=True)
        db.add(owner)
        await db.flush()

        conn = Connection(organization_id=org.id, name=f"wh-{suffix}", type="sqlite",
                          config={"database": str(db_path)}, auth_policy="system_only")
        conn.encrypt_credentials({})
        db.add(conn)
        await db.flush()
        ds = DataSource(name=f"agent-{suffix}", organization_id=org.id,
                        is_active=True, owner_user_id=owner.id)
        db.add(ds)
        await db.flush()
        await db.execute(domain_connection.insert().values(data_source_id=ds.id, connection_id=conn.id))

        for name, is_active in [(n, True) for n in active] + [(n, False) for n in inactive]:
            ct = ConnectionTable(name=name, connection_id=conn.id, columns=[{"name": "id", "dtype": "int"}],
                                 pks=[], fks=[], no_rows=3)
            db.add(ct)
            await db.flush()
            db.add(DataSourceTable(name=name, datasource_id=ds.id, connection_table_id=ct.id,
                                   is_active=is_active, columns=[{"name": "id", "dtype": "int"}],
                                   pks=[], fks=[], no_rows=3))
        await db.commit()
        return str(ds.id)


async def _clients(ds_id):
    async with async_session_maker() as db:
        ds = (await db.execute(
            select(DataSource).options(selectinload(DataSource.connections))
            .where(DataSource.id == ds_id))).scalar_one()
        return await DataSourceService().construct_clients(db, ds, None)


async def _set_active(ds_id, name, is_active):
    async with async_session_maker() as db:
        await db.execute(update(DataSourceTable)
                         .where(DataSourceTable.datasource_id == ds_id, DataSourceTable.name == name)
                         .values(is_active=is_active))
        await db.commit()


def _count(clients, table):
    key = next(k for k in clients if ":" in k)
    code = (
        "def generate_df(ds_clients, excel_files):\n"
        f"    return ds_clients[{key!r}].execute_query('SELECT COUNT(*) AS n FROM {table}')\n"
    )
    df, _, _ = StreamingCodeExecutor().execute_code(code=code, ds_clients=clients, excel_files=[])
    return int(df["n"].iloc[0])


@pytest.mark.parametrize("active,inactive", [
    (["events"], ["kno2_patients", "kno2_documents"]),
    (["orders", "customers"], ["payroll"]),
])
def test_only_activated_tables_are_queryable(tmp_path, active, inactive):
    path = tmp_path / "wh.db"
    _make_db(path, active + inactive)
    ds_id = _run(_seed(path, active, inactive))
    clients = _run(_clients(ds_id))

    for table in active:
        assert _count(clients, table) == 3
    for table in inactive:
        with pytest.raises(TableAccessError):
            _count(clients, table)


def test_toggling_activation_changes_what_can_be_queried(tmp_path):
    path = tmp_path / "wh.db"
    _make_db(path, ["events", "secret"])
    ds_id = _run(_seed(path, ["events"], ["secret"]))

    with pytest.raises(TableAccessError):
        _count(_run(_clients(ds_id)), "secret")

    _run(_set_active(ds_id, "secret", True))
    assert _count(_run(_clients(ds_id)), "secret") == 3

    _run(_set_active(ds_id, "events", False))
    with pytest.raises(TableAccessError):
        _count(_run(_clients(ds_id)), "events")


def test_unindexed_data_source_is_not_restricted(tmp_path):
    # No table rows at all: there is no selection to enforce, so the legacy
    # behaviour (query whatever the connection can read) is kept.
    path = tmp_path / "wh.db"
    _make_db(path, ["events"])
    ds_id = _run(_seed(path, [], []))
    assert _count(_run(_clients(ds_id)), "events") == 3
