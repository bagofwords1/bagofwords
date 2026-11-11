"""Incremental refresh against real engines: Postgres and SQL Server.

Loop B of docs/feedback-loops/custom-table-incremental-refresh.md. Skipped
unless a server is named in the environment:

    INCR_LOOP_PG_URL     postgresql://user:pw@host:port/db
    INCR_LOOP_MSSQL_URL  mssql+pyodbc://user:pw@host:port/master?driver=...

Each must be a **disposable** server seeded by
tools/agent/custom_table_incremental_seed.py — these tests insert, update and
delete rows in its `shop` data. The app database is the harness's own.

What a fake source cannot show and these do: that each dialect's literals are
accepted by its engine, that the filter reaches the source's index, that the
driver's types (JSON, arrays, UUIDs, 100 ns timestamps) survive the trip, and
that the copy matches a table of half a million messy rows exactly.
"""
import json
import os
import time
import uuid
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from app.data_sources.fast import artifacts, incremental
from app.dependencies import async_session_maker
from app.models.connection import Connection
from app.models.connection_table import KIND_BOW, ConnectionTable
from app.models.organization import Organization
from app.services import custom_table_runs
from app.services.custom_query_service import CustomQueryService

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

PG_URL = os.environ.get("INCR_LOOP_PG_URL")
MSSQL_URL = os.environ.get("INCR_LOOP_MSSQL_URL")
REPORT = os.environ.get("INCR_LOOP_REPORT")
ROWS = int(os.environ.get("INCR_LOOP_ROWS", "600000"))


def _seed_module():
    import importlib.util
    path = Path(__file__).resolve().parents[3] / "tools" / "agent" / "custom_table_incremental_seed.py"
    spec = importlib.util.spec_from_file_location("custom_table_incremental_seed", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Engine:
    """One live source: how to reach it and how its SQL spells things."""

    name: str
    orders: str
    events: str
    now: str            # the source's own clock, as a naive UTC timestamp
    family: str

    def __init__(self, url: str):
        self.url = url
        self.sa = create_engine(self.source_url(), isolation_level="AUTOCOMMIT")

    def source_url(self) -> str:
        return self.url

    def run(self, sql: str, **params):
        with self.sa.connect() as conn:
            return conn.execute(text(sql), params)

    def all(self, sql: str, **params) -> list:
        with self.sa.connect() as conn:
            return [tuple(r) for r in conn.execute(text(sql), params).fetchall()]

    def one(self, sql: str, **params):
        return self.all(sql, **params)[0]

    def seed(self) -> None:
        """Put the source back to its seeded state: every test starts from
        the same data, whatever the one before it changed."""
        raise NotImplementedError

    def ago(self, hours: float) -> str:
        raise NotImplementedError

    def plan(self, sql: str) -> str:
        raise NotImplementedError

    def connection(self, organization_id: str) -> Connection:
        raise NotImplementedError


class Postgres(Engine):
    name, orders, events, family = "postgres", "shop.orders", "shop.events", "postgres"
    now = "(now() AT TIME ZONE 'UTC')"

    def seed(self) -> None:
        _seed_module().seed_postgres(self.url, ROWS)

    def ago(self, hours: float) -> str:
        return f"({self.now} - interval '{hours} hours')"

    def plan(self, sql: str) -> str:
        return "\n".join(r[0] for r in self.all("EXPLAIN " + sql))

    def connection(self, organization_id: str) -> Connection:
        url = make_url(self.url)
        connection = Connection(
            name="Shop (Postgres)", type="postgresql", organization_id=organization_id,
            config={"host": url.host, "port": url.port or 5432, "database": url.database},
        )
        connection.encrypt_credentials({"user": url.username, "password": url.password})
        return connection


class SqlServer(Engine):
    name, orders, events, family = "mssql", "dbo.orders", "dbo.events", "tsql"
    now = "SYSUTCDATETIME()"

    def seed(self) -> None:
        _seed_module().seed_mssql(self.url, ROWS)

    def source_url(self) -> str:
        return make_url(self.url).set(database="shop").render_as_string(hide_password=False)

    def ago(self, hours: float) -> str:
        return f"DATEADD(SECOND, -{int(hours * 3600)}, {self.now})"

    def plan(self, sql: str) -> str:
        with self.sa.connect() as conn:
            raw = conn.connection.driver_connection
            cursor = raw.cursor()
            cursor.execute("SET SHOWPLAN_XML ON")
            try:
                cursor.execute(sql)
                return cursor.fetchone()[0]
            finally:
                cursor.execute("SET SHOWPLAN_XML OFF")

    def connection(self, organization_id: str) -> Connection:
        url = make_url(self.url)
        connection = Connection(
            name="Shop (SQL Server)", type="MSSQL", organization_id=organization_id,
            # The throwaway container has a self-signed certificate.
            config={"host": url.host, "port": url.port or 1433, "database": "shop", "encrypt": False},
        )
        connection.encrypt_credentials({"user": url.username, "password": url.password})
        return connection


ENGINES = [
    pytest.param((Postgres, PG_URL), id="postgres",
                 marks=pytest.mark.skipif(not PG_URL, reason="INCR_LOOP_PG_URL not set")),
    pytest.param((SqlServer, MSSQL_URL), id="mssql",
                 marks=pytest.mark.skipif(not MSSQL_URL, reason="INCR_LOOP_MSSQL_URL not set")),
]


class Lab:
    def __init__(self, root: Path, engine: Engine, connection_id: str):
        self.root, self.engine, self.connection_id = root, engine, connection_id
        self.service = CustomQueryService()
        self.table_id = None
        self.timings = {}

    async def table(self, sql: str, mode: str, **config) -> None:
        settings = self.service.refresh_settings(mode, config, sql)
        async with async_session_maker() as db:
            row = ConnectionTable(connection_id=self.connection_id, name="copy", kind=KIND_BOW,
                                  definition_sql=sql, columns=[], pks=[], fks=[],
                                  refresh_mode=settings.mode, refresh_config=settings.to_dict())
            db.add(row)
            await db.commit()
            self.table_id = row.id

    async def refresh(self, label: str = None, **kwargs) -> ConnectionTable:
        started = time.monotonic()
        async with async_session_maker() as db:
            connection = await db.get(Connection, self.connection_id)
            table = await db.get(ConnectionTable, self.table_id)
            await self.service.refresh(db, connection, table, **kwargs)
            await db.refresh(table)
        if label:
            self.timings[label] = round(time.monotonic() - started, 2)
        return table

    async def row(self) -> ConnectionTable:
        async with async_session_maker() as db:
            return await db.get(ConnectionTable, self.table_id)

    async def copy(self, sql: str) -> list:
        row = await self.row()
        con = artifacts.connect_encrypted(row.artifact_path, artifacts.decrypt_key(row.artifact_key_enc),
                                          read_only=True)
        try:
            return [tuple(r) for r in con.execute(sql).fetchall()]
        finally:
            con.close()

    async def last_run(self) -> dict:
        async with async_session_maker() as db:
            listing = await custom_table_runs.list_runs(db, self.table_id, limit=1)
            return await custom_table_runs.get_run(db, self.table_id, listing["runs"][0]["id"])

    def note(self, **facts) -> None:
        """Numbers for the feedback-loop doc, when asked for."""
        if not REPORT:
            return
        path = Path(REPORT)
        data = json.loads(path.read_text()) if path.exists() else {}
        data.setdefault(self.engine.name, {}).update(facts)
        path.write_text(json.dumps(data, indent=2, default=str))


@pytest_asyncio.fixture(params=ENGINES)
async def lab(request, tmp_path):
    engine_cls, url = request.param
    engine = engine_cls(url)
    engine.seed()
    root = tmp_path / "fast"
    with patch.object(artifacts, "_ARTIFACT_ROOT", root):
        async with async_session_maker() as db:
            org = Organization(name=f"Live {uuid.uuid4().hex[:8]}")
            db.add(org)
            await db.flush()
            connection = engine.connection(org.id)
            db.add(connection)
            await db.commit()
            connection_id = connection.id
        try:
            yield Lab(root, engine, connection_id)
        finally:
            engine.sa.dispose()


def _num(value):
    """Engines disagree on the Python type of a SUM; the value is what counts."""
    if value is None:
        return None
    return Decimal(str(value)).quantize(Decimal("0.01"))


ORDER_FINGERPRINT = (
    "SELECT COUNT(*), SUM(id), COUNT(updated_at), SUM(amount), COUNT(customer_name), "
    "SUM(customer_id), COUNT(notes) FROM {table}"
)


async def assert_orders_match(lab: Lab, where: str = "") -> tuple:
    """The copy equals the source: same rows, same values, by aggregate and by sample."""
    e = lab.engine
    source = e.one(ORDER_FINGERPRINT.format(table=e.orders) + where)
    copy = (await lab.copy(ORDER_FINGERPRINT.format(table="copy")))[0]
    assert [_num(v) for v in copy] == [_num(v) for v in source], f"copy {copy} != source {source}"

    # Row for row, on a spread of ids including the awkward ones.
    sample_sql = ("SELECT id, customer_name, amount, currency, status, notes, updated_at FROM {table} "
                  "WHERE id % 9973 = 1 OR id % 211 = 0 ORDER BY id")
    theirs = e.all(sample_sql.format(table=e.orders))
    ours = await lab.copy(sample_sql.format(table="copy"))
    assert len(ours) == len(theirs) > 1000

    def comparable(row):
        id_, name, amount, currency, status, notes, updated = row
        if isinstance(updated, datetime):
            # Our copy keeps microseconds; SQL Server holds 100 ns.
            updated = updated.replace(microsecond=updated.microsecond)
        return (id_, name, _num(amount), currency, status, notes, updated)

    assert [comparable(r) for r in ours] == [comparable(r) for r in theirs]
    return source


def merge_settings(**over) -> dict:
    return {"cursor_column": "updated_at", "unique_key": ["id"], **over}


# -- the full life of one table ---------------------------------------------------

async def test_merge_follows_a_large_messy_table(lab):
    e = lab.engine
    await lab.table(f"SELECT * FROM {e.orders}", "merge", **merge_settings())

    # 1. First load: cut into windows, every row arrives once.
    with patch.object(incremental, "CHUNK_TARGET_ROWS", 100_000):
        table = await lab.refresh("first_load")
    total = (await assert_orders_match(lab))[0]
    first = await lab.last_run()
    assert first["mode"] == "full" and first["chunks_total"] >= 5 == first["chunks_done"] or \
        first["chunks_total"] == first["chunks_done"] >= 5
    assert sum(c["rows"] for c in first["chunks"]) == total == table.no_rows
    missing = e.one(f"SELECT COUNT(*) FROM {e.orders} WHERE updated_at IS NULL")[0]
    assert missing > 1000 and any(f"{missing:,} have no" in ev["message"] for ev in first["events"])
    # The one row dated 2099 did not drag the checkpoint into the future.
    checkpoint = incremental.decode(table.refresh_state["checkpoint"])
    assert checkpoint <= datetime.utcnow() and checkpoint.year < 2099
    lab.note(rows=total, first_load_seconds=lab.timings["first_load"], first_load_windows=first["chunks_total"],
             artifact_mb=round(table.artifact_bytes / 1e6, 1))

    # 2. Nothing changed: an increment re-reads only the lookback and the
    #    rows that have no cursor, and changes nothing.
    table = await lab.refresh("idle_increment")
    idle = await lab.last_run()
    assert idle["mode"] == "incremental"
    assert (idle["rows_inserted"], idle["rows_updated"], idle["row_count"]) == (0, 0, total)
    assert idle["rows_read"] < total / 20, "an increment must not re-read the table"
    lab.note(idle_increment_seconds=lab.timings["idle_increment"], idle_rows_read=idle["rows_read"])

    # 3. The source moves: new rows, changed rows, a late arrival inside the
    #    lookback, one outside it, a hard delete, and a row with no cursor
    #    that changes without ever getting one.
    top = e.one(f"SELECT MAX(id) FROM {e.orders}")[0]
    e.run(f"INSERT INTO {e.orders} (id, customer_id, customer_name, amount, currency, status, updated_at) "
          f"SELECT id + :top, customer_id, customer_name, amount, currency, 'new', {e.now} "
          f"FROM {e.orders} WHERE id % 600 = 1", top=top)
    e.run(f"UPDATE {e.orders} SET status = 'touched', amount = COALESCE(amount, 0) + 1, updated_at = {e.now} "
          f"WHERE id % 1100 = 3")
    e.run(f"UPDATE {e.orders} SET status = 'late-inside', updated_at = {e.ago(2)} WHERE id = 5")
    e.run(f"UPDATE {e.orders} SET status = 'late-outside', updated_at = {e.ago(72)} WHERE id = 6")
    e.run(f"DELETE FROM {e.orders} WHERE id IN (7, 8)")
    null_id = e.one(f"SELECT MIN(id) FROM {e.orders} WHERE updated_at IS NULL")[0]
    e.run(f"UPDATE {e.orders} SET status = 'no-cursor-edit' WHERE id = :id", id=null_id)
    inserted = e.one(f"SELECT COUNT(*) FROM {e.orders} WHERE id > :top", top=top)[0]
    touched = e.one(f"SELECT COUNT(*) FROM {e.orders} WHERE status = 'touched'")[0]

    table = await lab.refresh("increment")
    run = await lab.last_run()
    assert run["mode"] == "incremental" and run["status"] == "success"
    assert run["rows_inserted"] == inserted
    assert run["rows_updated"] == touched + 2      # + the late arrival, + the no-cursor edit
    assert run["rows_read"] < total / 20
    by_id = dict(await lab.copy(f"SELECT id, status FROM copy WHERE id IN (5, 6, 7, 8) OR id = {int(null_id)}"))
    assert by_id[5] == "late-inside" and by_id[null_id] == "no-cursor-edit"
    assert by_id[6] != "late-outside", "a change older than the lookback waits for a full refresh"
    assert 7 in by_id and 8 in by_id, "an increment cannot see a deletion"
    lab.note(increment_seconds=lab.timings["increment"], increment_rows_read=run["rows_read"],
             increment_inserted=inserted, increment_updated=run["rows_updated"])

    # 4. The filter the source ran used its index.
    rendered = run["rendered_query"]
    plan = e.plan(rendered)
    assert "orders_updated_at" in plan, f"the window did not reach the index:\n{plan[:1500]}"
    lab.note(increment_query=rendered)

    # 5. A full refresh makes the copy exact again: the late change arrives,
    #    the deleted rows leave.
    with patch.object(incremental, "CHUNK_TARGET_ROWS", 100_000):
        await lab.refresh("full_refresh", full=True)
    await assert_orders_match(lab)
    assert await lab.copy("SELECT id, status FROM copy WHERE id IN (6, 7, 8)") == [(6, "late-outside")]
    lab.note(full_refresh_seconds=lab.timings["full_refresh"])

    # Every row names a run that exists, and the runs tell the story in order.
    async with async_session_maker() as db:
        listing = await custom_table_runs.list_runs(db, lab.table_id, limit=10)
    assert [(r["mode"], r["status"]) for r in reversed(listing["runs"])] == [
        ("full", "success"), ("incremental", "success"), ("incremental", "success"), ("full", "success")]


async def test_an_interrupted_first_load_resumes_and_still_matches(lab):
    e = lab.engine
    await lab.table(f"SELECT * FROM {e.orders}", "merge", **merge_settings())
    from app.data_sources.fast.sources import SqlAlchemySource
    original, calls = SqlAlchemySource.stream_batches, {"n": 0}

    def dies_on_third_window(self, sql, batch_rows):
        calls["n"] += 1
        if calls["n"] == 3:
            raise ConnectionError("connection reset by peer")
        return original(self, sql, batch_rows)

    with patch.object(incremental, "CHUNK_TARGET_ROWS", 100_000):
        with patch.object(SqlAlchemySource, "stream_batches", dies_on_third_window), \
                pytest.raises(HTTPException):
            await lab.refresh()
        failed = await lab.last_run()
        assert failed["status"] == "failed" and failed["chunks_done"] == 2
        assert (await lab.row()).artifact_path is None
        await lab.refresh("resumed_load")
    resumed = await lab.last_run()
    total = (await assert_orders_match(lab))[0]
    assert resumed["status"] == "success" and resumed["rows_read"] < total
    assert resumed["rows_read"] + sum(c["rows"] for c in failed["chunks"] if c["status"] == "done") == total
    lab.note(resumed_load_seconds=lab.timings["resumed_load"], resumed_rows_read=resumed["rows_read"])


async def test_a_filter_placed_by_the_admin_is_filled_in_the_engines_own_sql(lab):
    """{{since}} / {{until}} on the base column; rows with no cursor are, by
    the query's own predicate, not part of this table."""
    e = lab.engine
    sql = (f"SELECT id, customer_name, amount, status, updated_at FROM {e.orders} "
           f"WHERE updated_at >= {{{{since}}}} AND updated_at < {{{{until}}}}")
    await lab.table(sql, "merge", **merge_settings(cursor_kind="timestamp", lookback_seconds=3600))
    with patch.object(incremental, "CHUNK_TARGET_ROWS", 150_000):
        await lab.refresh()
    with_cursor = e.one(f"SELECT COUNT(*), SUM(id) FROM {e.orders} WHERE updated_at IS NOT NULL")
    assert tuple(_num(v) for v in (await lab.copy("SELECT COUNT(*), SUM(id) FROM copy"))[0]) == \
        tuple(_num(v) for v in with_cursor)
    first = await lab.last_run()
    assert first["chunks_total"] >= 3 and "bow_sub" not in first["rendered_query"]

    e.run(f"UPDATE {e.orders} SET status = 'placed', updated_at = {e.now} WHERE id % 5000 = 9")
    changed = e.one(f"SELECT COUNT(*) FROM {e.orders} WHERE status = 'placed'")[0]
    await lab.refresh()
    run = await lab.last_run()
    # A row that had no timestamp before was, by this query, not in the table:
    # it arrives now as an insert.
    assert run["rows_updated"] + run["rows_inserted"] == changed and run["rows_updated"] > 0
    assert run["rows_read"] < 10 * max(changed, 100)
    assert "orders_updated_at" in e.plan(run["rendered_query"])
    assert (await lab.copy("SELECT COUNT(*) FROM copy WHERE status = 'placed'"))[0][0] == changed
    assert (await lab.row()).no_rows == e.one(f"SELECT COUNT(*) FROM {e.orders} WHERE updated_at IS NOT NULL")[0]


async def test_append_follows_a_keyless_table_with_exact_duplicates(lab):
    e = lab.engine
    await lab.table(f"SELECT * FROM {e.events}", "append", cursor_column="happened_at",
                    lookback_seconds=6 * 3600)
    fingerprint = "SELECT COUNT(*), COUNT(order_id), SUM(order_id), COUNT(DISTINCT happened_at) FROM {t}"

    async def matches():
        assert [_num(v) for v in (await lab.copy(fingerprint.format(t="copy")))[0]] == \
            [_num(v) for v in e.one(fingerprint.format(t=e.events))]

    await lab.refresh()
    await matches()
    # New events, some of them exact repeats of each other, and the same batch
    # delivered a second time.
    for _ in range(2):
        e.run(f"INSERT INTO {e.events} (happened_at, kind, order_id, value) "
              f"SELECT {e.now}, kind, order_id, value FROM {e.events} WHERE order_id % 4000 = 7")
    await lab.refresh()
    await matches()
    run = await lab.last_run()
    assert run["mode"] == "incremental" and run["rows_read"] < e.one(f"SELECT COUNT(*) FROM {e.events}")[0] / 2
    await lab.refresh()                 # reading the same window again changes nothing
    await matches()


async def test_a_daily_rollup_recomputes_whole_days_only(lab):
    e = lab.engine
    day = "CAST(happened_at AS date)"
    sql = (f"SELECT {day} AS day, kind, COUNT(*) AS n, SUM(value) AS total FROM {e.events} "
           f"WHERE happened_at >= {{{{since}}}} GROUP BY {day}, kind")
    await lab.table(sql, "append", cursor_column="day", cursor_kind="date", grain="day",
                    lookback_seconds=2 * 86400)
    await lab.refresh()

    async def matches():
        truth = e.all(f"SELECT {day}, kind, COUNT(*), SUM(value) FROM {e.events} GROUP BY {day}, kind")
        ours = await lab.copy("SELECT day, kind, n, total FROM copy")
        norm = lambda rows: sorted((str(d), k, n, None if t is None else round(float(t), 6))  # noqa: E731
                                   for d, k, n, t in rows)
        assert norm(ours) == norm(truth)

    await matches()
    # More events land on the last day already loaded, and on a new one.
    last = e.one(f"SELECT MAX(happened_at) FROM {e.events}")[0]
    e.run(f"INSERT INTO {e.events} (happened_at, kind, order_id, value) "
          f"SELECT :at, kind, order_id, 1.5 FROM {e.events} WHERE order_id % 3000 = 11", at=last)
    await lab.refresh()
    await matches()
    run = await lab.last_run()
    days = e.one(f"SELECT COUNT(DISTINCT {day}) FROM {e.events}")[0]
    assert run["mode"] == "incremental" and run["rows_read"] <= 4 * 4 < days * 4, \
        "only the last few days were recomputed"


async def test_driver_types_and_awkward_values_survive_the_trip(lab):
    """What the driver hands back for JSON, arrays, UUIDs, bits, wide numbers
    and non-Latin text is stored, queryable, and equal to the source."""
    e = lab.engine
    lo, hi = ROWS // 6 - 2000, ROWS // 6 + 2000      # spans where the notes column starts being filled
    await lab.table(f"SELECT * FROM {e.orders} WHERE id > {lo} AND id <= {hi}", "merge", **merge_settings())
    table = await lab.refresh()
    types = {c["name"]: c["dtype"] for c in table.columns}
    assert types["id"] == "int64" and types["amount"].startswith("decimal")
    assert "timestamp" in types["updated_at"]
    assert types["customer_name"] == "string" and types["notes"] == "string"

    names = dict(await lab.copy("SELECT id % 11, MAX(customer_name) FROM copy WHERE id % 11 IN (0,1,2,3,4) GROUP BY 1"))
    assert names == {0: "O'Brien & Sons", 1: "Müller GmbH", 2: "株式会社テスト", 3: 'שלום בע"מ', 4: "Acme 🚀 Rockets"}
    # Empty string and NULL stay different things.
    assert (await lab.copy("SELECT COUNT(*) FROM copy WHERE customer_name = ''"))[0][0] > 0
    assert (await lab.copy("SELECT COUNT(*) FROM copy WHERE customer_name IS NULL"))[0][0] > 0
    # Negative, missing and very large amounts are exact.
    assert _num((await lab.copy("SELECT SUM(amount) FROM copy"))[0][0]) == \
        _num(e.one(f"SELECT SUM(amount) FROM {e.orders} WHERE id > {lo} AND id <= {hi}")[0])
    # JSON of every shape is kept as JSON text the engine can query.
    shapes = dict(await lab.copy(
        "SELECT json_type(TRY_CAST(payload AS JSON)), COUNT(*) FROM copy WHERE payload IS NOT NULL GROUP BY 1"))
    assert shapes.get("OBJECT", 0) > 0 and shapes.get("ARRAY", 0) > 0
    stores = (await lab.copy(
        "SELECT COUNT(*) FROM copy WHERE json_extract_string(TRY_CAST(payload AS JSON), '$.store.city') = 'Tel Aviv'"))
    assert stores[0][0] > 0
    assert (await lab.copy("SELECT COUNT(DISTINCT ref) FROM copy"))[0][0] > 3000


async def test_sql_server_rowversion_is_a_cursor_that_needs_no_clock(lab):
    if lab.engine.name != "mssql":
        pytest.skip("rowversion is SQL Server's")
    e = lab.engine
    sql = (f"SELECT id, status, amount, CAST(rv AS BIGINT) AS version FROM {e.orders} "
           f"WHERE rv >= CAST(CAST({{{{since}}}} AS BIGINT) AS BINARY(8))")
    # The cast to BINARY(8) is how rowversion compares; it only orders
    # correctly from zero up, so the load is told to start there.
    await lab.table(sql, "merge", cursor_column="version", cursor_kind="number", unique_key=["id"],
                    lookback_units=0, initial_value="0")
    await lab.refresh()
    total = e.one(f"SELECT COUNT(*) FROM {e.orders}")[0]
    assert (await lab.row()).no_rows == total
    # Any change moves rowversion — including one that sets no timestamp at all.
    e.run(f"UPDATE {e.orders} SET status = 'silent' WHERE id % 20000 = 4")
    silent = e.one(f"SELECT COUNT(*) FROM {e.orders} WHERE status = 'silent'")[0]
    await lab.refresh()
    run = await lab.last_run()
    assert run["rows_updated"] == silent and run["rows_read"] == silent + 1
    assert (await lab.copy("SELECT COUNT(*) FROM copy WHERE status = 'silent'"))[0][0] == silent


async def test_sql_server_timestamps_finer_than_a_microsecond_are_not_lost_at_the_boundary(lab):
    """The checkpoint keeps microseconds; the column holds 100 ns. Rows that
    fall between the two must still arrive."""
    if lab.engine.name != "mssql":
        pytest.skip("datetime2(7) is SQL Server's")
    e = lab.engine
    e.run("IF OBJECT_ID('dbo.ticks') IS NOT NULL DROP TABLE dbo.ticks")
    e.run("CREATE TABLE dbo.ticks (id INT PRIMARY KEY, at DATETIME2(7))")
    try:
        e.run("INSERT INTO dbo.ticks VALUES (1, '2026-05-01 10:00:00.1234561'), (2, '2026-05-01 10:00:00.1234567')")
        await lab.table("SELECT * FROM dbo.ticks", "merge", cursor_column="at", unique_key=["id"],
                        lookback_seconds=0)
        await lab.refresh()
        e.run("INSERT INTO dbo.ticks VALUES (3, '2026-05-01 10:00:00.1234568'), (4, '2026-05-01 10:00:00.1234569')")
        await lab.refresh()
        assert [r[0] for r in await lab.copy("SELECT id FROM copy ORDER BY id")] == [1, 2, 3, 4]
    finally:
        e.run("DROP TABLE dbo.ticks")


# -- a connector that is not SQL at all: Aria Operations (vROps) -----------------------

ARIA_URL = os.environ.get("INCR_LOOP_ARIA_URL")      # tools/aria_operations/mock_suite_api.py
STAT_KEYS = ["cpu|usage_average", "mem|usage_average", "virtualDisk|totalLatency",
             "virtualDisk|totalReadLatency", "virtualDisk|totalWriteLatency"]
FIVE_MIN_MS = 5 * 60 * 1000


@pytest_asyncio.fixture
async def aria(tmp_path):
    if not ARIA_URL:
        pytest.skip("INCR_LOOP_ARIA_URL not set")
    root = tmp_path / "fast"
    with patch.object(artifacts, "_ARTIFACT_ROOT", root):
        async with async_session_maker() as db:
            org = Organization(name=f"Aria {uuid.uuid4().hex[:8]}")
            db.add(org)
            await db.flush()
            connection = Connection(name="Aria Operations", type="aria_operations", organization_id=org.id,
                                    config={"url": ARIA_URL, "verify_ssl": False})
            connection.encrypt_credentials({
                "username": os.environ.get("INCR_LOOP_ARIA_USER", "admin"),
                "password": os.environ.get("INCR_LOOP_ARIA_PASSWORD", "Aria!2024"),
            })
            db.add(connection)
            await db.commit()
            connection_id = connection.id
        lab = Lab(root, None, connection_id)
        lab.engine = type("Aria", (), {"name": "aria_operations"})()
        yield lab


def _aria_client():
    from app.data_sources.clients.aria_operations_client import AriaOperationsClient
    return AriaOperationsClient(url=ARIA_URL, username=os.environ.get("INCR_LOOP_ARIA_USER", "admin"),
                                password=os.environ.get("INCR_LOOP_ARIA_PASSWORD", "Aria!2024"),
                                verify_ssl=False)


async def test_metrics_from_a_json_api_load_in_windows_and_then_only_the_new_points(aria):
    """Twenty-five days of metrics for fifty series is far more than one
    response holds. The load must split itself without cutting a rollup bucket
    in two, and the next refresh must ask for the last hour, not the month."""
    bucket = 15 * 60 * 1000
    request = {"table": "metrics", "resource_kind": "VirtualMachine", "stat_key": STAT_KEYS,
               "interval": "MINUTES", "interval_quantifier": 15, "rollup": "AVG", "duration_in_mins": 60}
    now = int(time.time() * 1000)
    start = (now - 25 * 86_400_000) // 86_400_000 * 86_400_000
    await aria.table(json.dumps(request), "append", cursor_column="timestamp", cursor_kind="number",
                     initial_value=str(start), lookback_units=3_600_000)
    table = await aria.refresh("first_load")
    first = await aria.last_run()
    assert first["status"] == "success" and table.no_rows > 100_000
    sent = json.loads(first["rendered_query"])
    assert sent["start_time"] == start and "duration_in_mins" not in sent

    shape = ("SELECT COUNT(DISTINCT resourceId || statKey), MIN(timestamp), MAX(timestamp), COUNT(*), "
             "COUNT(DISTINCT resourceId || statKey || CAST(timestamp AS VARCHAR)) FROM copy")
    series, lo, hi, points, distinct = (await aria.copy(shape))[0]
    assert series == 10 * len(STAT_KEYS) and lo == start
    assert points == distinct, "a bucket was returned by both halves of a split"
    assert points == series * ((hi - lo) // bucket + 1), "a truncated response would leave gaps"

    # The values are the API's own: every complete day, compared with that day
    # asked for directly (well under one response).
    direct = {k: v for k, v in request.items() if k != "duration_in_mins"}
    client = _aria_client()
    for day in range(start, start + 24 * 86_400_000, 4 * 86_400_000):
        theirs = client.execute_query(json.dumps({**direct, "limit": 50_000, "start_time": day,
                                                  "end_time": day + 86_400_000 - 1}))
        ours = await aria.copy(f"SELECT COUNT(*), SUM(value) FROM copy "
                               f"WHERE timestamp >= {day} AND timestamp < {day + 86_400_000}")
        assert ours[0][0] == len(theirs) == series * 96
        assert round(ours[0][1], 1) == round(float(theirs["value"].sum()), 1)

    table = await aria.refresh("increment")
    run = await aria.last_run()
    assert run["mode"] == "incremental" and run["status"] == "success"
    asked = json.loads(run["rendered_query"])
    assert asked["start_time"] == hi - 3_600_000 and asked["start_time"] % bucket == 0
    assert run["rows_read"] <= series * 6, "an hour of buckets, not twenty-five days"
    _series, _lo, hi2, points2, distinct2 = (await aria.copy(shape))[0]
    assert points2 == distinct2 >= points and hi2 >= hi, "re-reading the last hour duplicated nothing"
    assert points2 == series * ((hi2 - lo) // bucket + 1)
    aria.note(rows=points, first_load_seconds=aria.timings["first_load"],
              increment_seconds=aria.timings["increment"], increment_rows_read=run["rows_read"])


# -- other SQL engines: the dialect's literals meet a real server ----------------------

MARIADB_URL = os.environ.get("INCR_LOOP_MARIADB_URL")    # mysql+pymysql://user:pw@host:port/db


@pytest_asyncio.fixture
async def mariadb(tmp_path):
    if not MARIADB_URL:
        pytest.skip("INCR_LOOP_MARIADB_URL not set")
    url = make_url(MARIADB_URL)
    engine = type("MariaDb", (Engine,), {"name": "mariadb", "family": "mysql", "now": "UTC_TIMESTAMP(6)"})(MARIADB_URL)
    engine.run("DROP TABLE IF EXISTS incr_smoke")
    engine.run("CREATE TABLE incr_smoke (id INT PRIMARY KEY, v VARCHAR(40), amount DECIMAL(12,2), "
               "updated_at DATETIME(6) NULL, INDEX incr_smoke_updated_at (updated_at))")
    rows = []
    for i in range(1, 5001):
        if i % 97 == 0:
            stamp = None                                   # never stamped
        elif i == 1:
            stamp = "1970-01-02 00:00:00"                  # an ancient default
        elif i == 2:
            stamp = "2099-01-01 00:00:00"                  # a clock set wrong
        elif 2000 <= i < 2300:
            stamp = "2026-03-01 02:00:00"                  # one bulk import
        else:
            stamp = f"2026-0{1 + i % 9}-{1 + i % 27:02d} {i % 24:02d}:{i % 60:02d}:00.{i:06d}"
        rows.append({"id": i, "v": f"שלום 🚀 {i}" if i % 5 == 0 else f"v{i}", "amount": (i % 400) - 37.25,
                     "at": stamp})
    with engine.sa.connect() as conn:
        conn.execute(text("INSERT INTO incr_smoke VALUES (:id, :v, :amount, :at)"), rows)
    root = tmp_path / "fast"
    with patch.object(artifacts, "_ARTIFACT_ROOT", root):
        async with async_session_maker() as db:
            org = Organization(name=f"Maria {uuid.uuid4().hex[:8]}")
            db.add(org)
            await db.flush()
            connection = Connection(name="Shop (MariaDB)", type="mariadb", organization_id=org.id,
                                    config={"host": url.host, "port": url.port or 3306, "database": url.database})
            connection.encrypt_credentials({"user": url.username, "password": url.password})
            db.add(connection)
            await db.commit()
            connection_id = connection.id
        try:
            yield Lab(root, engine, connection_id)
        finally:
            engine.run("DROP TABLE IF EXISTS incr_smoke")
            engine.sa.dispose()


@pytest.mark.parametrize("sql", [
    "SELECT * FROM incr_smoke",
    "SELECT * FROM incr_smoke WHERE updated_at >= {{since}} AND updated_at < {{until}}",
])
async def test_mariadb_accepts_the_rendered_windows_and_follows_the_source(mariadb, sql):
    e = mariadb.engine
    placed = "{{" in sql
    where = " WHERE updated_at IS NOT NULL" if placed else ""
    fingerprint = "SELECT COUNT(*), SUM(id), SUM(amount), COUNT(updated_at) FROM {t}"

    async def matches():
        assert [_num(v) for v in (await mariadb.copy(fingerprint.format(t="copy")))[0]] == \
            [_num(v) for v in e.one(fingerprint.format(t="incr_smoke") + where)]

    await mariadb.table(sql, "merge", cursor_column="updated_at", unique_key=["id"], lookback_seconds=3600,
                        **({"cursor_kind": "timestamp"} if placed else {}))
    with patch.object(incremental, "CHUNK_TARGET_ROWS", 800):
        table = await mariadb.refresh()
    await matches()
    first = await mariadb.last_run()
    assert first["chunks_total"] >= 4, "the load was cut into windows the server accepted"
    assert incremental.decode(table.refresh_state["checkpoint"]).year < 2099
    names = dict(await mariadb.copy("SELECT id, v FROM copy WHERE id IN (5, 6)"))
    assert names == {5: "שלום 🚀 5", 6: "v6"}

    e.run(f"UPDATE incr_smoke SET v = 'changed', updated_at = {e.now} WHERE id % 500 = 3")
    e.run(f"INSERT INTO incr_smoke VALUES (9001, 'new', 1.5, {e.now}), (9002, 'new', NULL, {e.now})")
    await mariadb.refresh()
    await matches()
    run = await mariadb.last_run()
    assert run["mode"] == "incremental" and (run["rows_inserted"], run["rows_updated"]) == (2, 10)
    assert run["rows_read"] < 500
    plan = " ".join(str(r) for r in e.all("EXPLAIN " + run["rendered_query"]))
    assert "incr_smoke_updated_at" in plan, f"the window did not reach the index: {plan}"


# -- a connector with no source built for it: cached through its own query call --------

SERVICENOW_URL = os.environ.get("INCR_LOOP_SERVICENOW_URL")     # tools/agent/fake_servicenow.py


async def test_a_connector_with_only_execute_query_is_cached_and_refreshed(tmp_path):
    """ServiceNow has no extraction source and no window writer. Its real
    client, against the repo's fake instance, is cached through the one call
    every connector has — and its table refreshes incrementally all the same."""
    if not SERVICENOW_URL:
        pytest.skip("INCR_LOOP_SERVICENOW_URL not set")
    root = tmp_path / "fast"
    with patch.object(artifacts, "_ARTIFACT_ROOT", root):
        async with async_session_maker() as db:
            org = Organization(name=f"ServiceNow {uuid.uuid4().hex[:8]}")
            db.add(org)
            await db.flush()
            connection = Connection(name="ServiceNow", type="servicenow", organization_id=org.id,
                                    config={"instance_url": SERVICENOW_URL, "verify_ssl": False})
            connection.encrypt_credentials({"username": "any", "password": "any"})
            db.add(connection)
            await db.commit()
            lab = Lab(root, None, connection.id)
        request = json.dumps({"table": "kb_knowledge", "fields": ["number", "short_description", "sys_updated_on"],
                              "limit": 500})
        await lab.table(request, "merge", cursor_column="sys_updated_on", unique_key=["number"])
        table = await lab.refresh()
        assert table.last_refresh_status == "ok" and table.no_rows >= 1
        assert {"number", "short_description", "sys_updated_on"} <= {c["name"] for c in table.columns}
        first = await lab.last_run()
        assert first["rendered_query"] == request
        notes = " ".join(e["message"] for e in first["events"])
        assert "own query call" in notes and "cannot filter at the source" in notes
        rows = await lab.copy("SELECT number, sys_updated_on FROM copy")

        table = await lab.refresh()
        run = await lab.last_run()
        assert run["mode"] == "incremental" and (run["rows_inserted"], run["rows_updated"]) == (0, 0)
        assert await lab.copy("SELECT number, sys_updated_on FROM copy") == rows
