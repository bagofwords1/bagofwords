"""read_routine fetches ONE routine's source on demand, scoped to the run's
data sources, and degrades honestly when the source isn't readable.

Contract: a name not in scope returns found=False; a routine whose source the
account can't see returns definition_available=False with a message and never
calls the client; a readable routine returns its source text; a name on more
than one connection without connection_id asks to disambiguate. The client is
mocked at the construct_client boundary (no real database driver).
"""
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

import main  # noqa: F401 — registers mappers
from app.models.base import BaseSchema
from app.models.organization import Organization
from app.models.data_source import DataSource
from app.models.connection import Connection
from app.models.connection_routine import ConnectionRoutine
from app.ai.tools.implementations.read_routine import ReadRoutineTool


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(BaseSchema.metadata.create_all)
    maker = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as session:
        yield session
    await engine.dispose()


class _Hub:
    def __init__(self, data_sources, user=None):
        self.data_sources = data_sources
        self.user = user


class _FakeClient:
    def __init__(self, source):
        self._source = source

    async def aget_routine_definition(self, name):
        return self._source


async def _seed(db, *, routines):
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
    for r in routines:
        db.add(ConnectionRoutine(connection_id=str(conn.id), **r))
    await db.commit()
    return ds, conn


async def _run(tool, db, ds, name, monkeypatch=None, client_source=None, connection_id=None):
    if monkeypatch is not None:
        async def _construct(self, _db, connection, user):
            return _FakeClient(client_source)
        from app.services.connection_service import ConnectionService
        monkeypatch.setattr(ConnectionService, "construct_client", _construct)
    runtime = {"db": db, "context_hub": _Hub([ds])}
    inp = {"name": name}
    if connection_id:
        inp["connection_id"] = connection_id
    out = None
    observation = None
    async for ev in tool.run_stream(inp, runtime):
        if ev.type == "tool.end":
            out = ev.payload["output"]
            observation = ev.payload.get("observation")
    _LAST["observation"] = observation
    return out


_LAST: dict = {}


@pytest.mark.asyncio
async def test_reads_source_of_a_known_routine(db, monkeypatch):
    ds, _ = await _seed(db, routines=[dict(name="dbo.usp_load", routine_type="procedure")])
    out = await _run(ReadRoutineTool(), db, ds, "dbo.usp_load",
                     monkeypatch=monkeypatch, client_source="CREATE PROCEDURE dbo.usp_load AS ...")
    assert out["found"] is True
    assert out["definition_available"] is True
    assert "CREATE PROCEDURE" in out["source"]
    assert out["routine_type"] == "procedure"
    # The observation the planner reads must carry the actual source, not just a
    # char count — otherwise the agent reports metadata instead of the code.
    assert "CREATE PROCEDURE" in (_LAST["observation"]["summary"])


@pytest.mark.asyncio
async def test_unknown_name_returns_not_found(db, monkeypatch):
    ds, _ = await _seed(db, routines=[dict(name="dbo.usp_load", routine_type="procedure")])
    out = await _run(ReadRoutineTool(), db, ds, "dbo.does_not_exist",
                     monkeypatch=monkeypatch, client_source="x")
    assert out["found"] is False
    assert out["source"] is None


@pytest.mark.asyncio
async def test_unreadable_source_never_calls_client(db, monkeypatch):
    ds, _ = await _seed(db, routines=[
        dict(name="dbo.secret", routine_type="procedure", definition_available=False)])

    called = {"n": 0}
    async def _construct(self, _db, connection, user):
        called["n"] += 1
        return _FakeClient("should not be read")
    from app.services.connection_service import ConnectionService
    monkeypatch.setattr(ConnectionService, "construct_client", _construct)

    out = None
    async for ev in ReadRoutineTool().run_stream({"name": "dbo.secret"},
                                                 {"db": db, "context_hub": _Hub([ds])}):
        if ev.type == "tool.end":
            out = ev.payload["output"]
    assert out["found"] is True
    assert out["definition_available"] is False
    assert out["source"] is None
    assert out["message"]
    assert called["n"] == 0  # we must not build a client for an unreadable routine


@pytest.mark.asyncio
async def test_out_of_scope_data_source_is_not_found(db, monkeypatch):
    # Routine exists, but the run's scope has no data sources at all.
    await _seed(db, routines=[dict(name="dbo.usp_load", routine_type="procedure")])
    out = None
    async for ev in ReadRoutineTool().run_stream({"name": "dbo.usp_load"},
                                                 {"db": db, "context_hub": _Hub([])}):
        if ev.type == "tool.end":
            out = ev.payload["output"]
    assert out["found"] is False
