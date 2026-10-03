"""An @table mention puts a table into the prompt only if the run's schema
context would show it.

The mention's object_id is whatever id the client sent, so it proves nothing
about access. MentionContextBuilder used to load that DataSourceTable row and
render its name and raw columns without checking the agent, the activation
flag, or the caller's per-user overlay. It now resolves the id through
SchemaContextBuilder, the same rules as the rest of the prompt.
"""
from datetime import datetime

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
from app.models.report import Report
from app.models.completion import Completion
from app.models.mention import Mention, MentionType
from app.models.user_data_source_overlay import UserDataSourceTable, UserDataSourceColumn
from app.ai.context.builders.mention_context_builder import MentionContextBuilder


COLS = (("id", "Integer"), ("region", "Text"), ("amount", "Number"), ("ssn", "Text"))


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(BaseSchema.metadata.create_all)
    maker = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as session:
        yield session
    await engine.dispose()


async def _agent(db, org, name, auth_policy="system_only"):
    ds = DataSource(name=name, organization_id=str(org.id), is_active=True)
    db.add(ds)
    await db.flush()
    conn = Connection(name=f"{name}-conn", type="postgresql", config={}, is_active=True,
                      auth_policy=auth_policy, organization_id=str(org.id))
    conn.data_sources.append(ds)
    db.add(conn)
    await db.flush()
    await db.refresh(ds, attribute_names=["connections"])
    return ds


async def _table(db, ds, name, active):
    t = DataSourceTable(
        name=name, datasource_id=str(ds.id), is_active=active,
        columns=[{"name": n, "dtype": d} for n, d in COLS], pks=[], fks=[],
    )
    db.add(t)
    await db.flush()
    return t


async def _seed(db):
    org = Organization(name="o")
    db.add(org)
    await db.flush()
    user = User(name="u", email="u@x.com", hashed_password="x")
    db.add(user)
    await db.flush()
    report = Report(title="r", slug=f"r-{datetime.utcnow().timestamp()}",
                    user_id=str(user.id), organization_id=str(org.id))
    db.add(report)
    await db.flush()
    return org, user, report


async def _mention_tables(db, org, user, report, run_agents, tables):
    """Mention every table in `tables` in one user turn; return what the
    builder puts in the prompt, keyed by mentioned id."""
    completion = Completion(report_id=str(report.id), role="user", prompt={"content": "q"},
                            completion={}, sigkill=datetime.utcnow(), user_id=str(user.id))
    db.add(completion)
    await db.flush()
    for t in tables:
        db.add(Mention(type=MentionType.TABLE, report_id=str(report.id), object_id=str(t.id),
                       mention_content=t.name, completion_id=str(completion.id)))
    await db.flush()
    section = await MentionContextBuilder(
        db, org, report, completion, user=user, data_sources=run_agents,
    ).build()
    return {item.id: item for item in section.tables}


@pytest.mark.asyncio
async def test_active_table_on_run_agent_is_rendered_with_real_columns(db):
    org, user, report = await _seed(db)
    ds = await _agent(db, org, "sales")
    orders = await _table(db, ds, "public.orders", active=True)

    shown = await _mention_tables(db, org, user, report, [ds], [orders])

    item = shown[str(orders.id)]
    assert item.table_name == "public.orders"
    assert item.data_source_name == "sales"
    # Previews read "name:dtype", not a stringified column dict.
    assert set(item.columns_preview) == {f"{n}:{d}" for n, d in COLS}


@pytest.mark.asyncio
async def test_only_visible_tables_survive_among_mixed_mentions(db):
    """One turn mentioning a visible table, an inactive table on the same
    agent, and an active table on an agent this run is not scoped to:
    only the visible one is rendered."""
    org, user, report = await _seed(db)
    ds = await _agent(db, org, "sales")
    other = await _agent(db, org, "hr")
    visible = await _table(db, ds, "public.orders", active=True)
    inactive = await _table(db, ds, "public.payroll_raw", active=False)
    foreign = await _table(db, other, "public.employees", active=True)

    shown = await _mention_tables(db, org, user, report, [ds], [visible, inactive, foreign])

    assert set(shown) == {str(visible.id)}


@pytest.mark.asyncio
async def test_unknown_table_id_is_dropped(db):
    org, user, report = await _seed(db)
    ds = await _agent(db, org, "sales")
    ghost = DataSourceTable(id="00000000-0000-0000-0000-000000000000", name="x",
                            datasource_id=str(ds.id))  # never persisted

    assert await _mention_tables(db, org, user, report, [ds], [ghost]) == {}


async def _delegated_overlay(db, ds, user, table, accessible_cols):
    ot = UserDataSourceTable(
        data_source_id=str(ds.id), user_id=str(user.id), table_name=table.name,
        is_accessible=True, status="accessible", data_source_table_id=str(table.id),
    )
    db.add(ot)
    await db.flush()
    for n, d in COLS:
        db.add(UserDataSourceColumn(user_data_source_table_id=str(ot.id), column_name=n,
                                    is_accessible=n in accessible_cols, data_type=d))
    await db.flush()


def _force_delegated(monkeypatch):
    """Every connection counts as one the user runs delegated on (see
    test_overlay_inactive_canonical_context._force_delegated)."""
    from app.services.data_source_service import DataSourceService

    async def _classify(self, db, data_source, current_user):
        conns = getattr(data_source, "connections", None) or []
        return [], [str(c.id) for c in conns], []

    monkeypatch.setattr(DataSourceService, "classify_connection_access", _classify)


@pytest.mark.asyncio
async def test_delegated_mention_lists_only_the_users_accessible_columns(db, monkeypatch):
    org, user, report = await _seed(db)
    ds = await _agent(db, org, "sales", auth_policy="user_required")
    orders = await _table(db, ds, "public.orders", active=True)
    await _delegated_overlay(db, ds, user, orders, accessible_cols={"id", "amount"})
    _force_delegated(monkeypatch)

    shown = await _mention_tables(db, org, user, report, [ds], [orders])

    assert set(shown[str(orders.id)].columns_preview) == {"id:Integer", "amount:Number"}


@pytest.mark.asyncio
async def test_delegated_table_outside_users_overlay_is_dropped(db, monkeypatch):
    """Activated on the agent, but the caller's own credentials do not reach
    it: the mention must not disclose the table or its columns."""
    org, user, report = await _seed(db)
    ds = await _agent(db, org, "sales", auth_policy="user_required")
    orders = await _table(db, ds, "public.orders", active=True)
    secret = await _table(db, ds, "public.salaries", active=True)
    await _delegated_overlay(db, ds, user, orders, accessible_cols={n for n, _ in COLS})
    _force_delegated(monkeypatch)

    shown = await _mention_tables(db, org, user, report, [ds], [orders, secret])

    assert set(shown) == {str(orders.id)}
