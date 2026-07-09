"""Unit tests for streaming query results to Parquet (writer half).

Exercises the real streamers against SQLite (DBAPI), SQLAlchemy, DuckDB
(native COPY) and pyarrow (Arrow consumer) — all dependencies, no live
service needed. Results are verified by reading the written Parquet back
directly; the out-of-core read handle lands in a follow-up.
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
