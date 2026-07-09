"""Unit tests for the out-of-core query path.

Exercises the real streamers/consumers against SQLite (DBAPI), SQLAlchemy,
DuckDB (native COPY) and pyarrow (Arrow consumer) — all dependencies, no live
service needed. Writer tests verify the written Parquet directly; reader
tests go through the LazyFrame handle.
"""
from __future__ import annotations

import os
import sqlite3
import time
from contextlib import contextmanager

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from app.data_sources.clients.lazy_frame import (
    ResultTooLargeError,
    StreamConfig,
    _consume_arrow_to_parquet,
    _consume_chunks_to_parquet,
    _swept_roots,
    consume_arrow_to_lazyframe,
    consume_chunks_to_lazyframe,
    consume_row_dicts_to_lazyframe,
    lazy_from_dataframe,
    lazy_query_via_dbapi_cursor,
    lazy_query_via_dbapi_readsql,
    lazy_query_via_duckdb,
    lazy_query_via_sqlalchemy,
    stream_dbapi_cursor_to_parquet,
    stream_dbapi_readsql_to_parquet,
    stream_duckdb_to_parquet,
    stream_sqlalchemy_to_parquet,
)


@pytest.fixture
def sqlite_db(tmp_path):
    path = tmp_path / "t.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE s(id INTEGER, region TEXT, amt INTEGER)")
    conn.executemany(
        "INSERT INTO s VALUES (?,?,?)",
        [(i, ["EU", "US", "APAC"][i % 3], i) for i in range(300)],
    )
    conn.commit()
    conn.close()

    @contextmanager
    def cm():
        c = sqlite3.connect(path)
        try:
            yield c
        finally:
            c.close()

    return cm


def test_dbapi_readsql_streaming(tmp_path, sqlite_db, monkeypatch):
    monkeypatch.setenv("BOW_LAZY_CHUNKSIZE", "50")
    out = tmp_path / "out.parquet"
    stream_dbapi_readsql_to_parquet(sqlite_db, "SELECT * FROM s", out, StreamConfig())
    df = pq.read_table(out).to_pandas()
    assert len(df) == 300
    assert int(df.amt.sum()) == sum(range(300))


def test_dbapi_cursor_streaming_preserves_schema(tmp_path, sqlite_db, monkeypatch):
    monkeypatch.setenv("BOW_LAZY_CHUNKSIZE", "50")
    out = tmp_path / "out.parquet"
    stream_dbapi_cursor_to_parquet(sqlite_db, "SELECT id, region, amt FROM s", out, StreamConfig())
    table = pq.read_table(out)
    assert table.num_rows == 300
    assert table.schema.names == ["id", "region", "amt"]


def test_sqlalchemy_streaming(tmp_path, monkeypatch):
    sqlalchemy = pytest.importorskip("sqlalchemy")
    monkeypatch.setenv("BOW_LAZY_CHUNKSIZE", "50")
    dbf = tmp_path / "z.db"
    conn = sqlite3.connect(dbf)
    conn.execute("CREATE TABLE s(id INTEGER, region TEXT)")
    conn.executemany("INSERT INTO s VALUES (?,?)", [(i, "EU") for i in range(120)])
    conn.commit()
    conn.close()
    engine = sqlalchemy.create_engine(f"sqlite:///{dbf}")

    @contextmanager
    def cm():
        with engine.connect() as c:
            yield c

    out = tmp_path / "out.parquet"
    try:
        stream_sqlalchemy_to_parquet(cm, "SELECT id, region FROM s", out, StreamConfig())
        df = pq.read_table(out).to_pandas()
        assert len(df) == 120
        assert list(df.columns) == ["id", "region"]
    finally:
        engine.dispose()


def test_native_duckdb_copy_zero_load(tmp_path):
    duckdb = pytest.importorskip("duckdb")
    dbf = tmp_path / "x.duckdb"
    con = duckdb.connect(str(dbf))
    con.execute("CREATE TABLE s AS SELECT * FROM range(300) t(id)")
    con.close()

    @contextmanager
    def cm():
        c = duckdb.connect(str(dbf), read_only=True)
        try:
            yield c
        finally:
            c.close()

    out = tmp_path / "out.parquet"
    stream_duckdb_to_parquet(cm, "SELECT id FROM s WHERE id < 100", out, StreamConfig())
    df = pq.read_table(out).to_pandas()
    assert len(df) == 100
    assert int(df.id.sum()) == sum(range(100))


def test_arrow_consumer_mixed_batch_and_table(tmp_path):
    batches = [
        pa.record_batch({"a": pa.array([1, 2, 3]), "b": pa.array(["x", "y", "z"])}),
        pa.table({"a": pa.array([4, 5]), "b": pa.array(["p", "q"])}),
    ]
    out = tmp_path / "out.parquet"
    _consume_arrow_to_parquet(iter(batches), out, StreamConfig())
    df = pq.read_table(out).to_pandas()
    assert len(df) == 5
    assert int(df.a.sum()) == 15


def test_early_abort_on_row_cap(tmp_path, sqlite_db, monkeypatch):
    monkeypatch.setenv("BOW_LAZY_CHUNKSIZE", "50")
    monkeypatch.setenv("BOW_LAZY_MAX_ROWS", "100")
    out = tmp_path / "out.parquet"
    with pytest.raises(ResultTooLargeError) as exc:
        stream_dbapi_cursor_to_parquet(sqlite_db, "SELECT * FROM s", out, StreamConfig())
    assert exc.value.status_code == 413
    # partial file must not be left behind
    assert not out.exists()


def test_duckdb_copy_respects_row_cap(tmp_path, monkeypatch):
    duckdb = pytest.importorskip("duckdb")
    monkeypatch.setenv("BOW_LAZY_MAX_ROWS", "100")

    @contextmanager
    def cm():
        c = duckdb.connect(":memory:")
        try:
            yield c
        finally:
            c.close()

    out = tmp_path / "out.parquet"
    with pytest.raises(ResultTooLargeError):
        stream_duckdb_to_parquet(cm, "SELECT * FROM range(300)", out, StreamConfig())
    assert not out.exists()


def test_schema_drift_all_null_later_chunk(tmp_path):
    # A nullable numeric column that is all-NULL in one chunk infers a
    # different Arrow dtype (null) than the writer's (int64); the write must
    # cast, not abort the stream.
    chunks = [
        pd.DataFrame({"id": [1, 2], "v": [10, 20]}),
        pd.DataFrame({"id": [3, 4], "v": [None, None]}),
        pd.DataFrame({"id": [5], "v": [50]}),
    ]
    out = tmp_path / "out.parquet"
    _consume_chunks_to_parquet(iter(chunks), out, StreamConfig())
    df = pq.read_table(out).to_pandas()
    assert len(df) == 5
    assert int(df.v.sum()) == 80


def test_schema_drift_all_null_first_chunk(tmp_path):
    # The anomalous chunk can also come FIRST: an all-NULL column infers
    # pa.null() and must be widened (to float64) before it locks the writer
    # schema, or every later non-null chunk would fail to write.
    chunks = [
        pd.DataFrame({"id": [1, 2], "v": [None, None]}),
        pd.DataFrame({"id": [3, 4], "v": [30, 40]}),
    ]
    out = tmp_path / "out.parquet"
    _consume_chunks_to_parquet(iter(chunks), out, StreamConfig())
    df = pq.read_table(out).to_pandas()
    assert len(df) == 4
    assert list(df.columns) == ["id", "v"]
    assert int(df.v.sum()) == 70


def test_zero_row_sqlalchemy_preserves_columns(tmp_path):
    sqlalchemy = pytest.importorskip("sqlalchemy")
    dbf = tmp_path / "z.db"
    conn = sqlite3.connect(dbf)
    conn.execute("CREATE TABLE s(id INTEGER, region TEXT)")
    conn.commit()
    conn.close()
    engine = sqlalchemy.create_engine(f"sqlite:///{dbf}")

    @contextmanager
    def cm():
        with engine.connect() as c:
            yield c

    out = tmp_path / "out.parquet"
    try:
        stream_sqlalchemy_to_parquet(cm, "SELECT id, region FROM s WHERE id < 0", out, StreamConfig())
        table = pq.read_table(out)
        assert table.num_rows == 0
        assert table.schema.names == ["id", "region"]
    finally:
        engine.dispose()


def test_zero_row_dbapi_readsql_preserves_columns(tmp_path, sqlite_db):
    out = tmp_path / "out.parquet"
    stream_dbapi_readsql_to_parquet(sqlite_db, "SELECT id, region FROM s WHERE id < 0", out, StreamConfig())
    table = pq.read_table(out)
    assert table.num_rows == 0
    assert table.schema.names == ["id", "region"]


def test_zero_row_chunk_consumer_with_columns_preserves_schema(tmp_path):
    out = tmp_path / "out.parquet"
    _consume_chunks_to_parquet(iter([]), out, StreamConfig(), columns=["id", "name"])
    table = pq.read_table(out)
    assert table.num_rows == 0
    assert table.schema.names == ["id", "name"]


def test_chunk_consumer_closes_generator_on_abort(tmp_path, monkeypatch):
    # On abort the passed-in generator must be closed explicitly (releasing its
    # connection), not left suspended for a later GC pass.
    monkeypatch.setenv("BOW_LAZY_MAX_ROWS", "10")
    closed = []

    def gen():
        try:
            while True:
                yield pd.DataFrame({"v": range(50)})
        finally:
            closed.append(True)

    g = gen()  # hold a reference so refcount GC can't mask a missing close()
    with pytest.raises(ResultTooLargeError):
        _consume_chunks_to_parquet(g, tmp_path / "out.parquet", StreamConfig())
    assert closed == [True]


def test_stale_lazy_files_swept_on_config_init(tmp_path, monkeypatch):
    monkeypatch.setenv("BOW_LAZY_DIR", str(tmp_path))
    old = tmp_path / "lazy_orphan.parquet"
    old.write_bytes(b"x")
    stale = time.time() - 25 * 3600
    os.utime(old, (stale, stale))
    fresh = tmp_path / "lazy_fresh.parquet"
    fresh.write_bytes(b"x")
    other = tmp_path / "keep.txt"  # non-lazy files must never be touched
    other.write_bytes(b"x")
    os.utime(other, (stale, stale))
    _swept_roots.discard(tmp_path)  # sweep runs once per root per process
    StreamConfig()
    assert not old.exists()
    assert fresh.exists()
    assert other.exists()


# --- reader half: the LazyFrame handle over the written Parquet ------------


def test_lazy_from_dataframe_out_of_core(tmp_path, monkeypatch):
    monkeypatch.setenv("BOW_LAZY_DIR", str(tmp_path))
    df = pd.DataFrame({"region": ["EU", "US", "EU", "APAC"], "amount": [10, 20, 30, 40]})
    h = lazy_from_dataframe(df)
    try:
        assert h.row_count() == 4
        out = h.sql("SELECT region, SUM(amount) t FROM data GROUP BY region ORDER BY region").to_df()
        assert dict(zip(out.region, out.t)) == {"APAC": 40, "EU": 40, "US": 20}
        assert h.byte_size() > 0
    finally:
        h.close()


def test_lazy_query_via_dbapi_readsql(tmp_path, sqlite_db, monkeypatch):
    monkeypatch.setenv("BOW_LAZY_DIR", str(tmp_path))
    monkeypatch.setenv("BOW_LAZY_CHUNKSIZE", "50")
    h = lazy_query_via_dbapi_readsql(sqlite_db, "SELECT * FROM s")
    try:
        assert h.row_count() == 300
        total = h.sql("SELECT SUM(amt) t FROM data").to_df().iloc[0, 0]
        assert int(total) == sum(range(300))
    finally:
        h.close()


def test_lazy_query_via_dbapi_cursor_preserves_schema(tmp_path, sqlite_db, monkeypatch):
    monkeypatch.setenv("BOW_LAZY_DIR", str(tmp_path))
    monkeypatch.setenv("BOW_LAZY_CHUNKSIZE", "50")
    h = lazy_query_via_dbapi_cursor(sqlite_db, "SELECT id, region, amt FROM s")
    try:
        assert h.row_count() == 300
        assert h.columns == ["id", "region", "amt"]
    finally:
        h.close()


def test_lazy_query_via_duckdb(tmp_path, monkeypatch):
    monkeypatch.setenv("BOW_LAZY_DIR", str(tmp_path))
    duckdb = pytest.importorskip("duckdb")
    dbf = tmp_path / "x.duckdb"
    con = duckdb.connect(str(dbf))
    con.execute("CREATE TABLE s AS SELECT * FROM range(300) t(id)")
    con.close()

    @contextmanager
    def cm():
        c = duckdb.connect(str(dbf), read_only=True)
        try:
            yield c
        finally:
            c.close()

    h = lazy_query_via_duckdb(cm, "SELECT id FROM s WHERE id < 100")
    try:
        assert h.row_count() == 100
        assert int(h.sql("SELECT SUM(id) s FROM data").to_df().iloc[0, 0]) == sum(range(100))
    finally:
        h.close()


def test_consume_arrow_to_lazyframe(tmp_path, monkeypatch):
    monkeypatch.setenv("BOW_LAZY_DIR", str(tmp_path))
    batches = [
        pa.record_batch({"a": pa.array([1, 2, 3]), "b": pa.array(["x", "y", "z"])}),
        pa.table({"a": pa.array([4, 5]), "b": pa.array(["p", "q"])}),
    ]
    h = consume_arrow_to_lazyframe(iter(batches))
    try:
        assert h.row_count() == 5
        assert int(h.sql("SELECT SUM(a) s FROM data").to_df().iloc[0, 0]) == 15
    finally:
        h.close()


def test_row_dicts_consumer(tmp_path, monkeypatch):
    monkeypatch.setenv("BOW_LAZY_DIR", str(tmp_path))
    rows = [{"id": i, "meta": '{"k": %d}' % i} for i in range(200)]
    h = consume_row_dicts_to_lazyframe(iter(rows))
    try:
        assert h.row_count() == 200
        assert h.columns == ["id", "meta"]
    finally:
        h.close()


def test_chunk_consumer(tmp_path, monkeypatch):
    monkeypatch.setenv("BOW_LAZY_DIR", str(tmp_path))
    chunks = [pd.DataFrame({"v": range(64)}), pd.DataFrame({"v": range(64, 100)})]
    h = consume_chunks_to_lazyframe(iter(chunks))
    try:
        assert h.row_count() == 100
    finally:
        h.close()


def test_lazy_early_abort_leaves_no_file(tmp_path, sqlite_db, monkeypatch):
    monkeypatch.setenv("BOW_LAZY_DIR", str(tmp_path))
    monkeypatch.setenv("BOW_LAZY_CHUNKSIZE", "50")
    monkeypatch.setenv("BOW_LAZY_MAX_ROWS", "100")
    with pytest.raises(ResultTooLargeError) as exc:
        lazy_query_via_dbapi_cursor(sqlite_db, "SELECT * FROM s")
    assert exc.value.status_code == 413
    # partial file must not be left behind
    assert list(tmp_path.glob("*.parquet")) == []


def test_close_removes_owned_temp_file(tmp_path, monkeypatch):
    monkeypatch.setenv("BOW_LAZY_DIR", str(tmp_path))
    h = lazy_from_dataframe(pd.DataFrame({"a": [1, 2, 3]}))
    [src] = h._source_paths
    assert src.exists()
    h.close()
    assert not src.exists()


def test_zero_row_lazy_sqlalchemy_sql_projection(tmp_path, monkeypatch):
    sqlalchemy = pytest.importorskip("sqlalchemy")
    monkeypatch.setenv("BOW_LAZY_DIR", str(tmp_path))
    dbf = tmp_path / "z.db"
    conn = sqlite3.connect(dbf)
    conn.execute("CREATE TABLE s(id INTEGER, region TEXT)")
    conn.commit()
    conn.close()
    engine = sqlalchemy.create_engine(f"sqlite:///{dbf}")

    @contextmanager
    def cm():
        with engine.connect() as c:
            yield c

    h = lazy_query_via_sqlalchemy(cm, "SELECT id, region FROM s WHERE id < 0")
    try:
        assert h.row_count() == 0
        assert h.columns == ["id", "region"]
        out = h.sql("SELECT region FROM data").to_df()
        assert list(out.columns) == ["region"] and len(out) == 0
    finally:
        h.close()
        engine.dispose()


def test_zero_row_dicts_with_columns_preserves_schema(tmp_path, monkeypatch):
    monkeypatch.setenv("BOW_LAZY_DIR", str(tmp_path))
    h = consume_row_dicts_to_lazyframe(iter([]), columns=["id", "name"])
    try:
        assert h.row_count() == 0
        assert h.columns == ["id", "name"]
    finally:
        h.close()


def test_derived_frame_close_does_not_close_parent(tmp_path, monkeypatch):
    # .sql()/.limit() return handles that share the parent's connection and
    # file (owns_source=False); closing them must not tear down the parent.
    monkeypatch.setenv("BOW_LAZY_DIR", str(tmp_path))
    h = lazy_from_dataframe(pd.DataFrame({"a": [1, 2, 3]}))
    try:
        with h.sql("SELECT a FROM data WHERE a > 1") as derived:
            assert derived.row_count() == 2
        # parent still usable after the derived handle was closed
        assert h.row_count() == 3
        assert all(p.exists() for p in h._source_paths)
    finally:
        h.close()


# --- connector wiring ---------------------------------------------------


def test_mongodb_decimal128_conversion():
    pytest.importorskip("pymongo")
    bson = pytest.importorskip("bson")
    from app.data_sources.clients.mongodb_client import MongodbClient

    client = MongodbClient(host="localhost", database="x")

    def make_doc():
        return {
            "price": bson.Decimal128("19.99"),
            "nested": {"amt": bson.Decimal128("0.001")},
            "arr": [bson.Decimal128("2.5"), {"inner": bson.Decimal128("3.5")}],
        }

    # Lazy path opts into the lossy float coercion (pyarrow can't spill Decimal128)
    doc = make_doc()
    client._convert_bson_types(doc, coerce_decimal128=True)
    assert isinstance(doc["price"], float) and doc["price"] == pytest.approx(19.99)
    assert doc["nested"]["amt"] == pytest.approx(0.001)
    assert doc["arr"][0] == pytest.approx(2.5)
    assert doc["arr"][1]["inner"] == pytest.approx(3.5)
    # the converted doc must survive the columnar spill (raw Decimal128 raises)
    pa.Table.from_pandas(pd.DataFrame([{"price": doc["price"]}]))

    # Eager path keeps exact Decimal128 values — its behavior must not change
    doc = make_doc()
    client._convert_bson_types(doc)
    assert isinstance(doc["price"], bson.Decimal128)
    assert isinstance(doc["nested"]["amt"], bson.Decimal128)
    assert isinstance(doc["arr"][0], bson.Decimal128)


# --- review fixes: empty schemaless results, column drift, budgets ---------


def test_empty_row_dicts_no_columns_returns_empty_lazyframe(tmp_path, monkeypatch):
    # A zero-row result from a schemaless source writes a zero-column Parquet,
    # which DuckDB cannot read; the handle must still behave as an empty frame.
    monkeypatch.setenv("BOW_LAZY_DIR", str(tmp_path))
    lf = consume_row_dicts_to_lazyframe(iter([]))
    try:
        assert lf.row_count() == 0
        assert lf.columns == []
        assert lf.to_df().empty
        assert lf.to_arrow().num_rows == 0
        assert lf.head().empty
        assert lf.sql("SELECT * FROM data").to_df().empty
        assert lf.limit(5).to_df().empty
    finally:
        lf.close()
    assert not any(tmp_path.glob("lazy_*.parquet"))  # spill cleaned up


def test_empty_arrow_stream_returns_empty_lazyframe(tmp_path, monkeypatch):
    monkeypatch.setenv("BOW_LAZY_DIR", str(tmp_path))
    lf = consume_arrow_to_lazyframe(iter([]))
    try:
        assert lf.row_count() == 0
        assert lf.to_df().empty
    finally:
        lf.close()
    assert not any(tmp_path.glob("lazy_*.parquet"))


def test_column_set_drift_rolls_part_files(tmp_path, monkeypatch):
    # Schemaless sources (Mongo) can grow columns between chunks. The writer
    # rolls a new part file and the reader unions parts by name, NULL-filling
    # missing columns — matching eager pandas behavior instead of aborting.
    monkeypatch.setenv("BOW_LAZY_DIR", str(tmp_path))
    monkeypatch.setenv("BOW_LAZY_CHUNKSIZE", "2")
    rows = [
        {"a": 1, "b": "x"},
        {"a": 2, "b": "y"},
        {"a": 3, "b": "z", "c": 30},  # new column appears mid-stream
        {"a": 4, "b": "w", "c": 40},
    ]
    lf = consume_row_dicts_to_lazyframe(iter(rows))
    try:
        df = lf.sql("SELECT * FROM data ORDER BY a").to_df()
        assert list(df["a"]) == [1, 2, 3, 4]
        assert set(df.columns) == {"a", "b", "c"}
        assert pd.isna(df["c"].iloc[0]) and df["c"].iloc[3] == 40
        assert len(lf._source_paths) == 2  # one part per column set
    finally:
        lf.close()
    assert not any(tmp_path.glob("lazy_*.parquet"))  # all parts removed


def test_column_order_drift_is_reconciled(tmp_path):
    # Same column set, different order across chunks: reorder, don't abort.
    chunks = [
        pd.DataFrame({"a": [1], "b": ["x"]}),
        pd.DataFrame({"b": ["y"], "a": [2]}),
    ]
    out = tmp_path / "out.parquet"
    paths = _consume_chunks_to_parquet(iter(chunks), out, StreamConfig())
    assert paths == [out]
    df = pq.read_table(out).to_pandas()
    assert list(df["a"]) == [1, 2]


def test_lazy_from_dataframe_enforces_row_budget(tmp_path, monkeypatch):
    monkeypatch.setenv("BOW_LAZY_DIR", str(tmp_path))
    monkeypatch.setenv("BOW_LAZY_MAX_ROWS", "2")
    with pytest.raises(ResultTooLargeError) as exc:
        lazy_from_dataframe(pd.DataFrame({"a": [1, 2, 3]}))
    assert exc.value.status_code == 413
    assert not any(tmp_path.glob("lazy_*.parquet"))  # nothing spilled


def test_lazy_from_dataframe_jsonifies_nested_cells(tmp_path, monkeypatch):
    # The generic materialize-then-spill fallback must not die on dict/list
    # cells (pyarrow ArrowInvalid) — e.g. ADX dynamic columns.
    monkeypatch.setenv("BOW_LAZY_DIR", str(tmp_path))
    df = pd.DataFrame({"id": [1, 2], "props": [{"k": "v"}, ["a", "b"]]})
    lf = lazy_from_dataframe(df)
    try:
        out = lf.sql("SELECT * FROM data ORDER BY id").to_df()
        assert out["props"].tolist() == ['{"k": "v"}', '["a", "b"]']
        assert df["props"].iloc[0] == {"k": "v"}  # caller's frame untouched
    finally:
        lf.close()


def test_duckdb_copy_byte_budget_bounds_write(tmp_path, monkeypatch):
    # Wide rows: the sample-derived row cap must fire on max_bytes, not write
    # up to max_rows worth of data to disk first.
    duckdb = pytest.importorskip("duckdb")
    monkeypatch.setenv("BOW_LAZY_MAX_BYTES", "10000")

    @contextmanager
    def cm():
        c = duckdb.connect(":memory:")
        try:
            yield c
        finally:
            c.close()

    out = tmp_path / "out.parquet"
    with pytest.raises(ResultTooLargeError):
        stream_duckdb_to_parquet(
            cm, "SELECT i, repeat('x', 1000) AS pad FROM range(100000) t(i)",
            out, StreamConfig(),
        )
    assert not out.exists()


# --- review round 2: dup columns, SQL tails, type drift, empty-mode SQL ----


def test_duplicate_column_names_are_deduped(tmp_path, monkeypatch):
    # `SELECT *` over a join can repeat a column name; pyarrow refuses
    # duplicates, so the writer renames them (id, id_1) like DuckDB would.
    monkeypatch.setenv("BOW_LAZY_DIR", str(tmp_path))
    chunk = pd.DataFrame([[1, "a", 2], [3, "b", 4]], columns=["id", "name", "id"])
    out = tmp_path / "out.parquet"
    _consume_chunks_to_parquet(iter([chunk]), out, StreamConfig())
    table = pq.read_table(out)
    assert table.schema.names == ["id", "name", "id_1"]
    assert table.num_rows == 2

    df = pd.DataFrame([[1, 2]], columns=["x", "x"])
    lf = lazy_from_dataframe(df)
    try:
        assert lf.columns == ["x", "x_1"]
    finally:
        lf.close()


def test_duckdb_wrapper_survives_trailing_comments(tmp_path):
    duckdb = pytest.importorskip("duckdb")

    @contextmanager
    def cm():
        c = duckdb.connect(":memory:")
        try:
            yield c
        finally:
            c.close()

    for sql in [
        "SELECT 1 AS a -- trailing note",
        "SELECT 1 AS a;\n-- comment after semicolon",
        "SELECT 1 AS a\n-- whole-line comment\n;",
    ]:
        out = tmp_path / "out.parquet"
        stream_duckdb_to_parquet(cm, sql, out, StreamConfig())
        df = pq.read_table(out).to_pandas()
        assert list(df["a"]) == [1]
        out.unlink()


def test_all_null_leading_string_column_rolls_part(tmp_path, monkeypatch):
    # A sparse text column all-NULL in the first chunk locks the file schema
    # to float64; when real strings arrive the writer must roll a part (read
    # side promotes double+varchar to varchar), not abort the stream.
    monkeypatch.setenv("BOW_LAZY_DIR", str(tmp_path))
    chunks = [
        pd.DataFrame({"a": [1, 2], "s": [None, None]}),
        pd.DataFrame({"a": [3], "s": ["hello"]}),
    ]
    lf = consume_chunks_to_lazyframe(iter(chunks))
    try:
        df = lf.sql("SELECT * FROM data ORDER BY a").to_df()
        assert len(df) == 3
        assert df["s"].iloc[2] == "hello"
        assert pd.isna(df["s"].iloc[0])
    finally:
        lf.close()
    assert not any(tmp_path.glob("lazy_*.parquet"))


def test_empty_mode_sql_keeps_aggregate_semantics(tmp_path, monkeypatch):
    # COUNT(*) over an empty unknown-schema result must return one row with 0,
    # not a zero-row frame (SQL semantics; downstream does df["n"].iloc[0]).
    monkeypatch.setenv("BOW_LAZY_DIR", str(tmp_path))
    lf = consume_row_dicts_to_lazyframe(iter([]))
    try:
        agg = lf.sql("SELECT COUNT(*) AS n FROM data").to_df()
        assert len(agg) == 1
        assert int(agg["n"].iloc[0]) == 0
    finally:
        lf.close()


def test_zero_column_chunk_mid_stream_is_skipped(tmp_path, monkeypatch):
    # Mongo docs projected to {} produce (n, 0) chunks; they must not roll an
    # unreadable zero-column part file.
    monkeypatch.setenv("BOW_LAZY_DIR", str(tmp_path))
    monkeypatch.setenv("BOW_LAZY_CHUNKSIZE", "2")
    rows = [{"a": 1}, {"a": 2}, {}, {}, {"a": 5}]
    lf = consume_row_dicts_to_lazyframe(iter(rows))
    try:
        df = lf.sql("SELECT * FROM data ORDER BY a").to_df()
        assert list(df["a"]) == [1, 2, 5]
    finally:
        lf.close()
    assert not any(tmp_path.glob("lazy_*.parquet"))


def test_druid_basic_token_cursor_fetchmany():
    from app.data_sources.clients.druid_client import _BasicTokenCursor

    cur = _BasicTokenCursor("http://x", "tok", True)
    cur._rows = [(1,), (2,), (3,)]
    cur._pos = 0
    assert cur.fetchmany(2) == [(1,), (2,)]
    assert cur.fetchmany(2) == [(3,)]
    assert cur.fetchmany(2) == []
    assert cur.fetchall() == [(1,), (2,), (3,)]  # fetchall stays non-destructive
