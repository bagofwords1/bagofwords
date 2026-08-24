"""A delegated (user_required) source must not vanish from the agent's schema
context just because its canonical catalog is inactive.

Root cause (confirmed end-to-end against a live Power BI verify-rls dataset):
the org-level canonical catalog is indexed by the service principal, which by
design has NO access to a delegated dataset — so the canonical DataSourceTable
row is inactive (or absent). The overlay branch gated the user's own,
provably-accessible overlay table on that canonical is_active flag and the
active_only filter, so build() returned ZERO tables. The owner then could not
build any query (the model correctly reported "no tables available").

The overlay's is_accessible flag is the per-user access authority; this test
pins that an accessible overlay table surfaces even when its canonical row is
inactive.
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


async def _seed(db):
    org = Organization(name="o")
    user = User(name="u", email="u@x.com", hashed_password="x")
    db.add_all([org, user])
    await db.flush()
    ds = DataSource(name="PowerBI verify-rls",
                    organization_id=str(org.id), is_active=True)
    db.add(ds)
    await db.flush()
    conn = Connection(name="c", type="powerbi", config={}, is_active=True,
                      auth_policy="user_required", organization_id=str(org.id))
    conn.data_sources.append(ds)  # M:N via domain_connection
    db.add(conn)
    # Canonical row is INACTIVE — the service principal cannot see the
    # delegated dataset, exactly as observed against verify-rls.
    canon = DataSourceTable(
        name="rls_sales/Sales", datasource_id=str(ds.id), is_active=False,
        columns=[{"name": "id", "dtype": "Integer"},
                 {"name": "Region", "dtype": "Text"},
                 {"name": "Amount", "dtype": "Number"}],
        pks=[], fks=[],
    )
    db.add(canon)
    # The user's overlay: accessible.
    ot = UserDataSourceTable(
        data_source_id=str(ds.id), user_id=str(user.id),
        table_name="rls_sales/Sales", is_accessible=True, status="accessible",
    )
    db.add(ot)
    await db.flush()
    for cn, dt in (("id", "Integer"), ("Region", "Text"), ("Amount", "Number")):
        db.add(UserDataSourceColumn(user_data_source_table_id=str(ot.id),
                                    column_name=cn, is_accessible=True, data_type=dt))
    await db.flush()
    # connections must be loadable off the ds for _resolve_user_access / gating
    await db.refresh(ds, attribute_names=["connections"])
    return org, user, ds


@pytest.mark.asyncio
async def test_accessible_overlay_table_surfaces_despite_inactive_canonical(db, monkeypatch):
    org, user, ds = await _seed(db)

    builder = SchemaContextBuilder(db, [ds], org, None, user=user)
    # Isolate the overlay-rendering path under test: the user has proven
    # access (own delegated creds), so _resolve_user_access → 'user'.
    async def _user_access(_ds):
        return "user"
    monkeypatch.setattr(builder, "_resolve_user_access", _user_access)

    ctx = await builder.build(with_stats=False)

    tables = [getattr(t, "name", None)
              for dss in ctx.data_sources for t in (getattr(dss, "tables", []) or [])]
    assert "rls_sales/Sales" in tables, (
        "accessible overlay table dropped because its canonical row is inactive")

    # And the user's columns are carried through (from the overlay, enriched
    # by the canonical dtypes).
    cols = [c
            for dss in ctx.data_sources for t in (getattr(dss, "tables", []) or [])
            if getattr(t, "name", None) == "rls_sales/Sales"
            for c in (getattr(t, "columns", []) or [])]
    names = {getattr(c, "name", None) for c in cols}
    assert {"id", "Region", "Amount"} <= names
