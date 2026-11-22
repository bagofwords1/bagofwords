"""Which results can be saved as a cached table, and what a first load reports.

"Save as cached table" turns the query behind a result the agent produced into
a custom table. Two promises are pinned here:

- only a result with ONE definition worth keeping is offered: a single text
  query that the connection itself answered. Anything else has no query to
  save, or would save the wrong one.
- a one-shot first load says how far it has got while it runs, so the person
  who started it watches rows arrive instead of a spinner, and says nothing
  once it has finished.
"""
import asyncio
import sqlite3
import time
from contextlib import closing
from unittest.mock import patch

import pandas as pd
import pytest

from app.ai.code_execution.code_execution import StreamingCodeExecutor
from app.ai.schemas.codegen import CodeGenContext, CodeGenRequest
from app.dependencies import async_session_maker
from app.services import custom_query_service as service_module
from app.services import custom_table_runs as runs
from app.services.custom_query_service import single_query_of
from tests.unit.test_fast_artifact_lifecycle import sandbox  # noqa: F401 - fixture

CONN = "11111111-1111-1111-1111-111111111111"
OTHER = "22222222-2222-2222-2222-222222222222"


def _result(queries, timings):
    return {"executed_queries": queries, "query_timings": timings}


def _timing(**over):
    return {"index": 0, "rows": 3, "connection_id": CONN, "client_key": "Sales:warehouse", **over}


# -- which result has a query to save ----------------------------------------

def test_a_single_query_on_the_connection_is_the_definition():
    assert single_query_of(_result(["  SELECT 1  "], [_timing()]), CONN) == "SELECT 1"


@pytest.mark.parametrize("result", [
    None,
    {},
    _result([], []),
    _result(["SELECT 1", "SELECT 2"], [_timing(), _timing(index=1)]),
    _result(["SELECT 1"], [_timing(), _timing(index=1)]),
    _result([{"universe": "Sales"}], [_timing()]),
    _result(["   "], [_timing()]),
], ids=["no result", "empty", "no queries", "two queries", "two timings", "structured request", "blank"])
def test_a_result_without_exactly_one_text_query_has_none(result):
    assert single_query_of(result, CONN) is None


def test_a_query_another_connection_answered_is_not_this_connections():
    assert single_query_of(_result(["SELECT 1"], [_timing(connection_id=OTHER)]), CONN) is None
    assert single_query_of(_result(["SELECT 1"], [_timing(connection_id=None)]), CONN) is None


def test_a_parameterized_result_has_no_single_definition():
    """Its captured text holds one viewer's values, and its card can be re-run
    with others: the query on record is then not the query behind the rows."""
    result = {**_result(["SELECT * FROM sales WHERE month = '2026-01'"], [_timing()]),
              "parameters": [{"name": "month", "type": "string"}]}
    assert single_query_of(result, CONN) is None
    assert single_query_of({**result, "parameters": []}, CONN) is not None


def test_a_failed_query_is_not_offered():
    assert single_query_of(_result(["SELECT 1"], [_timing(error="timeout")]), CONN) is None


def test_a_result_read_from_a_cached_table_is_not_cached_again():
    cached = _timing(client_key="Sales:warehouse::fast")
    assert single_query_of(_result(["SELECT * FROM revenue_summary"], [cached]), CONN) is None


# -- the timing record says which connection answered ------------------------

class _Source:
    def execute_query(self, query=None, *args, **kwargs):
        return pd.DataFrame({"n": [1, 2, 3]})


@pytest.mark.db
@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["subprocess", "inprocess"])
async def test_a_query_timing_names_the_connection_that_answered(monkeypatch, mode):
    """The result's viewer decides whether to offer caching from the timing
    record alone, so it has to say where the query ran."""
    monkeypatch.setenv("BOW_CODE_SANDBOX", mode)
    if mode == "inprocess":
        # In-process code runs only in a development environment; the test
        # suite runs with ENVIRONMENT=production, so the test names it.
        monkeypatch.setenv("ENVIRONMENT", "development")
    source = _Source()
    source._bow_connection_id = CONN
    source._bow_data_source_id = OTHER
    source._bow_client_key = "Sales:warehouse"

    async def codegen(**kwargs):
        return 'def generate_df(ds_clients, excel_files):\n    return ds_clients["Sales:warehouse"].execute_query("q")\n'

    events = [e async for e in StreamingCodeExecutor().generate_and_execute_stream_v2(
        request=CodeGenRequest(context=CodeGenContext(user_prompt="rows", schemas_excerpt=""), retries=1),
        ds_clients={"Sales:warehouse": source}, excel_files=[], code_generator_fn=codegen,
    )]
    done = events[-1]["payload"]
    assert done["df"] is not None, done.get("errors")
    (timing,) = done["query_timings"]
    assert timing["connection_id"] == CONN
    assert timing["data_source_id"] == OTHER
    assert timing["client_key"] == "Sales:warehouse"


# -- a first load reports its rows --------------------------------------------

ROWS = 4000


@pytest.mark.db
@pytest.mark.asyncio
@pytest.mark.parametrize("counted", [True, False], ids=["source counts rows", "source cannot count"])
async def test_a_running_first_load_reports_its_rows(sandbox, counted):  # noqa: F811
    """While a one-shot load runs, its table reports the rows read so far,
    against the source's own count where the source gives one (a Power BI
    model does; SQLite does not). Once it finishes there is nothing to report."""
    from app.data_sources.fast import extractor, loader

    with closing(sqlite3.connect(sandbox.source)) as db, db:
        db.execute("DROP TABLE orders")
        db.execute("CREATE TABLE orders (value INTEGER)")
        db.executemany("INSERT INTO orders VALUES (?)", [(i,) for i in range(ROWS)])

    real_normalize = loader.normalize_table

    def slow_normalize(raw):
        time.sleep(0.02)      # each batch takes a moment, so the load is observable
        return real_normalize(raw)

    def estimate(client, sql):
        return extractor.Estimate(rows=ROWS if counted else None)

    seen = []
    with patch.object(extractor, "BATCH_ROWS", 100), \
            patch.object(extractor, "estimate", estimate), \
            patch.object(loader, "normalize_table", slow_normalize), \
            patch.object(service_module, "PROGRESS_INTERVAL_SECONDS", 0.02):
        async with async_session_maker() as db:
            connection, table = await sandbox.load(db)
            load = asyncio.create_task(sandbox.service.refresh(db, connection, table))
            while not load.done():
                async with async_session_maker() as watcher:
                    live = await runs.live_progress(watcher, [sandbox.table_id])
                if live:
                    seen.append(live[sandbox.table_id])
                await asyncio.sleep(0.03)
            await load

    assert seen, "a running load never reported progress"
    rows = [p["rows"] for p in seen]
    assert rows == sorted(rows), "rows went backwards"
    assert any(0 < r < ROWS for r in rows), f"never seen part-way: {rows}"
    counts = {p["source_rows"] for p in seen if p["rows"]}
    assert counts == ({ROWS} if counted else {None})

    async with async_session_maker() as db:
        assert await runs.live_progress(db, [sandbox.table_id]) == {}
        _, table = await sandbox.load(db)
        assert table.no_rows == ROWS
