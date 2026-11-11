"""Incremental refresh and run history, through the real service.

Runs on the harness database (``pytestmark = db``), with a real SQLite source,
the real extraction path, real encrypted DuckDB files and the real locks. Only
the artifact root is replaced (a tmp dir), plus — where a test needs a load to
stop part-way — one source call.

The invariant most of these protect: **a table refreshed incrementally holds
exactly what a full refresh of the same source would hold**, whatever happened
to the source in between and however the refreshes were interrupted.
"""
import asyncio
import os
import random
import sqlite3
import time
import uuid
from contextlib import closing, contextmanager
from pathlib import Path
from unittest.mock import patch

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import select

from app.data_sources.fast import artifacts, extractor, incremental, rls
from app.data_sources.fast import events as table_events
from app.dependencies import async_session_maker
from app.models.connection import Connection
from app.models.connection_table import KIND_BOW, ConnectionTable
from app.models.custom_table_run import CustomTableRun
from app.models.organization import Organization
from app.services import custom_query_service, custom_table_runs
from app.services.custom_query_service import CustomQueryService

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

COLUMNS = "id, customer, amount, status, seq, stamp"


class Shop:
    """The source: a small order table that changes the way real ones do.

    `seq` grows on every insert and update (a numeric cursor); `stamp` is the
    same moment as text, the only form SQLite keeps timestamps in.
    """

    def __init__(self, path: Path):
        self.path = path
        self._seq = 0
        with self.db() as db:
            db.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, customer TEXT, amount REAL, "
                       "status TEXT, seq INTEGER, stamp TEXT)")
            db.execute("CREATE TABLE events (seq INTEGER, kind TEXT, value REAL)")

    @contextmanager
    def db(self):
        with closing(sqlite3.connect(self.path)) as db, db:
            yield db

    def tick(self) -> int:
        self._seq += 1
        return self._seq

    def stamp(self, seq: int) -> str:
        return f"2026-01-01 00:{seq // 60 % 60:02d}:{seq % 60:02d}" if seq < 3600 else f"2026-01-02 {seq:09d}"

    def add(self, count: int, **over) -> list:
        ids = []
        with self.db() as db:
            for _ in range(count):
                seq = over.get("seq", self.tick())
                cur = db.execute(
                    "INSERT INTO orders (customer, amount, status, seq, stamp) VALUES (?, ?, ?, ?, ?)",
                    (over.get("customer", f"c{seq % 7}"), over.get("amount", seq * 1.5),
                     over.get("status", "new"), seq, None if seq is None else self.stamp(seq)),
                )
                ids.append(cur.lastrowid)
        return ids

    def touch(self, ids, status="changed") -> None:
        with self.db() as db:
            for i in ids:
                seq = self.tick()
                db.execute("UPDATE orders SET status = ?, amount = amount + 1, seq = ?, stamp = ? WHERE id = ?",
                           (status, seq, self.stamp(seq), i))

    def remove(self, ids) -> None:
        with self.db() as db:
            db.executemany("DELETE FROM orders WHERE id = ?", [(i,) for i in ids])

    def ids(self) -> list:
        with self.db() as db:
            return [r[0] for r in db.execute("SELECT id FROM orders ORDER BY id")]

    def rows(self, sql: str = f"SELECT {COLUMNS} FROM orders") -> list:
        with self.db() as db:
            return sorted(db.execute(sql).fetchall(), key=repr)

    def log(self, count: int) -> None:
        with self.db() as db:
            for _ in range(count):
                seq = self.tick()
                db.execute("INSERT INTO events VALUES (?, ?, ?)", (seq, "click", seq / 2))


class Lab:
    def __init__(self, root: Path, shop: Shop, connection_id: str, org_id: str):
        self.root, self.shop, self.connection_id, self.org_id = root, shop, connection_id, org_id
        self.service = CustomQueryService()
        self.table_id = None

    async def table(self, sql="SELECT * FROM orders", mode="merge", **config) -> str:
        """Create a custom table row (not yet refreshed)."""
        if mode == "merge":
            config = {"cursor_column": "seq", "unique_key": ["id"], **config}
        elif mode == "append":
            config = {"cursor_column": "seq", **config}
        settings = self.service.refresh_settings(mode, config, sql)
        async with async_session_maker() as db:
            row = ConnectionTable(connection_id=self.connection_id, name="copy", kind=KIND_BOW,
                                  definition_sql=sql, columns=[], pks=[], fks=[],
                                  refresh_mode=settings.mode, refresh_config=settings.to_dict())
            db.add(row)
            await db.commit()
            self.table_id = row.id
        return self.table_id

    async def refresh(self, **kwargs) -> ConnectionTable:
        async with async_session_maker() as db:
            connection = await db.get(Connection, self.connection_id)
            table = await db.get(ConnectionTable, self.table_id)
            await self.service.refresh(db, connection, table, **kwargs)
            await db.refresh(table)
            return table

    async def row(self) -> ConnectionTable:
        async with async_session_maker() as db:
            return await db.get(ConnectionTable, self.table_id)

    async def copy(self, sql: str = f"SELECT {COLUMNS} FROM copy") -> list:
        """What the artifact holds, read the way a query would."""
        row = await self.row()
        con = artifacts.connect_encrypted(row.artifact_path, artifacts.decrypt_key(row.artifact_key_enc),
                                          read_only=True)
        try:
            return sorted(con.execute(sql).fetchall(), key=repr)
        finally:
            con.close()

    async def runs(self) -> list:
        """Newest first, in full."""
        async with async_session_maker() as db:
            listing = await custom_table_runs.list_runs(db, self.table_id, limit=100)
            return [await custom_table_runs.get_run(db, self.table_id, r["id"]) for r in listing["runs"]]

    async def matches_source(self, source_sql=f"SELECT {COLUMNS} FROM orders") -> None:
        assert await self.copy() == self.shop.rows(source_sql)

    def files(self, pattern="*.db") -> set:
        return set(self.root.rglob(pattern))


@pytest_asyncio.fixture
async def lab(tmp_path):
    root = tmp_path / "fast"
    shop = Shop(tmp_path / "source.sqlite")
    with patch.object(artifacts, "_ARTIFACT_ROOT", root):
        async with async_session_maker() as db:
            org = Organization(name=f"Incremental {uuid.uuid4().hex[:8]}")
            db.add(org)
            await db.flush()
            connection = Connection(name="Shop", type="sqlite", config={"database": str(shop.path)},
                                    organization_id=org.id)
            db.add(connection)
            await db.commit()
            ids = (connection.id, org.id)
        yield Lab(root, shop, *ids)


@contextmanager
def small_chunks(rows: int = 10):
    with patch.object(incremental, "CHUNK_TARGET_ROWS", rows):
        yield


@contextmanager
def source_fails_after(loads: int):
    """Let `loads` row-loading queries through, then fail every later one.

    Sizing and costing queries are not counted: what is interrupted is the
    moving of rows, wherever in the load that happens to be.
    """
    from app.data_sources.fast.sources import SqlAlchemySource
    original, seen = SqlAlchemySource.stream_batches, {"n": 0}

    def flaky(self, sql, batch_rows):
        seen["n"] += 1
        if seen["n"] > loads:
            raise ConnectionError("source went away")
        return original(self, sql, batch_rows)

    with patch.object(SqlAlchemySource, "stream_batches", flaky):
        yield seen


# -- the central invariant ------------------------------------------------------

@pytest.mark.parametrize("seed", [1, 7, 42])
@pytest.mark.parametrize("sql", ["SELECT * FROM orders",
                                 "SELECT * FROM orders WHERE seq >= {{since}} AND seq < {{until}}"])
async def test_merge_tracks_a_changing_source_exactly(lab, seed, sql):
    """Inserts, updates and re-reads, round after round: the copy equals the source."""
    rng = random.Random(seed)
    lab.shop.add(40)
    kind = {"cursor_kind": "number"} if "{{" in sql else {}
    await lab.table(sql, "merge", lookback_units=rng.choice([0, 3, 25]), **kind)
    with small_chunks(rng.choice([7, 15, 1000])):
        await lab.refresh()
    await lab.matches_source()
    for _ in range(6):
        lab.shop.add(rng.randint(0, 9))
        lab.shop.touch(rng.sample(lab.shop.ids(), k=rng.randint(0, 6)))
        table = await lab.refresh()
        assert table.last_refresh_status == "ok"
        await lab.matches_source()
    runs = await lab.runs()
    assert [r["mode"] for r in runs] == ["incremental"] * 6 + ["full"]
    assert all(r["status"] == "success" for r in runs)


@pytest.mark.parametrize("seed", [3, 11])
async def test_append_tracks_an_append_only_source_including_exact_duplicates(lab, seed):
    rng = random.Random(seed)
    lab.shop.log(25)
    await lab.table("SELECT * FROM events", "append", lookback_units=rng.choice([0, 4]))
    await lab.refresh()
    for _ in range(5):
        lab.shop.log(rng.randint(0, 8))
        if rng.random() < 0.5:
            with lab.shop.db() as db:      # the same event delivered twice
                db.execute("INSERT INTO events SELECT * FROM events ORDER BY seq DESC LIMIT 1")
        await lab.refresh()
        assert await lab.copy("SELECT seq, kind, value FROM copy") == lab.shop.rows("SELECT * FROM events")


async def test_an_incremental_run_reads_only_what_is_new(lab):
    lab.shop.add(200)
    await lab.table(lookback_units=0)
    await lab.refresh()
    lab.shop.add(3)
    lab.shop.touch(lab.shop.ids()[:2])
    table = await lab.refresh()
    run = (await lab.runs())[0]
    # The checkpoint row itself is re-read (inclusive bound), plus the 5 changes.
    assert run["rows_read"] == 6
    assert (run["rows_inserted"], run["rows_updated"], run["row_count"]) == (3, 2, 203)
    assert "seq" in run["rendered_query"] and ">= 200" in run["rendered_query"]
    assert run["window_from"] == {"kind": "number", "value": "200"}
    assert run["window_to"] == {"kind": "number", "value": "205"} == table.refresh_state["checkpoint"]
    assert table.no_rows == 203


async def test_rows_sharing_the_checkpoint_value_are_neither_lost_nor_doubled(lab):
    """Two rows stamped the same; one was committed after the refresh looked."""
    lab.shop.add(5)
    await lab.table(lookback_units=0)
    await lab.refresh()
    lab.shop.add(2, seq=5)            # same cursor value as the checkpoint
    await lab.refresh()
    await lab.matches_source()
    assert (await lab.runs())[0]["rows_inserted"] == 2


async def test_rows_without_a_cursor_value_are_carried_not_dropped(lab):
    lab.shop.add(6)
    with lab.shop.db() as db:
        db.execute("UPDATE orders SET seq = NULL, stamp = NULL WHERE id IN (2, 4)")
    await lab.table(lookback_units=0)
    with small_chunks(2):
        await lab.refresh()
    await lab.matches_source()
    first = (await lab.runs())[0]
    assert any("no 'seq' value" in e["message"] for e in first["events"])
    # A row that only later gets its value, and one that changes while still without.
    with lab.shop.db() as db:
        db.execute("UPDATE orders SET status = 'still-null' WHERE id = 2")
    lab.shop.touch([4])
    await lab.refresh()
    await lab.matches_source()


async def test_duplicate_keys_in_the_source_collapse_to_the_newest_and_are_reported(lab):
    with lab.shop.db() as db:
        db.execute("CREATE TABLE feed (id INTEGER, v TEXT, seq INTEGER)")
        db.executemany("INSERT INTO feed VALUES (?, ?, ?)", [(1, "old", 1), (1, "new", 2), (2, "only", 3)])
    await lab.table("SELECT * FROM feed", "merge")
    await lab.refresh()
    assert await lab.copy("SELECT id, v FROM copy") == [(1, "new"), (2, "only")]
    run = (await lab.runs())[0]
    assert run["status"] == "success"
    assert any(e["level"] == "warning" and "collapsed" in e["message"] for e in run["events"])


# -- first loads in windows -------------------------------------------------------

async def test_a_first_load_is_cut_into_windows_that_cover_the_source_once(lab):
    lab.shop.add(95)
    await lab.table()
    with small_chunks(10):
        table = await lab.refresh()
    await lab.matches_source()
    run = (await lab.runs())[0]
    assert run["chunks_total"] == run["chunks_done"] >= 8
    assert max(c["rows"] for c in run["chunks"]) <= 20, "no window holds most of the table"
    assert sum(c["rows"] for c in run["chunks"]) == run["rows_read"] == 95 == table.no_rows
    assert table.refresh_state.get("load") is None
    assert table.refresh_state["checkpoint"] == {"kind": "number", "value": "95"}
    assert not lab.files("*.tmp")


async def test_a_load_that_fails_part_way_resumes_instead_of_starting_over(lab):
    lab.shop.add(60)
    await lab.table()
    with small_chunks(10):
        # Three windows load, the next fails.
        with source_fails_after(3), pytest.raises(HTTPException) as error:
            await lab.refresh()
        assert "source went away" in str(error.value.detail)
        table = await lab.row()
        assert table.last_refresh_status == "error" and table.artifact_path is None
        windows = table.refresh_state["load"]["windows"]
        assert [w["status"] for w in windows].count("done") == 3
        assert lab.files("*.tmp"), "the part file is kept for the resume"

        lab.shop.add(4)                 # the source moves on while the load is down
        await lab.refresh()
    await lab.matches_source()
    resumed, failed = (await lab.runs())[:2]
    assert failed["status"] == "failed" and failed["chunks_done"] == 3 < failed["chunks_total"]
    assert resumed["status"] == "success"
    assert any("Continuing" in e["message"] for e in resumed["events"])
    # Only the windows that were not finished are read again.
    assert resumed["rows_read"] < 60
    assert not lab.files("*.tmp")


async def test_a_load_over_its_time_budget_pauses_and_the_next_run_continues_it(lab):
    lab.shop.add(50)
    await lab.table()
    with small_chunks(10), patch.object(custom_query_service, "LOAD_RUN_BUDGET_SECONDS", -1), \
            patch.object(lab.service, "_schedule_resume") as resume:
        statuses = []
        for _ in range(40):
            table = await lab.refresh()
            statuses.append(table.last_refresh_status)
            if table.last_refresh_status == "ok":
                break
        # Each run makes progress on exactly one window, then yields.
        windows = len(statuses)
        assert windows >= 4 and statuses == ["partial"] * (windows - 1) + ["ok"]
        assert resume.call_count == windows - 1
    await lab.matches_source()
    runs = await lab.runs()
    assert [r["status"] for r in runs] == ["success"] + ["partial"] * (windows - 1)
    assert [r["chunks_done"] for r in runs] == list(range(windows, 0, -1))
    assert sum(r["rows_read"] for r in runs) == 50, "no window was read twice"


async def test_a_paused_load_shows_its_progress_on_the_table(lab):
    from app.schemas.custom_query_schema import CustomQuerySchema
    lab.shop.add(30)
    await lab.table()
    with small_chunks(10), patch.object(custom_query_service, "LOAD_RUN_BUDGET_SECONDS", -1), \
            patch.object(lab.service, "_schedule_resume"):
        schema = CustomQuerySchema.from_model(await lab.refresh())
    progress = schema.load_progress
    assert progress["windows_total"] > 2 and (progress["windows_done"], progress["source_rows"]) == (1, 30)
    assert 0 < progress["rows"] < 30
    assert schema.refresh_mode == "merge" and schema.checkpoint is None


async def test_deleting_a_table_mid_load_leaves_no_file_behind(lab):
    lab.shop.add(30)
    await lab.table()
    with small_chunks(10), patch.object(custom_query_service, "LOAD_RUN_BUDGET_SECONDS", -1), \
            patch.object(lab.service, "_schedule_resume"):
        await lab.refresh()
        assert lab.files("*.tmp")
        async with async_session_maker() as db:
            connection = await db.get(Connection, lab.connection_id)
            table = await db.get(ConnectionTable, lab.table_id)
            await lab.service.delete(db, connection, table)
    assert not lab.files("*.tmp") and not lab.files("*.db")


async def test_a_sibling_table_gets_the_volume_between_windows(lab):
    """A long first load must not hold the connection for its whole length."""
    lab.shop.add(40)
    await lab.table()
    turns = []

    async def sibling():
        async with artifacts.refresh_lock(lab.connection_id):
            turns.append("sibling")

    original = lab.service._work_window

    def window(run, index):
        turns.append(index)
        if index == 0:
            # Queue the sibling while the first window holds the volume.
            asyncio.run_coroutine_threadsafe(_spawn(sibling), loop).result(5)
            time.sleep(0.3)             # let it register as waiting
        return original(run, index)

    tasks = []

    async def _spawn(fn):
        tasks.append(asyncio.create_task(fn()))

    loop = asyncio.get_running_loop()
    with small_chunks(10), patch.object(lab.service, "_work_window", window):
        await lab.refresh()
    await asyncio.gather(*tasks)
    assert turns.index("sibling") < turns.index(3), f"sibling waited for the whole load: {turns}"
    await lab.matches_source()


async def test_a_start_value_leaves_older_rows_out_and_they_do_not_count_against_the_load(lab):
    lab.shop.add(60)
    await lab.table(initial_value="41")
    with small_chunks(5), patch.object(extractor, "DEFAULT_MAX_ROWS", 25):
        # 60 rows would be refused under a 25-row ceiling; the 20 this table asks for are not.
        table = await lab.refresh()
    await lab.matches_source(f"SELECT {COLUMNS} FROM orders WHERE seq >= 41")
    run = (await lab.runs())[0]
    assert run["rows_read"] == 20 == table.no_rows and run["chunks_total"] > 1
    assert any("Source has 20 rows" in e["message"] for e in run["events"])
    lab.shop.add(3)
    await lab.refresh()
    await lab.matches_source(f"SELECT {COLUMNS} FROM orders WHERE seq >= 41")


# -- when the next run is not an increment ---------------------------------------

async def test_changing_what_decides_the_rows_rebuilds_from_scratch(lab):
    lab.shop.add(20)
    await lab.table("SELECT * FROM orders WHERE amount > 0")
    await lab.refresh()
    async with async_session_maker() as db:
        connection = await db.get(Connection, lab.connection_id)
        table = await db.get(ConnectionTable, lab.table_id)
        await lab.service.update(db, connection, table, definition_sql="SELECT * FROM orders WHERE id > 10")
    run = (await lab.runs())[0]
    assert (run["trigger"], run["mode"], run["row_count"]) == ("definition", "full", 10)
    assert any("changed" in e["message"] for e in run["events"])
    await lab.matches_source(f"SELECT {COLUMNS} FROM orders WHERE id > 10")


async def test_changing_only_the_schedule_or_lookback_does_not_rebuild(lab):
    lab.shop.add(5)
    await lab.table()
    await lab.refresh()
    async with async_session_maker() as db:
        connection = await db.get(Connection, lab.connection_id)
        table = await db.get(ConnectionTable, lab.table_id)
        await lab.service.update(db, connection, table, refresh_interval_minutes=5,
                                 refresh_config={"lookback_units": 9, "full_refresh_every_days": 3})
        assert table.refresh_config["lookback_units"] == 9
        assert table.refresh_config["unique_key"] == ["id"], "settings not sent keep their value"
    assert len(await lab.runs()) == 1


async def test_deleted_rows_leave_at_the_next_full_refresh(lab):
    lab.shop.add(10)
    await lab.table(full_refresh_every_days=2)
    await lab.refresh()
    lab.shop.remove([3, 4])
    await lab.refresh()
    assert len(await lab.copy()) == 10, "an increment cannot see a deletion"

    await lab.refresh(full=True)
    await lab.matches_source()
    assert (await lab.runs())[0]["mode"] == "full"

    # …and without being asked, once the configured interval has passed.
    lab.shop.remove([5])
    async with async_session_maker() as db:
        table = await db.get(ConnectionTable, lab.table_id)
        table.refresh_state = {**table.refresh_state, "last_full_refresh_at": "2020-01-01T00:00:00"}
        await db.commit()
    await lab.refresh()
    await lab.matches_source()
    run = (await lab.runs())[0]
    assert run["mode"] == "full" and any("Scheduled full refresh" in e["message"] for e in run["events"])


async def test_a_new_source_column_rebuilds_instead_of_failing_every_run(lab):
    lab.shop.add(8)
    await lab.table()
    await lab.refresh()
    with lab.shop.db() as db:
        db.execute("ALTER TABLE orders ADD COLUMN region TEXT DEFAULT 'emea'")
    lab.shop.add(1)
    table = await lab.refresh()
    assert table.last_refresh_status == "ok"
    assert "region" in [c["name"] for c in table.columns]
    assert await lab.copy("SELECT id, region FROM copy") == lab.shop.rows("SELECT id, region FROM orders")
    run = (await lab.runs())[0]
    assert run["mode"] == "full" and any(e["level"] == "warning" and "columns changed" in e["message"]
                                         for e in run["events"])


async def test_a_text_cursor_works_but_one_that_could_break_a_query_is_refused(lab):
    lab.shop.add(12)
    await lab.table(cursor_column="stamp", lookback_seconds=0)
    await lab.refresh()
    lab.shop.add(2)
    lab.shop.touch([1])
    table = await lab.refresh()
    await lab.matches_source()
    assert table.refresh_state["checkpoint"]["kind"] == "text"

    with lab.shop.db() as db:
        db.execute("UPDATE orders SET stamp = 'zzz'' OR 1=1 --' WHERE id = 2")
    await lab.refresh(full=True)
    with pytest.raises(HTTPException) as error:
        await lab.refresh()
    assert "cannot be used as a checkpoint" in str(error.value.detail)


async def test_renaming_a_table_keeps_refreshing_and_serving_it(lab):
    lab.shop.add(12)
    await lab.table()
    await lab.refresh()
    lab.shop.add(2)
    async with async_session_maker() as db:
        connection = await db.get(Connection, lab.connection_id)
        table = await db.get(ConnectionTable, lab.table_id)
        await lab.service.update(db, connection, table, name="orders_renamed")
        await db.refresh(table)
        assert (table.last_refresh_status, table.no_rows) == ("ok", 14)
        frame = CustomQueryService.build_fast_client([table], identity=rls.ANONYMOUS).execute_query(
            "SELECT count(*) AS n FROM orders_renamed")
        assert int(frame["n"][0]) == 14
    assert (await lab.runs())[0]["mode"] == "incremental", "a rename is not a reason to reload the table"
    lab.shop.touch([1])
    await lab.refresh()
    assert await lab.copy(f"SELECT {COLUMNS} FROM orders_renamed") == lab.shop.rows()


async def test_names_the_loader_keeps_for_itself_cannot_be_used(lab):
    for name in ("bow__staging", "BOW__progress", "bow__anything"):
        with pytest.raises(HTTPException) as error:
            lab.service.validate_name(name)
        assert "reserved" in error.value.detail
    assert lab.service.validate_name("bow_orders") == "bow_orders"


async def test_a_full_refresh_restarts_a_load_that_is_in_progress(lab):
    lab.shop.add(40)
    await lab.table()
    with small_chunks(10), patch.object(custom_query_service, "LOAD_RUN_BUDGET_SECONDS", -1), \
            patch.object(lab.service, "_schedule_resume"):
        await lab.refresh()
        (part,) = lab.files("*.tmp")
    with small_chunks(10):
        table = await lab.refresh(full=True)
    assert table.last_refresh_status == "ok" and not part.exists() and not lab.files("*.tmp")
    await lab.matches_source()
    run = (await lab.runs())[0]
    assert run["rows_read"] == 40 and any("restarted" in e["message"] for e in run["events"])


async def test_a_rebuild_that_keeps_failing_stops_blocking_what_is_new(lab):
    """A table already being served must not freeze because its periodic
    rebuild cannot finish."""
    lab.shop.add(30)
    await lab.table()
    with small_chunks(10):
        await lab.refresh()
        # One window loads, then the source stops answering: run after run
        # fails without getting any further.
        for attempt in range(custom_query_service.MAX_LOAD_FAILURES):
            with source_fails_after(1 if attempt == 0 else 0), pytest.raises(HTTPException):
                await lab.refresh(full=attempt == 0)
        failing = await lab.row()
        assert failing.refresh_state["load"]["failures"] == custom_query_service.MAX_LOAD_FAILURES
        assert failing.no_rows == 30, "the served table is untouched while the rebuild fails"

        lab.shop.add(5)
        table = await lab.refresh()
    assert table.last_refresh_status == "ok" and table.refresh_state.get("load") is None
    assert table.refresh_state["full_refresh_retry_after"] > "2026"
    await lab.matches_source()
    run = (await lab.runs())[0]
    assert run["mode"] == "incremental"
    assert any(e["level"] == "warning" and "trying the rebuild again" in e["message"] for e in run["events"])
    assert not lab.files("*.tmp")
    # …and it does not go straight back to rebuilding on the next tick.
    async with async_session_maker() as db:
        row = await db.get(ConnectionTable, lab.table_id)
        row.refresh_state = {**row.refresh_state, "last_full_refresh_at": "2020-01-01T00:00:00"}
        await db.commit()
    await lab.refresh()
    assert (await lab.runs())[0]["mode"] == "incremental"


async def test_a_first_load_that_keeps_failing_starts_over_rather_than_retrying_one_window_forever(lab):
    lab.shop.add(30)
    await lab.table()
    with small_chunks(10):
        for attempt in range(custom_query_service.MAX_LOAD_FAILURES):
            with source_fails_after(1 if attempt == 0 else 0), pytest.raises(HTTPException):
                await lab.refresh()
        (stuck,) = lab.files("*.tmp")
        await lab.refresh()
    assert not stuck.exists()
    await lab.matches_source()
    assert any("starting it over" in e["message"] for e in (await lab.runs())[0]["events"])


async def test_a_load_whose_file_is_on_another_host_is_left_to_that_host_for_a_while(lab):
    from datetime import datetime, timedelta
    lab.shop.add(30)
    await lab.table()
    with small_chunks(10), patch.object(custom_query_service, "LOAD_RUN_BUDGET_SECONDS", -1), \
            patch.object(lab.service, "_schedule_resume"):
        await lab.refresh()
    (part,) = lab.files("*.tmp")
    elsewhere = part.with_name("elsewhere.tmp")

    async def as_if_on_another_host(updated_at):
        async with async_session_maker() as db:
            row = await db.get(ConnectionTable, lab.table_id)
            load = {**row.refresh_state["load"], "host": "another-pod", "path": str(elsewhere),
                    "updated_at": updated_at.isoformat(timespec="seconds")}
            row.refresh_state = {**row.refresh_state, "load": load}
            await db.commit()

    await as_if_on_another_host(datetime.utcnow())
    with patch.object(lab.service, "_schedule_resume") as handed_back:
        table = await lab.refresh()
    assert handed_back.call_count == 1 and table.artifact_path is None
    assert len(await lab.runs()) == 1, "deferring is not a run"

    # Its owner went quiet: the load is started again here.
    await as_if_on_another_host(datetime.utcnow() - timedelta(hours=2))
    with small_chunks(10):
        table = await lab.refresh()
    assert table.last_refresh_status == "ok"
    await lab.matches_source()


async def test_a_request_cancelled_between_windows_does_not_leave_the_table_running(lab):
    lab.shop.add(30)
    await lab.table()
    reached = asyncio.Event()

    turns = {"n": 0}

    async def held(connection_id):
        turns["n"] += 1
        if turns["n"] == 2:                 # one window is in; hold before the next
            reached.set()
            await asyncio.sleep(30)

    with small_chunks(10), patch.object(lab.service, "_yield_to_waiters", held):
        task = asyncio.create_task(lab.refresh())
        await asyncio.wait_for(reached.wait(), 10)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    table = await lab.row()
    assert table.last_refresh_status == "error" and table.last_refresh_error
    assert (await lab.runs())[0]["status"] == "cancelled"
    with small_chunks(10):
        await lab.refresh()                 # and what it had loaded is not wasted
    await lab.matches_source()
    assert any("Continuing" in e["message"] for e in (await lab.runs())[0]["events"])


async def test_a_window_committed_but_not_recorded_is_not_loaded_twice_on_resume(lab):
    """Append has no key to collapse on: a window loaded twice would be duplicates."""
    lab.shop.log(40)
    await lab.table("SELECT * FROM events", "append")
    original = lab.service._save_state
    calls = {"n": 0}

    async def state_store_fails_once(run):
        calls["n"] += 1
        if calls["n"] == 3:                 # the plan, the first window, then this one
            raise ConnectionError("app database went away")
        await original(run)

    with small_chunks(10):
        with patch.object(lab.service, "_save_state", state_store_fails_once), \
                pytest.raises(ConnectionError):
            await lab.refresh()
        await lab.refresh()
    assert await lab.copy("SELECT seq, kind, value FROM copy") == lab.shop.rows("SELECT * FROM events")


# -- failure never costs the served data -------------------------------------------

async def test_a_failed_increment_keeps_the_table_and_the_checkpoint(lab):
    lab.shop.add(10)
    await lab.table()
    before = await lab.refresh()
    served = await lab.copy()
    lab.shop.add(3)
    with source_fails_after(0), pytest.raises(HTTPException):
        await lab.refresh()
    after = await lab.row()
    assert (after.artifact_path, after.refresh_state) == (before.artifact_path, before.refresh_state)
    assert after.last_refresh_status == "error" and await lab.copy() == served
    assert lab.files() == {Path(after.artifact_path)} and not lab.files("*.tmp")
    await lab.refresh()                 # and the next one simply catches up
    await lab.matches_source()


async def test_a_refresh_that_loses_most_of_its_rows_is_not_published(lab):
    lab.shop.add(100)
    await lab.table("SELECT * FROM orders", "full", row_drop_guard_pct=50)
    before = await lab.refresh()
    lab.shop.remove(lab.shop.ids()[:80])
    with pytest.raises(HTTPException) as error:
        await lab.refresh()
    assert "down 80%" in str(error.value.detail)
    after = await lab.row()
    assert after.artifact_path == before.artifact_path and after.no_rows == 100
    assert lab.files() == {Path(before.artifact_path)}
    run = (await lab.runs())[0]
    assert run["status"] == "failed" and "down 80%" in run["error"]

    async with async_session_maker() as db:
        connection = await db.get(Connection, lab.connection_id)
        table = await db.get(ConnectionTable, lab.table_id)
        await lab.service.update(db, connection, table, refresh_config={"row_drop_guard_pct": None})
    assert (await lab.refresh()).no_rows == 20


async def test_a_table_that_predates_these_settings_refreshes_as_it_always_did(lab):
    lab.shop.add(100)
    async with async_session_maker() as db:
        row = ConnectionTable(connection_id=lab.connection_id, name="copy", kind=KIND_BOW,
                              definition_sql="SELECT * FROM orders", columns=[], pks=[], fks=[])
        db.add(row)
        await db.commit()
        lab.table_id = row.id
    await lab.refresh()
    lab.shop.remove(lab.shop.ids()[:95])
    table = await lab.refresh()         # no guard was ever configured for it
    assert (table.refresh_mode, table.refresh_state, table.no_rows) == ("full", None, 5)
    assert [r["mode"] for r in await lab.runs()] == ["full", "full"]


# -- run history ---------------------------------------------------------------------

async def test_every_refresh_that_starts_leaves_exactly_one_finished_run(lab):
    lab.shop.add(10)
    await lab.table()
    await lab.refresh(trigger="create")
    with source_fails_after(0), pytest.raises(HTTPException):
        await lab.refresh(trigger="schedule")
    await lab.refresh()
    runs = await lab.runs()
    assert [(r["trigger"], r["status"]) for r in runs] == [
        ("manual", "success"), ("schedule", "failed"), ("create", "success")]
    for run in runs:
        assert run["started_at"] and run["finished_at"] and run["elapsed_ms"] >= 0
        assert run["inputs"] == {"connection_id": lab.connection_id}
    assert runs[1]["error"] and runs[0]["error"] is None
    assert runs[0]["schema_fingerprint"] == runs[2]["schema_fingerprint"]


async def test_history_pages_newest_first_and_a_bad_cursor_returns_nothing(lab):
    lab.shop.add(3)
    await lab.table()
    for _ in range(5):
        await lab.refresh()
    async with async_session_maker() as db:
        first = await custom_table_runs.list_runs(db, lab.table_id, limit=2)
        second = await custom_table_runs.list_runs(db, lab.table_id, limit=2, before=first["next_before"])
        rest = await custom_table_runs.list_runs(db, lab.table_id, limit=2, before=second["next_before"])
        bad = await custom_table_runs.list_runs(db, lab.table_id, before="not-a-cursor")
        other = await custom_table_runs.get_run(db, str(uuid.uuid4()), first["runs"][0]["id"])
    ids = [r["id"] for page in (first, second, rest) for r in page["runs"]]
    assert len(ids) == len(set(ids)) == 5 and first["has_more"] and not rest["has_more"]
    assert bad == {"runs": [], "has_more": False, "next_before": None}
    assert other is None, "a run is only readable through its own table"


async def test_a_run_that_stopped_reporting_reads_as_interrupted(lab):
    from datetime import datetime, timedelta
    lab.shop.add(2)
    await lab.table()
    run_id = await custom_table_runs.start(connection_table_id=lab.table_id, organization_id=lab.org_id,
                                           trigger="schedule")
    assert (await lab.runs())[0]["status"] == "running"
    async with async_session_maker() as db:
        run = await db.get(CustomTableRun, run_id)
        run.started_at = run.updated_at = datetime.utcnow() - timedelta(hours=2)
        await db.commit()
    assert (await lab.runs())[0]["status"] == "interrupted"


async def test_old_runs_are_purged_and_recent_ones_kept(lab):
    from datetime import datetime, timedelta
    lab.shop.add(2)
    await lab.table()
    await lab.refresh()
    await lab.refresh()
    async with async_session_maker() as db:
        oldest = (await db.execute(select(CustomTableRun).order_by(CustomTableRun.started_at))).scalars().first()
        oldest.started_at = datetime.utcnow() - timedelta(days=40)
        await db.commit()
    assert await custom_table_runs.purge_old_runs() == 1
    assert len(await lab.runs()) == 1


async def test_recording_a_run_never_breaks_a_refresh(lab):
    lab.shop.add(4)
    await lab.table()

    async def broken(work):
        raise RuntimeError("history store is down")

    with patch.object(custom_table_runs, "_write", broken):
        table = await lab.refresh()
    assert table.last_refresh_status == "ok"
    await lab.matches_source()


# -- what leaves the table ------------------------------------------------------------

async def test_lineage_columns_never_reach_a_query_or_the_column_list(lab):
    lab.shop.add(3)
    await lab.table()
    table = await lab.refresh()
    assert not any(c["name"].startswith("_bow_") for c in table.columns)
    for identity_rows in ([table],):
        client = CustomQueryService.build_fast_client(identity_rows, identity=rls.ANONYMOUS)
        frame = client.execute_query("SELECT * FROM copy")
        assert list(frame.columns) == COLUMNS.split(", ")
        import duckdb
        with pytest.raises(duckdb.Error):
            client.execute_query("SELECT _bow_run_id FROM copy")
        # Nor by reaching past the view to the attached file.
        with pytest.raises(ValueError):
            client.execute_query("SELECT * FROM bow_a0.copy")
    # They are there for us: each row names the run that wrote it.
    run_ids = {r["id"] for r in await lab.runs()}
    assert {r[0] for r in await lab.copy("SELECT DISTINCT _bow_run_id FROM copy")} <= run_ids


async def test_publishing_announces_the_table_and_run(lab):
    lab.shop.add(3)
    await lab.table()
    seen = []
    table_events.subscribe(table_events.TABLE_PUBLISHED, seen.append)
    try:
        await lab.refresh()
        with source_fails_after(0), pytest.raises(HTTPException):
            await lab.refresh()
    finally:
        table_events.unsubscribe(table_events.TABLE_PUBLISHED, seen.append)
    run = (await lab.runs())[-1]
    assert seen == [{"table_id": lab.table_id, "connection_id": lab.connection_id, "run_id": run["id"],
                     "rows": 3, "schema_fingerprint": run["schema_fingerprint"]}]


async def test_the_sweep_keeps_a_load_in_progress_and_removes_an_abandoned_one(lab):
    from app.services.fast_artifact_maintenance import purge_orphaned_fast_artifacts
    lab.shop.add(30)
    await lab.table()
    with small_chunks(10), patch.object(custom_query_service, "LOAD_RUN_BUDGET_SECONDS", -1), \
            patch.object(lab.service, "_schedule_resume"):
        await lab.refresh()
    (part,) = lab.files("*.tmp")
    stray = part.with_name(f"{uuid.uuid4().hex}.tmp")
    stray.write_bytes(b"abandoned")
    long_ago = time.time() - 3 * 86400
    for path in (part, stray):
        os.utime(path, (long_ago, long_ago))
    await purge_orphaned_fast_artifacts()
    assert lab.files("*.tmp") == {part}
    with small_chunks(10):
        await lab.refresh()
        await lab.refresh()
    await lab.matches_source()


async def test_settings_are_checked_when_saved(lab):
    for mode, config, problem in [
        ("merge", {"cursor_column": "seq"}, "key"),
        ("append", {}, "cursor"),
        ("nonsense", {}, "mode"),
    ]:
        with pytest.raises(HTTPException) as error:
            lab.service.refresh_settings(mode, config, "SELECT 1")
        assert error.value.status_code == 400 and problem in error.value.detail.lower()


async def test_a_cursor_or_key_missing_from_the_result_fails_with_the_column_names(lab):
    lab.shop.add(3)
    await lab.table(cursor_column="updated")
    with pytest.raises(HTTPException) as error:
        await lab.refresh()
    assert "updated" in str(error.value.detail)
    table = await lab.row()
    assert table.artifact_path is None and not lab.files() and not lab.files("*.tmp")
    assert (table.refresh_state or {}).get("load") is None
