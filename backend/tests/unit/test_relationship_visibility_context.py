"""Relationships the agent manager hid (DataSourceTable.excluded_fks) never
reach the agent's schema context — on the shared-credential path AND on the
per-user (user_required) overlay path — while every other edge, and the
hidden edge's own column, stays.
"""
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

import main  # noqa: F401 — registers all mappers
from app.models.base import BaseSchema
from app.models.organization import Organization
from app.models.user import User
from app.models.data_source import DataSource
from app.models.connection import Connection
from app.models.connection_table import ConnectionTable
from app.models.datasource_table import DataSourceTable
from app.models.user_data_source_overlay import UserDataSourceTable, UserDataSourceColumn
from app.ai.context.builders.schema_context_builder import SchemaContextBuilder


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(BaseSchema.metadata.create_all)
    maker = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as session:
        yield session
    await engine.dispose()


def _col(n, d="int"):
    return {"name": n, "dtype": d}


def _fk(col, ref_table, ref_col):
    return {"column": _col(col), "references_name": ref_table, "references_column": _col(ref_col)}


def _hide(col, ref_table, ref_col):
    return {"column": col, "references_name": ref_table, "references_column": ref_col}


async def _seed(db, auth_policy):
    org = Organization(name="o")
    db.add(org)
    await db.flush()
    ds = DataSource(name="agent", organization_id=str(org.id), is_active=True)
    db.add(ds)
    await db.flush()
    conn = Connection(name="wh", type="postgresql", config={}, is_active=True,
                      auth_policy=auth_policy, organization_id=str(org.id))
    conn.data_sources.append(ds)
    db.add(conn)
    await db.flush()
    await db.refresh(ds, attribute_names=["connections"])
    return org, ds, conn


async def _table(db, ds, conn, name, cols, *, fks=(), excluded_fks=None):
    ct = ConnectionTable(name=name, connection_id=str(conn.id), columns=cols, pks=[], fks=list(fks))
    db.add(ct)
    await db.flush()
    t = DataSourceTable(name=name, datasource_id=str(ds.id), connection_table_id=str(ct.id),
                        is_active=True, columns=cols, pks=[], fks=list(fks),
                        excluded_fks=excluded_fks)
    db.add(t)
    await db.flush()
    return t


def _edges(table):
    return {(fk.column.name, fk.references_name, fk.references_column.name) for fk in (table.fks or [])}


async def _star(db, ds, conn, excluded_fks):
    """orders -> customers, orders -> stores, plus a two-column key to shipments."""
    for name in ("customers", "stores", "shipments"):
        await _table(db, ds, conn, name, [_col("id"), _col("region_id")])
    return await _table(
        db, ds, conn, "orders",
        [_col("id"), _col("customer_id"), _col("store_id"), _col("ship_id"), _col("ship_region")],
        fks=[_fk("customer_id", "customers", "id"), _fk("store_id", "stores", "id"),
             _fk("ship_id", "shipments", "id"), _fk("ship_region", "shipments", "region_id")],
        excluded_fks=excluded_fks,
    )


async def _render(db, org, ds, user=None):
    ctx = await SchemaContextBuilder(db, [ds], org, None, user=user).build(with_stats=False)
    tables = {t.name: t for d in ctx.data_sources for t in (d.tables or [])}
    return tables, ctx.render_combined(top_k_per_ds=50, index_limit=200)


def _mentions_edge(rendered, col, ref, ref_col):
    r = rendered.lower()
    return (f"foreign key ({col}) references {ref}({ref_col})" in r
            or f'<fk column="{col}" ref_table="{ref}" ref_column="{ref_col}"' in r)


ALL = {("customer_id", "customers", "id"), ("store_id", "stores", "id"),
       ("ship_id", "shipments", "id"), ("ship_region", "shipments", "region_id")}


@pytest.mark.asyncio
@pytest.mark.parametrize("hidden", [
    [("customer_id", "customers", "id")],
    [("STORE_ID", "Stores", "ID")],  # identifier case must not re-expose an edge
    [("ship_id", "shipments", "id")],  # one pair of a composite key, not the other
    [("customer_id", "customers", "id"), ("store_id", "stores", "id")],
])
async def test_hidden_relationships_absent_from_shared_connection_context(db, hidden):
    org, ds, conn = await _seed(db, "system_only")
    await _star(db, ds, conn, [_hide(*h) for h in hidden])
    await db.commit()

    tables, rendered = await _render(db, org, ds)
    hidden_l = {tuple(p.lower() for p in h) for h in hidden}
    expected = {e for e in ALL if e not in hidden_l}
    assert _edges(tables["orders"]) == expected
    # The edge goes, its column stays.
    assert {c.name for c in tables["orders"].columns} >= {h[0] for h in hidden_l}
    for edge in hidden_l:
        assert not _mentions_edge(rendered, *edge)
    for edge in expected:  # control: the check would see an edge that is there
        assert _mentions_edge(rendered, *edge)


@pytest.mark.asyncio
async def test_no_hidden_relationships_keeps_every_edge(db):
    org, ds, conn = await _seed(db, "system_only")
    await _star(db, ds, conn, [])
    await db.commit()
    tables, _ = await _render(db, org, ds)
    assert _edges(tables["orders"]) == ALL


@pytest.mark.asyncio
async def test_per_user_context_drops_hidden_relationship(db, monkeypatch):
    org, ds, conn = await _seed(db, "user_required")
    canonical = {}
    for name in ("customers", "stores"):
        canonical[name] = await _table(db, ds, conn, name, [_col("id")])
    canonical["orders"] = await _table(
        db, ds, conn, "orders", [_col("id"), _col("customer_id"), _col("store_id")],
        fks=[_fk("customer_id", "customers", "id"), _fk("store_id", "stores", "id")],
        excluded_fks=[_hide("store_id", "stores", "id")],
    )
    user = User(name="u", email="u@x.com", hashed_password="x")
    db.add(user)
    await db.flush()
    for name, row in canonical.items():
        ot = UserDataSourceTable(data_source_id=str(ds.id), user_id=str(user.id),
                                 connection_id=str(conn.id), table_name=name,
                                 data_source_table_id=str(row.id),
                                 is_accessible=True, status="accessible")
        db.add(ot)
        await db.flush()
        for c in row.columns:
            db.add(UserDataSourceColumn(user_data_source_table_id=str(ot.id), column_name=c["name"],
                                        is_accessible=True, data_type="int"))
    await db.commit()

    from app.services.data_source_service import DataSourceService

    async def _delegated(self, db, data_source, current_user):
        return [], [str(c.id) for c in data_source.connections], []

    monkeypatch.setattr(DataSourceService, "classify_connection_access", _delegated)

    tables, _ = await _render(db, org, ds, user=user)
    assert _edges(tables["orders"]) == {("customer_id", "customers", "id")}


@pytest.mark.asyncio
async def test_agent_facing_get_schemas_hides_but_management_read_keeps(db):
    org, ds, conn = await _seed(db, "system_only")
    await _star(db, ds, conn, [_hide("customer_id", "customers", "id")])
    await db.commit()
    agent = {t.name: t for t in await ds.get_schemas(db=db)}
    manage = {t.name: t for t in await ds.get_schemas(db=db, include_inactive=True)}
    assert _edges(agent["orders"]) == ALL - {("customer_id", "customers", "id")}
    assert _edges(manage["orders"]) == ALL


@pytest.mark.asyncio
async def test_saving_hidden_relationships_validates_against_the_table(db):
    from app.services.data_source_service import DataSourceService

    org, ds, conn = await _seed(db, "system_only")
    orders = await _star(db, ds, conn, None)
    await db.commit()

    svc = DataSourceService()
    res = await svc.update_tables_status_delta(
        db=db, data_source_id=str(ds.id), organization=org,
        excluded_fks={str(orders.id): [
            _hide("Customer_ID", "CUSTOMERS", "Id"),  # case-insensitive
            _hide("customer_id", "customers", "id"),  # duplicate
            _hide("customer_id", "customers", "missing"),  # not an edge of this table
            {"column": "store_id"},  # malformed
        ]},
    )
    assert res.relationships_updated_count == 1
    await db.refresh(orders)
    assert orders.excluded_fks == [_hide("customer_id", "customers", "id")]

    res = await svc.update_tables_status_delta(
        db=db, data_source_id=str(ds.id), organization=org, excluded_fks={str(orders.id): []},
    )
    assert res.relationships_updated_count == 1
    await db.refresh(orders)
    assert orders.excluded_fks is None


@pytest.mark.asyncio
async def test_prompt_table_drops_hidden_relationship(db):
    """DataSourceTable.to_prompt_table — the per-table view mentions and
    tools read — applies the same deny-list as the schema context."""
    from sqlalchemy.orm import selectinload
    from sqlalchemy import select

    org, ds, conn = await _seed(db, "system_only")
    orders = await _star(db, ds, conn, [_hide("store_id", "stores", "id")])
    await db.commit()
    row = (await db.execute(
        select(DataSourceTable).options(selectinload(DataSourceTable.connection_table))
        .where(DataSourceTable.id == orders.id)
    )).scalar_one()
    assert _edges(row.to_prompt_table()) == ALL - {("store_id", "stores", "id")}
