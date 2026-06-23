"""Unit tests for the out-of-core LazyFrame query path.

Exercises the real streamers/consumers against SQLite (DBAPI), DuckDB (native
COPY) and pyarrow (Arrow consumer) — all dependencies, no live service needed.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager

import pandas as pd
import pyarrow as pa
import pytest

from app.data_sources.clients.lazy_frame import (
    LazyFrame,
    ResultTooLargeError,
    StreamConfig,
    consume_arrow_to_lazyframe,
    consume_chunks_to_lazyframe,
    consume_row_dicts_to_lazyframe,
    lazy_from_dataframe,
    lazy_query_via_dbapi_cursor,
    lazy_query_via_dbapi_readsql,
    lazy_query_via_duckdb,
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


def test_dbapi_readsql_streaming(tmp_path, sqlite_db, monkeypatch):
    monkeypatch.setenv("BOW_LAZY_DIR", str(tmp_path))
    monkeypatch.setenv("BOW_LAZY_CHUNKSIZE", "50")
    h = lazy_query_via_dbapi_readsql(sqlite_db, "SELECT * FROM s")
    try:
        assert h.row_count() == 300
        total = h.sql("SELECT SUM(amt) t FROM data").to_df().iloc[0, 0]
        assert int(total) == sum(range(300))
    finally:
        h.close()


def test_dbapi_cursor_streaming_preserves_schema(tmp_path, sqlite_db, monkeypatch):
    monkeypatch.setenv("BOW_LAZY_DIR", str(tmp_path))
    monkeypatch.setenv("BOW_LAZY_CHUNKSIZE", "50")
    h = lazy_query_via_dbapi_cursor(sqlite_db, "SELECT id, region, amt FROM s")
    try:
        assert h.row_count() == 300
        assert h.columns == ["id", "region", "amt"]
    finally:
        h.close()


def test_native_duckdb_copy_zero_load(tmp_path, monkeypatch):
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


def test_arrow_consumer_mixed_batch_and_table(tmp_path, monkeypatch):
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


def test_early_abort_on_row_cap(tmp_path, sqlite_db, monkeypatch):
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
    src = h._source_path
    assert src.exists()
    h.close()
    assert not src.exists()
