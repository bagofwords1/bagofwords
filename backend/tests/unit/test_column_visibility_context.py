"""Columns the agent manager hid (DataSourceTable.excluded_columns) never
reach the agent's schema context — on the shared-credential path AND on the
per-user (user_required) overlay path, where the result is the intersection
of the user's own access and the agent's selection. Keys referencing a hidden
column are dropped too, so it cannot come back through DDL.
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


def _col(n, d="text"):
    return {"name": n, "dtype": d}


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


async def _table(db, ds, conn, name, cols, *, pks=(), fks=(), excluded=None):
    ct = ConnectionTable(name=name, connection_id=str(conn.id), columns=cols,
                         pks=list(pks), fks=list(fks))
    db.add(ct)
    await db.flush()
    t = DataSourceTable(name=name, datasource_id=str(ds.id), connection_table_id=str(ct.id),
                        is_active=True, columns=cols, pks=list(pks), fks=list(fks),
                        excluded_columns=excluded)
    db.add(t)
    await db.flush()
    return t


def _fk(col, ref_table, ref_col):
    return {"column": _col(col), "references_name": ref_table, "references_column": _col(ref_col)}


async def _render(db, org, ds, user=None):
    ctx = await SchemaContextBuilder(db, [ds], org, None, user=user).build(with_stats=False)
    tables = {t.name: t for d in ctx.data_sources for t in (d.tables or [])}
    return tables, ctx.render_combined(top_k_per_ds=50, index_limit=200)


@pytest.mark.asyncio
@pytest.mark.parametrize("hidden", [["ssn"], ["SSN", "email"], ["customer_id"]])
async def test_hidden_columns_absent_from_shared_connection_context(db, hidden):
    org, ds, conn = await _seed(db, "system_only")
    await _table(db, ds, conn, "customers",
                 [_col("customer_id", "int"), _col("email"), _col("ssn"), _col("country")],
                 pks=[_col("customer_id", "int")], excluded=hidden)
    await _table(db, ds, conn, "orders",
                 [_col("order_id", "int"), _col("customer_id", "int"), _col("total", "numeric")],
                 fks=[_fk("customer_id", "customers", "customer_id")])
    await db.commit()

    tables, rendered = await _render(db, org, ds)
    hidden_l = {h.lower() for h in hidden}
    cust_cols = {c.name.lower() for c in tables["customers"].columns}
    assert cust_cols == {"customer_id", "email", "ssn", "country"} - hidden_l
    assert not {c.name.lower() for c in (tables["customers"].pks or [])} & hidden_l
    # An inbound edge into a hidden column is dropped from the OTHER table too.
    inbound = [fk for fk in (tables["orders"].fks or [])
               if fk.references_column.name.lower() in hidden_l]
    assert inbound == []
    # Other tables are untouched.
    assert {c.name for c in tables["orders"].columns} == {"order_id", "customer_id", "total"}
    for h in hidden_l - {"customer_id"}:  # customer_id legitimately lives on orders
        assert h not in rendered.lower()


@pytest.mark.asyncio
async def test_no_exclusions_keeps_every_column(db):
    org, ds, conn = await _seed(db, "system_only")
    cols = [_col("a"), _col("b"), _col("c")]
    await _table(db, ds, conn, "t", cols, excluded=[])
    await db.commit()
    tables, _ = await _render(db, org, ds)
    assert [c.name for c in tables["t"].columns] == ["a", "b", "c"]


@pytest.mark.asyncio
async def test_per_user_context_is_intersection_of_user_access_and_agent_selection(db, monkeypatch):
    org, ds, conn = await _seed(db, "user_required")
    canonical = await _table(db, ds, conn, "sales",
                             [_col("id"), _col("region"), _col("amount"), _col("margin")],
                             excluded=["margin"])
    user = User(name="u", email="u@x.com", hashed_password="x")
    db.add(user)
    await db.flush()
    ot = UserDataSourceTable(data_source_id=str(ds.id), user_id=str(user.id),
                             connection_id=str(conn.id), table_name="sales",
                             data_source_table_id=str(canonical.id),
                             is_accessible=True, status="accessible")
    db.add(ot)
    await db.flush()
    # The user's grants: no `amount`; can reach `margin` but the agent hides it.
    for name, ok in (("id", True), ("region", True), ("amount", False), ("margin", True)):
        db.add(UserDataSourceColumn(user_data_source_table_id=str(ot.id), column_name=name,
                                    is_accessible=ok, data_type="text"))
    await db.commit()

    from app.services.data_source_service import DataSourceService

    async def _delegated(self, db, data_source, current_user):
        return [], [str(c.id) for c in data_source.connections], []

    monkeypatch.setattr(DataSourceService, "classify_connection_access", _delegated)

    tables, rendered = await _render(db, org, ds, user=user)
    assert {c.name for c in tables["sales"].columns} == {"id", "region"}
    assert "margin" not in rendered


@pytest.mark.asyncio
async def test_agent_facing_get_schemas_hides_but_management_read_keeps(db):
    org, ds, conn = await _seed(db, "system_only")
    await _table(db, ds, conn, "t", [_col("a"), _col("secret")], excluded=["secret"])
    await db.commit()
    agent = await ds.get_schemas(db=db)
    manage = await ds.get_schemas(db=db, include_inactive=True)
    assert [c.name for c in agent[0].columns] == ["a"]
    assert [c.name for c in manage[0].columns] == ["a", "secret"]
    assert manage[0].excluded_columns == ["secret"]


@pytest.mark.asyncio
async def test_selection_change_drops_cached_agent_schema(db):
    """The planner serves schema context from a per-org cache. A column (or
    table) selection change must reach the very next prompt, not wait out the
    cache TTL."""
    from app.ai.context import context_hub
    from app.services.data_source_service import DataSourceService

    org, ds, conn = await _seed(db, "system_only")
    t = await _table(db, ds, conn, "t", [_col("a"), _col("secret")])
    await db.commit()
    other_org_key = ("some-other-org", (), None, None)
    mine = (str(org.id), (str(ds.id),), None, None)
    context_hub._SCHEMA_CACHE[mine] = (0.0, "stale")
    context_hub._SCHEMA_CACHE[other_org_key] = (0.0, "other")
    try:
        await DataSourceService().update_tables_status_delta(
            db=db, data_source_id=str(ds.id), organization=org,
            excluded_columns={str(t.id): ["secret"]},
        )
        assert mine not in context_hub._SCHEMA_CACHE
        assert other_org_key in context_hub._SCHEMA_CACHE  # scoped to this org
    finally:
        context_hub._SCHEMA_CACHE.pop(other_org_key, None)
        context_hub._SCHEMA_CACHE.pop(mine, None)
