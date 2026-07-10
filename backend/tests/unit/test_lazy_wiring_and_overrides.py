"""Stage-4 wiring + bespoke-override tests for the lazy (out-of-core) query path.

Covers the pieces test_lazy_frame.py doesn't:
  - QueryCapturingClientWrapper.execute_query_lazy (capture, metering,
    ResultTooLargeError bookkeeping, wall-clock timeout);
  - the bespoke client overrides (NetSuite, Salesforce, BigQuery, ClickHouse,
    Spark, Redshift) against fake sessions/cursors — no network;
  - concurrency across threads sharing one spill dir, and aexecute_query_lazy.
"""

import asyncio
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

import pandas as pd
import pyarrow as pa
import pytest

from app.data_sources.clients.base import DataSourceClient
from app.data_sources.clients.lazy_frame import (
    LazyFrame,
    ResultTooLargeError,
    lazy_from_dataframe,
)


# =============================================================================
# Helpers
# =============================================================================

class TinyClient(DataSourceClient):
    """Minimal concrete client: no _lazy_strategy, so execute_query_lazy uses
    the generic materialize-then-spill fallback."""

    def __init__(self, frames=None):
        self.frames = frames or {}
        self.calls = []

    def execute_query(self, sql: str) -> pd.DataFrame:
        self.calls.append(sql)
        return self.frames.get(sql, pd.DataFrame({"x": [1, 2, 3]}))

    # abstract-method stubs
    def test_connection(self):
        return {"success": True}

    def get_schemas(self):
        return []

    def get_schema(self, table_name):
        return None

    def prompt_schema(self):
        return ""

    def system_prompt(self):
        return ""

    @property
    def description(self):
        return "tiny"


# =============================================================================
# QueryCapturingClientWrapper.execute_query_lazy (stage-4 wiring)
# =============================================================================

def _make_wrapper(client, timeout=30, lazy_enabled=True):
    from app.ai.code_execution.code_execution import QueryCapturingClientWrapper

    queries, timings = [], []
    wrapper = QueryCapturingClientWrapper(
        client, queries, timings, query_timeout_seconds=timeout,
        lazy_enabled=lazy_enabled,
    )
    return wrapper, queries, timings


class _FakeOrgSettings:
    """Minimal stand-in for OrganizationSettingsConfig.get_config."""

    def __init__(self, **values):
        self._values = values

    def get_config(self, key, default=None):
        return self._values.get(key, default)


class TestWrapperLazyPassthrough:
    def test_captures_query_and_meters_from_spill(self):
        client = TinyClient({"SELECT 1": pd.DataFrame({"a": [1, 2, 3], "b": list("xyz")})})
        wrapper, queries, timings = _make_wrapper(client)

        with wrapper.execute_query_lazy("SELECT 1") as lf:
            assert isinstance(lf, LazyFrame)
            assert lf.row_count() == 3
        assert queries == ["SELECT 1"]
        assert len(timings) == 1
        t = timings[0]
        assert t["lazy"] is True
        assert t["rows"] == 3
        assert t["result_bytes"] > 0
        assert t["sql"] == "SELECT 1"

    def test_result_too_large_recorded_and_propagates(self):
        class ExplodingClient(TinyClient):
            def execute_query_lazy(self, sql):
                raise ResultTooLargeError(rows=99, byte_estimate=1234, limit_desc="test cap")

        wrapper, queries, timings = _make_wrapper(ExplodingClient())
        with pytest.raises(ResultTooLargeError):
            wrapper.execute_query_lazy("SELECT big")
        assert queries == ["SELECT big"]
        assert timings[0]["error_type"] == "result_too_large"
        assert timings[0]["rows"] == 99
        assert timings[0]["lazy"] is True

    def test_wall_clock_timeout_applies_to_lazy_path(self):
        from app.ai.code_execution.code_execution import QueryTimeoutError

        class SlowClient(TinyClient):
            def execute_query_lazy(self, sql):
                time.sleep(5)

        wrapper, _, timings = _make_wrapper(SlowClient(), timeout=1)
        with pytest.raises(QueryTimeoutError):
            wrapper.execute_query_lazy("SELECT slow")
        assert timings[0]["error_type"] == "timeout"
        assert timings[0]["lazy"] is True

    def test_generic_error_recorded_and_propagates(self):
        class BrokenClient(TinyClient):
            def execute_query_lazy(self, sql):
                raise RuntimeError("boom")

        wrapper, _, timings = _make_wrapper(BrokenClient())
        with pytest.raises(RuntimeError, match="boom"):
            wrapper.execute_query_lazy("SELECT x")
        assert timings[0]["error"].startswith("boom")
        assert timings[0]["lazy"] is True

    def test_disabled_by_default(self):
        from app.ai.code_execution.code_execution import LazyQueriesDisabledError

        wrapper, queries, timings = _make_wrapper(TinyClient(), lazy_enabled=False)
        with pytest.raises(LazyQueriesDisabledError):
            wrapper.execute_query_lazy("SELECT 1")
        # rejected before capture — nothing ran, nothing to meter
        assert queries == []
        assert timings == []

    def test_wrap_clients_defaults_to_disabled_without_org_settings(self):
        from app.ai.code_execution.code_execution import (
            LazyQueriesDisabledError,
            wrap_clients_for_capture,
        )

        wrapped = wrap_clients_for_capture({"db": TinyClient()}, [], [])
        with pytest.raises(LazyQueriesDisabledError):
            wrapped["db"].execute_query_lazy("SELECT 1")

    def test_wrap_clients_honors_org_opt_in(self):
        from app.ai.code_execution.code_execution import wrap_clients_for_capture

        class _Flag:
            value = True

        queries, timings = [], []
        wrapped = wrap_clients_for_capture(
            {"db": TinyClient()}, queries, timings,
            organization_settings=_FakeOrgSettings(enable_lazy_queries=_Flag()),
        )
        with wrapped["db"].execute_query_lazy("SELECT 1") as lf:
            assert lf.row_count() == 3
        assert queries == ["SELECT 1"]

    def test_async_lazy_respects_gate_and_instrumentation(self):
        from app.ai.code_execution.code_execution import LazyQueriesDisabledError

        # disabled: the wrapper's async method must raise, not fall through
        # __getattr__ to the raw client's ungated aexecute_query_lazy
        wrapper, _, _ = _make_wrapper(TinyClient(), lazy_enabled=False)
        with pytest.raises(LazyQueriesDisabledError):
            asyncio.run(wrapper.aexecute_query_lazy("SELECT 1"))

        # enabled: runs and is captured/metered like the sync path
        wrapper, queries, timings = _make_wrapper(
            TinyClient({"q": pd.DataFrame({"v": [1, 2]})})
        )

        async def main():
            lf = await wrapper.aexecute_query_lazy("q")
            try:
                return lf.row_count()
            finally:
                lf.close()

        assert asyncio.run(main()) == 2
        assert queries == ["q"]
        assert timings[0]["lazy"] is True

    def test_resolve_lazy_enabled_handles_raw_and_feature_values(self):
        from app.ai.code_execution.code_execution import resolve_lazy_enabled

        class _Flag:
            def __init__(self, value):
                self.value = value

        assert resolve_lazy_enabled(None) is False
        assert resolve_lazy_enabled(_FakeOrgSettings()) is False
        assert resolve_lazy_enabled(_FakeOrgSettings(enable_lazy_queries=True)) is True
        assert resolve_lazy_enabled(_FakeOrgSettings(enable_lazy_queries=_Flag(True))) is True
        assert resolve_lazy_enabled(_FakeOrgSettings(enable_lazy_queries=_Flag(False))) is False


# =============================================================================
# NetSuite: offset pagination, nested-cell encoding, raise-at-cap
# =============================================================================

class _FakeResp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


class _FakeNetsuiteSession:
    """Serves `pages` keyed by offset; records every request."""

    def __init__(self, pages):
        self.pages = pages
        self.requests = []

    def post(self, url, json=None, params=None, timeout=None):
        self.requests.append(params)
        offset = params["offset"]
        return _FakeResp(self.pages.get(offset, {"items": [], "hasMore": False}))


def _make_netsuite(pages):
    from app.data_sources.clients.netsuite_client import NetsuiteClient

    client = NetsuiteClient(
        account_id="ACCT", consumer_key="k", consumer_secret="s",
        token_id="t", token_secret="ts",
    )
    session = _FakeNetsuiteSession(pages)

    @contextmanager
    def fake_connect():
        yield session

    client.connect = fake_connect
    return client, session


class TestNetsuiteLazy:
    def test_paginates_and_encodes_nested(self):
        pages = {
            0: {"items": [{"id": i, "meta": {"k": i}} for i in range(1000)], "hasMore": True},
            1000: {"items": [{"id": 1000, "meta": {"k": 1000}}], "hasMore": False},
        }
        client, session = _make_netsuite(pages)
        with client.execute_query_lazy("SELECT * FROM transaction") as lf:
            df = lf.to_df()
        assert len(df) == 1001
        assert json.loads(df["meta"].iloc[0]) == {"k": 0}
        # two pages fetched, offsets 0 and 1000
        assert [r["offset"] for r in session.requests] == [0, 1000]

    def test_raises_at_cap_instead_of_truncating(self, monkeypatch):
        monkeypatch.setenv("BOW_LAZY_MAX_ROWS", "5")
        pages = {0: {"items": [{"id": i} for i in range(7)], "hasMore": False}}
        client, _ = _make_netsuite(pages)
        with pytest.raises(ResultTooLargeError):
            client.execute_query_lazy("SELECT * FROM big")

    def test_http_error_propagates(self):
        client, session = _make_netsuite({})
        session.post = lambda *a, **k: _FakeResp({"error": "nope"}, status=400)
        with pytest.raises(RuntimeError, match="SuiteQL error"):
            client.execute_query_lazy("SELECT 1")


# =============================================================================
# Salesforce: query/query_more paging, attributes stripped
# =============================================================================

class _FakeSalesforce:
    def __init__(self, pages):
        self.pages = pages  # list of result dicts
        self.query_more_calls = []

    def query(self, soql):
        return self.pages[0]

    def query_more(self, url, identifier_is_url=False):
        self.query_more_calls.append((url, identifier_is_url))
        return self.pages[1]


class TestSalesforceLazy:
    def test_pages_and_strips_attributes(self):
        from app.data_sources.clients.salesforce_client import SalesforceClient

        pages = [
            {
                "records": [
                    {"attributes": {"type": "Account"}, "Id": "1", "Owner": {"Name": "a"}},
                    {"attributes": {"type": "Account"}, "Id": "2", "Owner": {"Name": "b"}},
                ],
                "done": False,
                "nextRecordsUrl": "/next",
            },
            {
                "records": [{"attributes": {"type": "Account"}, "Id": "3", "Owner": None}],
                "done": True,
            },
        ]
        client = SalesforceClient.__new__(SalesforceClient)
        sf = _FakeSalesforce(pages)

        @contextmanager
        def fake_connect():
            yield sf

        client.connect = fake_connect
        with client.execute_query_lazy("SELECT Id FROM Account") as lf:
            df = lf.to_df()
        assert len(df) == 3
        assert "attributes" not in df.columns
        assert json.loads(df["Owner"].iloc[0]) == {"Name": "a"}
        assert sf.query_more_calls == [("/next", True)]


# =============================================================================
# BigQuery / ClickHouse: Arrow streams, empty-stream schema recovery
# =============================================================================

class TestBigqueryLazy:
    def _client(self, batches, schema_table):
        from app.data_sources.clients.bigquery_client import BigqueryClient

        # __init__ validates real credentials; bypass it and set only what
        # execute_query_lazy reads.
        client = BigqueryClient.__new__(BigqueryClient)
        client.maximum_bytes_billed = None
        client.use_query_cache = False

        class FakeResult:
            def to_arrow_iterable(self):
                return iter(batches)

            def to_arrow(self):
                return schema_table

        class FakeJob:
            def result(self):
                return FakeResult()

        class FakeConn:
            def query(self, sql, job_config=None):
                return FakeJob()

        @contextmanager
        def fake_connect():
            yield FakeConn()

        client.connect = fake_connect
        return client

    def test_streams_batches(self):
        batches = [pa.record_batch({"a": [1, 2]}), pa.record_batch({"a": [3]})]
        client = self._client(batches, pa.table({"a": pa.array([], type=pa.int64())}))
        with client.execute_query_lazy("SELECT a") as lf:
            assert lf.to_df()["a"].tolist() == [1, 2, 3]

    def test_empty_stream_recovers_schema(self):
        empty = pa.table({"a": pa.array([], type=pa.int64()), "b": pa.array([], type=pa.string())})
        client = self._client([], empty)
        with client.execute_query_lazy("SELECT a, b WHERE false") as lf:
            assert lf.columns == ["a", "b"]
            assert lf.row_count() == 0


class TestClickhouseLazy:
    def _client(self, blocks, schema_table, probe_raises=False):
        from app.data_sources.clients.clickhouse_client import ClickhouseClient

        # __init__ opens a real HTTP connection; bypass it.
        client = ClickhouseClient.__new__(ClickhouseClient)

        class FakeStream:
            def __enter__(self):
                return iter(blocks)

            def __exit__(self, *exc):
                return False

        probe_sqls = []

        class FakeConn:
            def query_arrow_stream(self, sql):
                return FakeStream()

            def query_arrow(self, sql):
                probe_sqls.append(sql)
                if probe_raises:
                    raise RuntimeError("Syntax error: cannot be used as a subquery")
                return schema_table

        @contextmanager
        def fake_connect():
            yield FakeConn()

        client.connect = fake_connect
        return client, probe_sqls

    def test_streams_blocks(self):
        blocks = [pa.table({"v": [1, 2]}), pa.table({"v": [3]})]
        client, probes = self._client(blocks, pa.table({"v": pa.array([], type=pa.int64())}))
        with client.execute_query_lazy("SELECT v") as lf:
            assert lf.to_df()["v"].tolist() == [1, 2, 3]
        assert probes == []  # non-empty stream: no schema probe, no re-execution

    def test_empty_stream_recovers_schema_via_limit0_probe(self):
        client, probes = self._client([], pa.table({"v": pa.array([], type=pa.int64())}))
        with client.execute_query_lazy("SELECT v WHERE 0") as lf:
            assert lf.columns == ["v"]
            assert lf.row_count() == 0
        # the fallback must be a LIMIT 0 schema probe, not a full re-run
        assert len(probes) == 1
        assert "LIMIT 0" in probes[0]

    def test_unwrappable_statement_falls_back_to_empty_result(self):
        """SHOW TABLES etc. aren't valid as a subquery; a failing probe must
        yield an empty result (eager parity), not a query failure."""
        client, probes = self._client([], None, probe_raises=True)
        with client.execute_query_lazy("SHOW TABLES") as lf:
            assert lf.to_df().empty
            assert lf.columns == []
        assert len(probes) == 1


# =============================================================================
# Spark Connect: toLocalIterator rows, nested struct encoding
# =============================================================================

class _FakeSparkRow:
    def __init__(self, d):
        self._d = d

    def asDict(self, recursive=False):
        return dict(self._d)


class TestSparkConnectLazy:
    def test_rows_stream_with_nested_encoding(self):
        from app.data_sources.clients.spark_connect_client import SparkConnectClient

        client = SparkConnectClient(host="h")
        rows = [
            _FakeSparkRow({"id": 1, "payload": {"a": [1, 2]}}),
            _FakeSparkRow({"id": 2, "payload": {"b": "x"}}),  # struct keys drift
        ]

        class FakeSDF:
            def toLocalIterator(self):
                return iter(rows)

        class FakeSpark:
            def sql(self, q):
                return FakeSDF()

        @contextmanager
        def fake_connect():
            yield FakeSpark()

        client.connect = fake_connect
        client._run_guard = lambda spark, sql: None
        with client.execute_query_lazy("SELECT * FROM t") as lf:
            df = lf.to_df()
        assert df["id"].tolist() == [1, 2]
        # drifting struct keys survive because cells were JSON-encoded
        assert json.loads(df["payload"].iloc[0]) == {"a": [1, 2]}
        assert json.loads(df["payload"].iloc[1]) == {"b": "x"}


# =============================================================================
# Redshift: named (server-side) cursor streaming
# =============================================================================

class _FakeNamedCursor:
    """Mimics psycopg2 named-cursor semantics: description is populated only
    after the portal is declared by the first fetch."""

    def __init__(self, rows, columns):
        self._rows = rows
        self._columns = columns
        self._pos = 0
        self._fetched_once = False
        self.description = None
        self.itersize = None
        self.closed = False

    def execute(self, sql):
        pass

    def fetchmany(self, size):
        self._fetched_once = True
        self.description = [(c, None) for c in self._columns]
        batch = self._rows[self._pos:self._pos + size]
        self._pos += size
        return batch

    def close(self):
        self.closed = True


def _make_redshift(rows, columns):
    from app.data_sources.clients.aws_redshift_client import AwsRedshiftClient

    client = AwsRedshiftClient.__new__(AwsRedshiftClient)
    client._connected = False  # __del__ reads it; __new__ bypassed __init__
    cursor = _FakeNamedCursor(rows, columns)
    cursor_names = []

    class FakeConn:
        def cursor(self, name=None):
            cursor_names.append(name)
            return cursor

    @contextmanager
    def fake_connect():
        yield FakeConn()

    client.connect = fake_connect
    return client, cursor, cursor_names


class TestRedshiftLazy:
    def test_streams_via_named_cursor(self, monkeypatch):
        monkeypatch.setenv("BOW_LAZY_CHUNKSIZE", "2")
        rows = [(i, f"n{i}") for i in range(5)]
        client, cursor, names = _make_redshift(rows, ["id", "name"])
        with client.execute_query_lazy("SELECT id, name FROM t") as lf:
            df = lf.to_df()
        assert df["id"].tolist() == [0, 1, 2, 3, 4]
        # server-side cursor: a *named* cursor was requested and itersize set
        assert len(names) == 1 and names[0].startswith("bow_lazy_")
        assert cursor.itersize == 2
        assert cursor.closed

    def test_empty_result_preserves_schema(self):
        client, cursor, _ = _make_redshift([], ["id", "name"])
        with client.execute_query_lazy("SELECT id, name FROM t WHERE false") as lf:
            assert lf.columns == ["id", "name"]
            assert lf.row_count() == 0


# =============================================================================
# Concurrency + async
# =============================================================================

class TestConcurrency:
    def test_parallel_lazy_queries_share_spill_dir(self):
        n = 8
        client = TinyClient({
            f"q{i}": pd.DataFrame({"v": [i] * 100}) for i in range(n)
        })
        barrier = threading.Barrier(n)

        def run(i):
            barrier.wait()  # maximize overlap
            lf = client.execute_query_lazy(f"q{i}")
            return i, lf

        with ThreadPoolExecutor(max_workers=n) as pool:
            results = list(pool.map(run, range(n)))

        paths = set()
        try:
            for i, lf in results:
                assert lf.to_df()["v"].tolist() == [i] * 100
                paths.update(lf._source_paths)
        finally:
            for _, lf in results:
                lf.close()
        # every query spilled to its own file — no collisions in the shared dir
        assert len(paths) == n
        assert all(not p.exists() for p in paths)

    def test_aexecute_query_lazy(self):
        client = TinyClient({"q": pd.DataFrame({"v": [1, 2, 3]})})

        async def main():
            lfs = await asyncio.gather(*[client.aexecute_query_lazy("q") for _ in range(4)])
            try:
                return [lf.to_df()["v"].tolist() for lf in lfs]
            finally:
                for lf in lfs:
                    lf.close()

        assert asyncio.run(main()) == [[1, 2, 3]] * 4

    def test_aexecute_query_lazy_propagates_budget_error(self, monkeypatch):
        monkeypatch.setenv("BOW_LAZY_MAX_ROWS", "2")
        client = TinyClient({"q": pd.DataFrame({"v": [1, 2, 3]})})

        async def main():
            await client.aexecute_query_lazy("q")

        with pytest.raises(ResultTooLargeError):
            asyncio.run(main())


# =============================================================================
# Round-3 review fixes
# =============================================================================

class TestChunkStreamNestedCells:
    def test_heterogeneous_json_cells_survive_the_stream(self):
        """SQL JSON/JSONB columns arrive as dict cells; mixed shapes within one
        chunk used to abort strategy-based streams with ArrowInvalid."""
        from app.data_sources.clients.lazy_frame import consume_chunks_to_lazyframe

        chunks = [
            pd.DataFrame({"id": [1, 2], "payload": [{"a": 1}, "plain-string"]}),
            pd.DataFrame({"id": [3], "payload": [{"a": "x", "b": [1, 2]}]}),
        ]
        with consume_chunks_to_lazyframe(iter(chunks)) as lf:
            df = lf.to_df()
        assert len(df) == 3
        assert json.loads(df["payload"].iloc[0]) == {"a": 1}
        assert df["payload"].iloc[1] == "plain-string"
        assert json.loads(df["payload"].iloc[2]) == {"a": "x", "b": [1, 2]}


class TestSpillLifecycle:
    def test_finalizer_reclaims_spill_on_gc(self):
        import gc

        lf = lazy_from_dataframe(pd.DataFrame({"x": [1, 2]}))
        paths = list(lf._source_paths)
        assert all(p.exists() for p in paths)
        del lf
        gc.collect()
        assert all(not p.exists() for p in paths)

    def test_derived_frame_keeps_parent_alive(self):
        import gc

        # the owning frame is garbage as soon as .sql() returns; without the
        # parent pin its finalizer would close the shared DuckDB connection
        d = lazy_from_dataframe(pd.DataFrame({"x": [1, 2, 3]})).sql(
            "SELECT SUM(x) AS s FROM data"
        )
        gc.collect()
        assert int(d.to_df()["s"].iloc[0]) == 6

    def test_close_is_idempotent_with_finalizer(self):
        lf = lazy_from_dataframe(pd.DataFrame({"x": [1]}))
        lf.close()
        lf.close()  # finalizer already ran; must not raise
        assert all(not p.exists() for p in lf._source_paths)

    def test_schemaless_empty_frame_closes_lazily_created_connection(self):
        from app.data_sources.clients.lazy_frame import consume_row_dicts_to_lazyframe

        lf = consume_row_dicts_to_lazyframe(iter([]))  # zero-column spill, rel=None
        derived = lf.sql("SELECT COUNT(*) AS n FROM data")
        assert int(derived.to_df()["n"].iloc[0]) == 0
        con = lf._con
        assert con is not None  # sql() created it lazily
        lf.close()
        with pytest.raises(Exception):
            con.execute("SELECT 1")  # close() must also cover the lazy connection

    def test_stale_sweep_reruns_after_interval(self, tmp_path, monkeypatch):
        import os
        import app.data_sources.clients.lazy_frame as lf_mod

        monkeypatch.setenv("BOW_LAZY_DIR", str(tmp_path))
        stale_mtime = time.time() - 25 * 3600

        first = tmp_path / "lazy_first.parquet"
        first.write_bytes(b"x")
        os.utime(first, (stale_mtime, stale_mtime))
        lf_mod.StreamConfig()
        assert not first.exists()  # initial sweep

        second = tmp_path / "lazy_second.parquet"
        second.write_bytes(b"x")
        os.utime(second, (stale_mtime, stale_mtime))
        lf_mod.StreamConfig()
        assert second.exists()  # within the sweep interval: skipped

        # age the last-sweep stamp past the interval → swept again
        lf_mod._last_sweep[tmp_path] = (
            time.monotonic() - lf_mod._SWEEP_INTERVAL_SECONDS - 1
        )
        lf_mod.StreamConfig()
        assert not second.exists()


class TestDuckdbSingleScan:
    def test_query_executes_exactly_once(self):
        import duckdb
        from app.data_sources.clients.lazy_frame import lazy_query_via_duckdb

        executed = []

        class CountingCon:
            def __init__(self, con):
                self._con = con

            def execute(self, q):
                executed.append(q)
                return self._con.execute(q)

        @contextmanager
        def connect_cm():
            con = duckdb.connect()
            try:
                yield CountingCon(con)
            finally:
                con.close()

        with lazy_query_via_duckdb(connect_cm, "SELECT * FROM range(100)") as lf:
            assert lf.row_count() == 100
        assert len(executed) == 1  # no sampling pre-pass, no COUNT re-read
        assert executed[0].lstrip().startswith("SELECT")

    def test_row_budget_still_enforced(self, monkeypatch):
        import duckdb
        from app.data_sources.clients.lazy_frame import lazy_query_via_duckdb

        monkeypatch.setenv("BOW_LAZY_MAX_ROWS", "10")

        @contextmanager
        def connect_cm():
            con = duckdb.connect()
            try:
                yield con
            finally:
                con.close()

        with pytest.raises(ResultTooLargeError):
            lazy_query_via_duckdb(connect_cm, "SELECT * FROM range(100)")

    def test_byte_budget_enforced_mid_stream(self, monkeypatch):
        """max_bytes must abort during ingest, not after the full result is on
        disk — a wide 50M-row result must not be able to fill the disk first."""
        import duckdb
        from app.data_sources.clients.lazy_frame import lazy_query_via_duckdb

        monkeypatch.setenv("BOW_LAZY_MAX_BYTES", "10000")
        monkeypatch.setenv("BOW_LAZY_CHUNKSIZE", "1000")

        @contextmanager
        def connect_cm():
            con = duckdb.connect()
            try:
                yield con
            finally:
                con.close()

        with pytest.raises(ResultTooLargeError):
            lazy_query_via_duckdb(
                connect_cm,
                "SELECT range AS i, repeat('x', 100) AS pad FROM range(100000)",
            )

    def test_empty_result_keeps_schema(self):
        import duckdb
        from app.data_sources.clients.lazy_frame import lazy_query_via_duckdb

        @contextmanager
        def connect_cm():
            con = duckdb.connect()
            try:
                yield con
            finally:
                con.close()

        with lazy_query_via_duckdb(
            connect_cm, "SELECT range AS i FROM range(10) WHERE range < 0"
        ) as lf:
            assert lf.columns == ["i"]
            assert lf.row_count() == 0


class TestArrowSafeCell:
    def test_bson_scalars_become_strings(self):
        from bson.timestamp import Timestamp
        from bson.regex import Regex
        from app.data_sources.clients.lazy_frame import arrow_safe_cell

        assert isinstance(arrow_safe_cell(Timestamp(1690000000, 1)), str)
        assert isinstance(arrow_safe_cell(Regex("^a")), str)
        # containers JSON-encode even with exotic values inside
        encoded = arrow_safe_cell({"ts": Timestamp(1690000000, 1)})
        assert isinstance(encoded, str) and json.loads(encoded)
        # plain scalars pass through untouched
        assert arrow_safe_cell(5) == 5
        assert arrow_safe_cell("s") == "s"
        assert arrow_safe_cell(None) is None


class TestVerticaActivation:
    def _client(self):
        from app.data_sources.clients.vertica_client import VerticaClient

        client = VerticaClient.__new__(VerticaClient)
        client._connection_name = "vertica_conn_test"
        client._connected = True
        return client

    def test_private_api_failure_falls_back_to_activation(self, monkeypatch):
        """If verticapy relocates its private globals, connect() must degrade
        to unconditional re-activation, not fail every Vertica operation."""
        import app.data_sources.clients.vertica_client as vc

        def boom():
            raise AttributeError("internals moved")

        monkeypatch.setattr(
            "verticapy.connection.global_connection.get_global_connection", boom
        )
        calls = []
        monkeypatch.setattr(vc.vp, "connect", lambda name: calls.append(name))
        client = self._client()
        assert client.connect() == "vertica_conn_test"
        assert calls == ["vertica_conn_test"]

    def test_no_reactivation_when_already_active(self, monkeypatch):
        import app.data_sources.clients.vertica_client as vc

        class FakeActive:
            def closed(self):
                return False

        class FakeGlobal:
            def get_connection(self):
                return FakeActive()

            def get_dsn_section(self):
                return "vertica_conn_test"

        monkeypatch.setattr(
            "verticapy.connection.global_connection.get_global_connection",
            lambda: FakeGlobal(),
        )
        calls = []
        monkeypatch.setattr(vc.vp, "connect", lambda name: calls.append(name))
        client = self._client()
        assert client.connect() == "vertica_conn_test"
        assert calls == []  # already active: no kill-and-redial

    def test_lock_acquisition_times_out_instead_of_hanging_forever(self, monkeypatch):
        """A hung (abandoned) query holding the global lock must fail later
        queries with a clear error, not block them indefinitely."""
        import app.data_sources.clients.vertica_client as vc

        monkeypatch.setattr(vc, "_VP_LOCK_TIMEOUT_SECONDS", 0.2)
        release = threading.Event()

        def hog():
            with vc._VP_GLOBAL_LOCK:
                release.wait(5)

        t = threading.Thread(target=hog, daemon=True)
        t.start()
        time.sleep(0.1)  # let the hog acquire
        try:
            client = self._client()
            with pytest.raises(RuntimeError, match="Vertica connection lock"):
                client.connect()
        finally:
            release.set()
            t.join(2)


class TestWrapperMeteringRobustness:
    def test_metering_falls_back_to_disk_size(self):
        class HalfBrokenLazy:
            def row_count(self):
                raise RuntimeError("metadata unreadable")

            def uncompressed_byte_size(self):
                raise RuntimeError("metadata unreadable")

            def byte_size(self):
                return 42

        class Client(TinyClient):
            def execute_query_lazy(self, sql):
                return HalfBrokenLazy()

        wrapper, _, timings = _make_wrapper(Client())
        wrapper.execute_query_lazy("SELECT x")
        assert timings[0]["result_bytes"] == 42
        assert timings[0]["rows"] is None

    def test_lazy_quota_uses_uncompressed_size(self):
        # distinct values: dictionary/RLE encoding can't collapse the column,
        # so the uncompressed columnar size exceeds the compressed on-disk
        # size — and metering must charge the former
        client = TinyClient({"q": pd.DataFrame({"v": list(range(50_000))})})
        wrapper, _, timings = _make_wrapper(client)
        with wrapper.execute_query_lazy("q") as lf:
            assert timings[0]["result_bytes"] == lf.uncompressed_byte_size()
            assert timings[0]["result_bytes"] > lf.byte_size()

    def test_timed_out_lazy_result_is_reclaimed(self):
        from app.ai.code_execution.code_execution import QueryTimeoutError

        holder = {}

        class SlowLazyClient(TinyClient):
            def execute_query_lazy(self, sql):
                time.sleep(2)
                holder["lf"] = lazy_from_dataframe(pd.DataFrame({"x": [1]}))
                return holder["lf"]

        wrapper, _, _ = _make_wrapper(SlowLazyClient(), timeout=1)
        with pytest.raises(QueryTimeoutError):
            wrapper.execute_query_lazy("SELECT slow")
        # the abandoned daemon thread finishes ~1s later and must close the
        # spill instead of leaving it for the stale sweep
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            lf = holder.get("lf")
            if lf is not None and all(not p.exists() for p in lf._source_paths):
                break
            time.sleep(0.1)
        assert all(not p.exists() for p in holder["lf"]._source_paths)
