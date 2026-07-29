"""User-contributed semantic models must stay QUERYABLE, not just visible.

A service principal gets 401 on every RLS-protected semantic model, so those
models can only enter the catalog through a user's own discovery — and such rows
carry NO `connection_table_id` (there is no service-principal ConnectionTable row
to link to).

`_attach_stored_table_metadata` gives the Power BI client the dataset GUIDs it
needs to address `executeQueries`. It used to INNER JOIN ConnectionTable, which
silently dropped every user-contributed row, with a whole-catalog fallback that
only fired when there were ZERO linked rows. So in a MIXED tenant — some models
the SP can index, some it cannot — the user-contributed ones resolved to nothing
and every query against them failed:

    ValueError: Could not resolve Power BI dataset for table 'rls_sales/Sales'

Caught live (docs/feedback-loops/powerbi-obo-rls.md): a member could see the
model in their catalog and had permission to query it, but the agent could not
execute against it.

Run:
    cd backend
    BOW_DATABASE_URL=sqlite:///db/app.db \
      python -m pytest tests/e2e/test_powerbi_user_contributed_query_metadata.py -v
"""
import asyncio
import uuid

from app.dependencies import async_session_maker
from app.models.connection import Connection
from app.models.connection_table import ConnectionTable
from app.models.data_source import DataSource
from app.models.datasource_table import DataSourceTable
from app.models.domain_connection import domain_connection
from app.models.organization import Organization
from app.models.user import User
from app.services.data_source_service import DataSourceService


def _run(coro):
    return asyncio.run(coro)


def _pbi_meta(ds_id, ws_id, model, table):
    return {"powerbi": {"datasetId": ds_id, "workspaceId": ws_id,
                        "datasetName": model, "tableName": table}}


class _Recorder:
    """Stands in for PowerBIClient — records what metadata it was handed."""

    def __init__(self):
        self.attached = None

    def attach_table_metadata(self, tables):
        self.attached = {t["name"]: t["metadata_json"] for t in tables}


async def _seed():
    """A delegated Power BI connection holding both row shapes: one model the
    service principal indexed (ConnectionTable-linked) and one only a user could
    see (unlinked)."""
    suffix = uuid.uuid4().hex[:8]
    async with async_session_maker() as db:
        org = Organization(name=f"RLS Org {suffix}")
        db.add(org)
        await db.flush()

        owner = User(name="Owner", email=f"owner-{suffix}@example.com", hashed_password="x",
                     is_active=True, is_superuser=False, is_verified=True)
        db.add(owner)
        await db.flush()

        conn = Connection(
            organization_id=org.id, name=f"PBI {suffix}", type="powerbi",
            config={}, auth_policy="user_required", allowed_user_auth_modes=["oauth"],
        )
        conn.encrypt_credentials({"tenant_id": "t", "client_id": "c", "client_secret": "s"})
        db.add(conn)
        await db.flush()

        ds = DataSource(name=f"PBI DS {suffix}", organization_id=org.id,
                        is_active=True, owner_user_id=owner.id)
        db.add(ds)
        await db.flush()
        await db.execute(domain_connection.insert().values(
            data_source_id=ds.id, connection_id=conn.id))

        # SP-indexed model: ConnectionTable row + linked DataSourceTable row.
        ct = ConnectionTable(name="SalesPush/Sales", connection_id=conn.id,
                             columns=[{"name": "id", "dtype": "int"}], pks=[], fks=[],
                             no_rows=0, metadata_json=_pbi_meta("ds-sp", "ws-sp", "SalesPush", "Sales"))
        db.add(ct)
        await db.flush()
        db.add(DataSourceTable(
            name="SalesPush/Sales", datasource_id=ds.id, connection_table_id=ct.id,
            is_active=True, metadata_json=_pbi_meta("ds-sp", "ws-sp", "SalesPush", "Sales"),
            columns=[{"name": "id", "dtype": "int"}], pks=[], fks=[], no_rows=0))

        # RLS model the SP cannot see — contributed by a user's own discovery,
        # so there is no ConnectionTable row and the link stays NULL.
        db.add(DataSourceTable(
            name="rls_sales/Sales", datasource_id=ds.id, connection_table_id=None,
            is_active=True,
            metadata_json={**_pbi_meta("ds-rls", "ws-rls", "rls_sales", "Sales"),
                           "discovered_by": "user"},
            columns=[{"name": "id", "dtype": "int"}], pks=[], fks=[], no_rows=0))

        await db.commit()
        return str(ds.id), str(conn.id)


def test_user_contributed_model_gets_query_metadata():
    ds_id, conn_id = _run(_seed())

    async def _check():
        async with async_session_maker() as db:
            from sqlalchemy import select
            from sqlalchemy.orm import selectinload
            ds = (await db.execute(
                select(DataSource).options(selectinload(DataSource.connections))
                .where(DataSource.id == ds_id))).scalar_one()
            conn = (await db.execute(
                select(Connection).where(Connection.id == conn_id))).scalar_one()
            rec = _Recorder()
            await DataSourceService()._attach_stored_table_metadata(db, rec, ds, conn)
            return rec.attached

    attached = _run(_check())

    # The SP-indexed model still resolves (no regression).
    assert "SalesPush/Sales" in attached
    assert attached["SalesPush/Sales"]["powerbi"]["datasetId"] == "ds-sp"

    # The user-contributed RLS model must resolve too — without this the client
    # raises "Could not resolve Power BI dataset" for every query against it.
    assert "rls_sales/Sales" in attached, (
        "user-contributed model missing from query metadata — it is visible in "
        "the catalog but unqueryable"
    )
    assert attached["rls_sales/Sales"]["powerbi"]["datasetId"] == "ds-rls"
    assert attached["rls_sales/Sales"]["powerbi"]["workspaceId"] == "ws-rls"
