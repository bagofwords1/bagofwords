"""A soft-deleted custom table must not be re-synced onto agents.

Deleting a custom table (KIND_BOW) drops it from the catalog and removes its
agent activations, but the row survives with ``deleted_at`` set. A later schema
reload runs ``sync_domain_tables_from_connection``, which rebuilds each agent's
``DataSourceTable`` rows from the connection's ``ConnectionTable`` catalog. If
that rebuild does not skip deleted rows, it re-creates an activation for a
relation the list/edit paths already hide (they filter ``deleted_at``), leaving
an inactive, un-editable, un-deletable ghost row on every agent.

The contract: the sync re-creates activations for live tables only — a live
regular table and a live custom table — and never for a deleted one.
"""
import asyncio
from datetime import datetime
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.dependencies import async_session_maker
from app.models.connection import Connection
from app.models.connection_table import ConnectionTable, KIND_BOW, KIND_TABLE
from app.models.data_source import DataSource
from app.models.datasource_table import DataSourceTable
from app.models.domain_connection import domain_connection
from app.services.data_source_service import DataSourceService

pytestmark = pytest.mark.e2e


def _cols(column="id"):
    return {"columns": [{"name": column, "dtype": "int"}], "pks": [], "fks": []}


async def _seed(org_id):
    """A connection and one agent, plus three catalog rows: a live regular
    table, a live custom table, and a soft-deleted custom table with no
    activation (the post-delete state). Returns (data_source_id, connection_id,
    deleted_table_id)."""
    async with async_session_maker() as db:
        conn = Connection(name=str(uuid4()), type="sqlite", config={"database": ":memory:"},
                          organization_id=org_id, auth_policy="system_only",
                          last_synced_at=datetime(2025, 1, 1))
        db.add(conn)
        await db.flush()

        ds = DataSource(name=str(uuid4()), organization_id=org_id, is_active=True)
        db.add(ds)
        await db.flush()
        await db.execute(domain_connection.insert().values(data_source_id=ds.id, connection_id=conn.id))

        live_regular = ConnectionTable(connection_id=conn.id, name="orders",
                                       kind=KIND_TABLE, no_rows=0, **_cols())
        live_custom = ConnectionTable(connection_id=conn.id, name="orders_cache",
                                      kind=KIND_BOW, no_rows=0, **_cols())
        deleted_custom = ConnectionTable(connection_id=conn.id, name="invoices_cache",
                                         kind=KIND_BOW, no_rows=0,
                                         deleted_at=datetime(2025, 1, 2), **_cols())
        db.add_all([live_regular, live_custom, deleted_custom])
        await db.flush()
        deleted_id = deleted_custom.id
        await db.commit()
        return str(ds.id), str(conn.id), str(deleted_id)


async def _sync_then_read(data_source_id, connection_id):
    async with async_session_maker() as db:
        ds = await db.get(DataSource, data_source_id)
        conn = await db.get(Connection, connection_id)
        await DataSourceService().sync_domain_tables_from_connection(
            db, ds, conn, max_auto_select=None, commit=True,
        )
        rows = (await db.execute(
            select(DataSourceTable).where(DataSourceTable.datasource_id == data_source_id)
        )).scalars().all()
        return {r.name: r.connection_table_id for r in rows}


def test_sync_skips_deleted_custom_table_but_keeps_live_ones(
    create_user, login_user, whoami,
):
    user = create_user()
    org = whoami(login_user(user["email"], user["password"]))["organizations"][0]["id"]
    ds_id, conn_id, deleted_id = asyncio.run(_seed(org))

    activated = asyncio.run(_sync_then_read(ds_id, conn_id))

    # The live tables are synced onto the agent...
    assert "orders" in activated
    assert "orders_cache" in activated
    # ...and the soft-deleted custom table is not — neither by name nor by a
    # dangling link to its row.
    assert "invoices_cache" not in activated
    assert deleted_id not in activated.values()
