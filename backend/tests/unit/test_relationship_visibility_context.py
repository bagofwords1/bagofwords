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


@pytest.mark.asyncio
@pytest.mark.parametrize("odd", [
    {"column": "store_id", "references_name": "stores", "references_column": "id"},  # flat
    {"column": None, "references_name": "stores", "references_column": {"name": "id"}},
    {"column": {"name": "store_id"}, "references_name": "stores"},  # no target column
])
async def test_save_tolerates_any_stored_fk_shape(db, odd):
    """An unexpected shape in a table's stored fks must not fail the save —
    which would also drop the table activations sent in the same request."""
    from app.services.data_source_service import DataSourceService

    org, ds, conn = await _seed(db, "system_only")
    orders = await _star(db, ds, conn, None)
    orders.fks = [*orders.fks, odd]
    orders.is_active = False
    await db.commit()

    res = await DataSourceService().update_tables_status_delta(
        db=db, data_source_id=str(ds.id), organization=org, activate=[str(orders.id)],
        excluded_fks={str(orders.id): [_hide("customer_id", "customers", "id")]},
    )
    assert res.activated_count == 1
    assert res.relationships_updated_count == 1
    await db.refresh(orders)
    assert orders.is_active is True
    assert orders.excluded_fks == [_hide("customer_id", "customers", "id")]


async def _rename(db, conn, old, new):
    """Rename a table at the source while it keeps its identity: the same
    ConnectionTable row comes back with a new name, and edges into it are
    re-written with the new name (what a Power BI re-crawl does)."""
    from sqlalchemy import select
    cts = (await db.execute(select(ConnectionTable).where(ConnectionTable.connection_id == str(conn.id)))).scalars().all()
    for ct in cts:
        if ct.name == old:
            ct.name = new
        ct.fks = [{**fk, "references_name": new} if fk.get("references_name") == old else fk for fk in (ct.fks or [])]
    await db.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize("renames", [
    [("customers", "clients")],
    [("customers", "Customers (v2)")],  # new spelling, different case
    [("customers", "tmp"), ("stores", "customers"), ("tmp", "stores")],  # swap
])
async def test_hidden_relationship_stays_hidden_after_target_rename(db, renames):
    from app.services.data_source_service import DataSourceService

    org, ds, conn = await _seed(db, "system_only")
    await _star(db, ds, conn, [_hide("customer_id", "customers", "id")])
    await db.commit()
    for old, new in renames:
        await _rename(db, conn, old, new)
    # Where each original target ends up after the renames.
    now = {"customers": "customers", "stores": "stores"}
    for old, new in renames:
        now = {k: (new if v == old else v) for k, v in now.items()}

    await DataSourceService().sync_domain_tables_from_connection(db, ds, conn)

    tables, rendered = await _render(db, org, ds)
    edges = _edges(tables["orders"])
    assert ("customer_id", now["customers"].lower(), "id") not in {(c, r.lower(), rc) for c, r, rc in edges}
    assert ("store_id", now["stores"], "id") in edges  # the edge that wasn't hidden stays
    assert not _mentions_edge(rendered, "customer_id", now["customers"].lower(), "id")


@pytest.mark.asyncio
async def test_rename_on_another_connection_leaves_namesake_alone(db):
    """Two connections on one agent can each have a `customers` table. A
    rename on one must not remap edges hidden on the other."""
    from app.services.data_source_service import DataSourceService

    org, ds, conn_a = await _seed(db, "system_only")
    conn_b = Connection(name="wh2", type="postgresql", config={}, is_active=True,
                        auth_policy="system_only", organization_id=str(org.id))
    conn_b.data_sources.append(ds)
    db.add(conn_b)
    await db.flush()
    await _star(db, ds, conn_a, None)
    b_cust = await _table(db, ds, conn_b, "customers", [_col("id")])
    b_orders = await _table(db, ds, conn_b, "b_orders", [_col("id"), _col("customer_id")],
                            fks=[_fk("customer_id", "customers", "id")],
                            excluded_fks=[_hide("customer_id", "customers", "id")])
    await db.commit()

    await _rename(db, conn_a, "customers", "clients")
    await DataSourceService().sync_domain_tables_from_connection(db, ds, conn_a)
    await db.refresh(b_orders)
    assert b_orders.excluded_fks == [_hide("customer_id", "customers", "id")]
    assert b_cust.name == "customers"


@pytest.mark.asyncio
async def test_rename_remaps_entry_stored_in_other_case(db):
    """An entry saved as sent (no canonical fks to take the spelling from)
    can differ in case from the table name; the remap matches it anyway."""
    from app.services.data_source_service import DataSourceService

    org, ds, conn = await _seed(db, "system_only")
    orders = await _star(db, ds, conn, [_hide("customer_id", "CUSTOMERS", "id")])
    await db.commit()
    await _rename(db, conn, "customers", "clients")
    await DataSourceService().sync_domain_tables_from_connection(db, ds, conn)
    await db.refresh(orders)
    assert orders.excluded_fks == [_hide("customer_id", "clients", "id")]


async def _two_sales(db, ds, conn, orders_fks, excluded):
    """Two rows that both still carry the old name `Sales` (the duplicate
    state a sync heals) whose connection tables now come back as
    `Sales (v2)` and `Sales (v3)`, plus an `orders` table pointing at them."""
    rows = {}
    for new in ("Sales (v2)", "Sales (v3)"):
        ct = ConnectionTable(name=new, connection_id=str(conn.id), columns=[_col("id"), _col("region_id")], pks=[], fks=[])
        db.add(ct)
        await db.flush()
        row = DataSourceTable(name="Sales", datasource_id=str(ds.id), connection_table_id=str(ct.id),
                              is_active=True, columns=ct.columns, pks=[], fks=[])
        db.add(row)
        rows[new] = row
    ct = ConnectionTable(name="orders", connection_id=str(conn.id),
                         columns=[_col("id"), _col("x_id"), _col("y_id")], pks=[], fks=orders_fks)
    db.add(ct)
    await db.flush()
    orders = DataSourceTable(name="orders", datasource_id=str(ds.id), connection_table_id=str(ct.id),
                             is_active=True, columns=ct.columns, pks=[], fks=[], excluded_fks=excluded)
    db.add(orders)
    await db.commit()
    return orders


@pytest.mark.asyncio
async def test_rename_collision_follows_the_rows_own_edges(db):
    """Two rows renamed away from the same old name: each hidden entry goes
    to the target its own edge now names, not to whichever rename came last."""
    from app.services.data_source_service import DataSourceService

    org, ds, conn = await _seed(db, "system_only")
    orders = await _two_sales(
        db, ds, conn,
        orders_fks=[_fk("x_id", "Sales (v2)", "id"), _fk("y_id", "Sales (v3)", "id")],
        excluded=[_hide("x_id", "Sales", "id")],
    )
    await DataSourceService().sync_domain_tables_from_connection(db, ds, conn)
    await db.refresh(orders)
    assert orders.excluded_fks == [_hide("x_id", "Sales (v2)", "id")]
    tables, _ = await _render(db, org, ds)
    assert _edges(tables["orders"]) == {("y_id", "Sales (v3)", "id")}


@pytest.mark.asyncio
async def test_rename_collision_left_alone_when_still_ambiguous(db):
    """Same column pair into both new names: no way to tell which one the
    manager meant, so the entry is not guessed."""
    from app.services.data_source_service import DataSourceService

    org, ds, conn = await _seed(db, "system_only")
    orders = await _two_sales(
        db, ds, conn,
        orders_fks=[_fk("x_id", "Sales (v2)", "id"), _fk("x_id", "Sales (v3)", "id")],
        excluded=[_hide("x_id", "Sales", "id")],
    )
    await DataSourceService().sync_domain_tables_from_connection(db, ds, conn)
    await db.refresh(orders)
    assert orders.excluded_fks == [_hide("x_id", "Sales", "id")]


def test_apply_column_exclusions_accepts_a_normalized_set():
    """filter_fks takes raw entries or a pre-built frozenset; so must
    apply_column_exclusions, or a normalized caller hides nothing."""
    from app.ai.prompt_formatters import Table, TableColumn, ForeignKey, apply_column_exclusions, excluded_fk_set

    def table():
        cid, cust_id = TableColumn(name="cid", dtype="int"), TableColumn(name="id", dtype="int")
        return Table(name="orders", columns=[cid], pks=[], fks=[
            ForeignKey(column=cid, references_name="customers", references_column=cust_id),
        ])

    hidden = [_hide("cid", "customers", "id")]
    for arg in (hidden, excluded_fk_set(hidden)):
        assert apply_column_exclusions(table(), None, arg).fks == []


@pytest.mark.asyncio
async def test_table_without_fks_stores_no_hidden_relationships(db):
    """With no canonical fks nothing can ever be filtered, so whatever is
    sent is dropped instead of kept as dead data."""
    from app.services.data_source_service import DataSourceService

    org, ds, conn = await _seed(db, "system_only")
    lone = await _table(db, ds, conn, "events", [_col("id"), _col("user_id")])
    await db.commit()
    res = await DataSourceService().update_tables_status_delta(
        db=db, data_source_id=str(ds.id), organization=org,
        excluded_fks={str(lone.id): [_hide("user_id", "users", "id"), _hide("x" * 5000, "y" * 5000, "z" * 5000)]},
    )
    assert res.relationships_updated_count == 0
    await db.refresh(lone)
    assert lone.excluded_fks is None
