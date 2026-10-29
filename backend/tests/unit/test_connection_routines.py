"""ConnectionService.refresh_routines syncs a connection's stored-procedure /
function catalog into connection_routines — additively to table indexing.

Contract under test (not the SQL, which is per-client and mocked at the driver
boundary): signatures are upserted, bodies are never stored, an authoritative
crawl prunes routines that vanished while a per-user crawl does not, a source
with no routines is a clean no-op, and RoutineFormatter renders signatures the
planner can read.
"""
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

import main  # noqa: F401 — registers all mappers
from app.models.base import BaseSchema
from app.models.organization import Organization
from app.models.connection import Connection
from app.models.connection_routine import ConnectionRoutine
from app.ai.prompt_formatters import Routine, RoutineParam, RoutineFormatter
from app.services.connection_service import ConnectionService


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(BaseSchema.metadata.create_all)
    maker = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as session:
        yield session
    await engine.dispose()


async def _connection(db):
    org = Organization(name="org")
    db.add(org)
    await db.flush()
    conn = Connection(name="mssql-conn", type="mssql", config={}, is_active=True,
                      organization_id=str(org.id))
    db.add(conn)
    await db.flush()
    return conn


class _FakeClient:
    """A data-source client stand-in: only the routine-discovery surface."""

    def __init__(self, routines, *, unsupported=False):
        self._routines = routines
        self._unsupported = unsupported

    async def aget_routines(self):
        if self._unsupported:
            raise NotImplementedError("no routines on this source")
        return self._routines


def _proc(name, **kw):
    return Routine(name=name, routine_type="procedure", **kw)


async def _rows(db, conn):
    res = await db.execute(
        select(ConnectionRoutine).where(
            ConnectionRoutine.connection_id == str(conn.id),
            ConnectionRoutine.deleted_at.is_(None),
        )
    )
    return {r.name: r for r in res.scalars().all()}


@pytest.mark.asyncio
async def test_catalog_is_upserted_with_signature_but_no_body(db):
    conn = await _connection(db)
    svc = ConnectionService()
    routines = [
        _proc("dbo.usp_load", params=[RoutineParam(name="@from", dtype="date"),
                                      RoutineParam(name="@to", dtype="date")]),
        Routine(name="dbo.fn_tax", routine_type="function",
                params=[RoutineParam(name="amount", dtype="decimal")], returns="decimal"),
    ]
    count = await svc.refresh_routines(db, conn, _FakeClient(routines), authoritative=True)

    assert count == 2
    assert svc.last_routine_count == 2
    rows = await _rows(db, conn)
    assert set(rows) == {"dbo.usp_load", "dbo.fn_tax"}
    assert rows["dbo.fn_tax"].routine_type == "function"
    assert rows["dbo.fn_tax"].returns == "decimal"
    assert len(rows["dbo.usp_load"].params) == 2
    # The body is deliberately never persisted — the model has no column for it.
    assert not hasattr(rows["dbo.usp_load"], "body")


@pytest.mark.asyncio
async def test_authoritative_crawl_prunes_vanished_routines(db):
    conn = await _connection(db)
    svc = ConnectionService()
    await svc.refresh_routines(
        db, conn, _FakeClient([_proc("dbo.a"), _proc("dbo.b")]), authoritative=True)
    assert set(await _rows(db, conn)) == {"dbo.a", "dbo.b"}

    # 'dbo.b' is gone from the source on the next authoritative crawl.
    await svc.refresh_routines(db, conn, _FakeClient([_proc("dbo.a")]), authoritative=True)
    assert set(await _rows(db, conn)) == {"dbo.a"}


@pytest.mark.asyncio
async def test_per_user_crawl_never_prunes(db):
    conn = await _connection(db)
    svc = ConnectionService()
    await svc.refresh_routines(
        db, conn, _FakeClient([_proc("dbo.a"), _proc("dbo.b")]), authoritative=True)

    # A non-authoritative (per-user) crawl that sees only a slice must add/update
    # but never delete the rows it didn't see.
    await svc.refresh_routines(db, conn, _FakeClient([_proc("dbo.a")]), authoritative=False)
    assert set(await _rows(db, conn)) == {"dbo.a", "dbo.b"}


@pytest.mark.asyncio
async def test_source_without_routines_is_a_clean_noop(db):
    conn = await _connection(db)
    svc = ConnectionService()
    count = await svc.refresh_routines(db, conn, _FakeClient(None, unsupported=True),
                                       authoritative=True)
    assert count == 0
    assert await _rows(db, conn) == {}


@pytest.mark.asyncio
async def test_unreadable_definition_is_recorded_on_the_row(db):
    conn = await _connection(db)
    svc = ConnectionService()
    await svc.refresh_routines(
        db, conn,
        _FakeClient([_proc("dbo.visible", definition_available=True),
                     _proc("dbo.hidden", definition_available=False)]),
        authoritative=True)
    rows = await _rows(db, conn)
    assert rows["dbo.visible"].definition_available is True
    assert rows["dbo.hidden"].definition_available is False


@pytest.mark.asyncio
async def test_overloaded_routines_collapse_without_integrity_error(db):
    # Postgres/Oracle allow two routines with the same schema.name but different
    # argument signatures. They arrive as several Routine objects with one name;
    # they must collapse to a single catalog row, not violate the unique
    # constraint and fail the whole refresh.
    conn = await _connection(db)
    svc = ConnectionService()
    routines = [
        Routine(name="public.f", routine_type="function", arguments="a integer", returns="integer"),
        Routine(name="public.f", routine_type="function", arguments="a text", returns="text"),
    ]
    count = await svc.refresh_routines(db, conn, _FakeClient(routines), authoritative=True)
    rows = await _rows(db, conn)
    assert set(rows) == {"public.f"}
    assert count == 1


@pytest.mark.asyncio
async def test_active_routines_surface_even_with_no_active_tables(db):
    # Routine selection is independent of table selection: a procedure the admin
    # activated must appear in the <routines> context even when the agent has no
    # active tables on that connection.
    from app.ai.context.builders.schema_context_builder import SchemaContextBuilder
    from app.ai.context.sections.tables_schema_section import TablesSchemaContext
    from app.schemas.data_source_schema import DataSourceSummarySchema
    from app.models.data_source import DataSource

    org = Organization(name="org")
    db.add(org)
    await db.flush()
    ds = DataSource(name="sales", organization_id=str(org.id), is_active=True)
    db.add(ds)
    await db.flush()
    conn = Connection(name="mssql", type="mssql", config={}, is_active=True,
                      organization_id=str(org.id))
    conn.data_sources.append(ds)
    db.add(conn)
    await db.flush()
    routine = ConnectionRoutine(connection_id=str(conn.id), name="dbo.usp_load", routine_type="procedure")
    db.add(routine)
    await db.flush()
    ds.active_routine_ids = [str(routine.id)]
    await db.commit()

    builder = SchemaContextBuilder(db, [ds], org, None)
    section = TablesSchemaContext.DataSource(
        info=DataSourceSummarySchema(id=str(ds.id), name=ds.name, type="mssql", context=None),
        tables=[],  # NO active tables on purpose
    )
    await builder._attach_routines([section])

    assert [r.name for r in section.routines] == ["dbo.usp_load"]


def test_agent_section_renders_routines_only_in_full_tiers():
    from app.ai.context.sections.tables_schema_section import TablesSchemaContext
    from app.ai.prompt_formatters import Table as PT
    from app.schemas.data_source_schema import DataSourceSummarySchema

    info = DataSourceSummarySchema(id="ds1", name="Sales", type="mssql", context=None)
    table = PT(name="dbo.orders", columns=[], pks=[], fks=[],
               connection_id="c1", connection_name="MSSQL", connection_type="mssql")
    routines = [
        Routine(name="dbo.usp_load", routine_type="procedure", arguments="@from date"),
        Routine(name="dbo.secret", routine_type="procedure", definition_available=False),
    ]
    section = TablesSchemaContext.DataSource(info=info, tables=[table], routines=routines)

    full = section.render()
    topk = section._render_topk_tables_full(10)
    gist = section._render_gist()

    # Full tiers surface routines with the read_routine pointer; thin tiers omit them.
    assert "<routines>" in full and "read_routine" in full
    assert "<routines>" in topk
    assert "<routines>" not in gist
    # The unreadable routine is flagged, not silently dropped.
    assert "dbo.secret" in full


def test_formatter_renders_readable_signatures():
    out = RoutineFormatter([
        Routine(name="dbo.usp_load", routine_type="procedure",
                params=[RoutineParam(name="@from", dtype="date")]),
        Routine(name="dbo.fn_tax", routine_type="function",
                params=[RoutineParam(name="amount", dtype="decimal")], returns="decimal"),
    ]).routine_str
    # Contract: the planner can see each routine's name, kind, params and (for a
    # function) its return type — one line each.
    lines = out.splitlines()
    assert len(lines) == 2
    assert "dbo.usp_load" in out and "@from" in out
    assert "function dbo.fn_tax" in out and "returns decimal" in out
