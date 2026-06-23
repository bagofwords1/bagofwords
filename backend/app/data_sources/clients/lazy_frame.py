"""Out-of-core query results (v2, opt-in) — `execute_query_lazy`.

The default `execute_query` path is unchanged: it runs the query and returns a
full `pandas.DataFrame` held in memory. This module adds a *separate*, opt-in
path that never materializes the whole result:

  1. Stream the source result in chunks (server-side cursor) straight to a
     Parquet file on disk. Peak memory during ingest is one chunk, not the whole
     result. An early byte/row cap aborts oversized scans *before* they OOM.
  2. Return a `LazyFrame` — a thin handle over a DuckDB relation backed by that
     Parquet file. Filtering / aggregation run out-of-core inside DuckDB (which
     spills to disk); only the final, presumably-small result enters RAM, when
     the caller explicitly calls `.to_df()`.

Nothing here changes existing behavior. A client opts in by overriding
`execute_query_lazy` (see TrinoClient for the reference wiring); callers opt in
by calling it instead of `execute_query`.
"""

from __future__ import annotations

import logging
import os
import tempfile
import uuid
from pathlib import Path
from typing import Callable, ContextManager, Optional

import pandas as pd

from app.errors.app_error import AppError
from app.errors.codes import ErrorCode

logger = logging.getLogger(__name__)


class ResultTooLargeError(AppError):
    """Raised when a streamed result exceeds the configured row/byte budget.

    Mirrors QueryTimeoutError: the surrounding handler surfaces it to the
    planner so the agent can narrow the query (LIMIT / filters / aggregation).
    """

    def __init__(self, *, rows: int, byte_estimate: int, limit_desc: str) -> None:
        message = (
            f"Query result exceeded the streaming budget ({limit_desc}) after "
            f"{rows} rows (~{byte_estimate} bytes). Narrow the query with a "
            "LIMIT, tighter filters, or aggregation."
        )
        super().__init__(
            ErrorCode.QUERY_RESULT_TOO_LARGE,
            message,
            status_code=413,
            params={"rows": int(rows), "byte_estimate": int(byte_estimate)},
        )
        self.rows = int(rows)
        self.byte_estimate = int(byte_estimate)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ[name])
    except (KeyError, ValueError):
        return default


class StreamConfig:
    """Caps for streaming ingest. Generous defaults; tune via env."""

    def __init__(self) -> None:
        self.chunksize = _env_int("BOW_LAZY_CHUNKSIZE", 50_000)
        self.max_rows = _env_int("BOW_LAZY_MAX_ROWS", 50_000_000)
        self.max_bytes = _env_int("BOW_LAZY_MAX_BYTES", 8 * 1024 * 1024 * 1024)
        root = os.environ.get("BOW_LAZY_DIR")
        self.root = Path(root) if root else Path(tempfile.gettempdir()) / "bow_lazy"

    def limit_desc(self) -> str:
        return f"max_rows={self.max_rows}, max_bytes={self.max_bytes}"


class LazyFrame:
    """An out-of-core handle over a Parquet file via a DuckDB relation.

    Data stays on disk; DuckDB executes filters/aggregations and spills as
    needed. Only `.to_df()` / `.to_arrow()` pull results into memory — so keep
    those for the *reduced* result, not the raw scan.
    """

    def __init__(self, con, relation, source_path: Path, owns_source: bool = True):
        self._con = con
        self._rel = relation
        self._source_path = source_path
        self._owns_source = owns_source

    @classmethod
    def from_parquet(cls, path: Path, owns_source: bool = True) -> "LazyFrame":
        import duckdb

        con = duckdb.connect(database=":memory:")  # DuckDB spills to disk on its own
        rel = con.read_parquet(str(path))
        return cls(con, rel, path, owns_source=owns_source)

    # -- lazy transforms (return new handles, no materialization) -----------

    def sql(self, sql_query: str, table_name: str = "data") -> "LazyFrame":
        """Run SQL against this result, referring to it as `table_name`.

        e.g. lf.sql("SELECT region, SUM(amount) FROM data GROUP BY region")
        """
        new_rel = self._rel.query(table_name, sql_query)
        return LazyFrame(self._con, new_rel, self._source_path, owns_source=False)

    def limit(self, n: int) -> "LazyFrame":
        return LazyFrame(self._con, self._rel.limit(n), self._source_path, owns_source=False)

    # -- materialization (explicit; this is where memory is spent) ----------

    def to_df(self) -> pd.DataFrame:
        return self._rel.df()

    def to_arrow(self):
        return self._rel.arrow()

    def row_count(self) -> int:
        return int(self._rel.aggregate("count(*) AS n").fetchone()[0])

    def head(self, n: int = 10) -> pd.DataFrame:
        return self._rel.limit(n).df()

    @property
    def columns(self) -> list:
        return list(self._rel.columns)

    def byte_size(self) -> int:
        """On-disk size of the backing Parquet, used for usage accounting without
        materializing the frame. Returns 0 if the file is unavailable."""
        try:
            return int(Path(self._source_path).stat().st_size)
        except Exception:
            return 0

    # -- lifecycle ----------------------------------------------------------

    def close(self) -> None:
        try:
            self._con.close()
        except Exception:
            logger.debug("LazyFrame: failed to close duckdb connection", exc_info=True)
        if self._owns_source:
            try:
                Path(self._source_path).unlink(missing_ok=True)
            except Exception:
                logger.debug("LazyFrame: failed to unlink %s", self._source_path, exc_info=True)

    def __enter__(self) -> "LazyFrame":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def stream_sqlalchemy_to_parquet(
    connect_cm: Callable[[], ContextManager],
    sql: str,
    path: Path,
    config: StreamConfig,
) -> Path:
    """Stream a SQLAlchemy query result to a Parquet file, chunk by chunk.

    `connect_cm` is a zero-arg callable returning a context manager that yields a
    SQLAlchemy connection (e.g. a client's `connect` method). Aborts with
    ResultTooLargeError once the row/byte budget is exceeded, deleting the
    partial file.
    """
    import pyarrow as pa
    import pyarrow.parquet as pq
    from sqlalchemy import text

    path.parent.mkdir(parents=True, exist_ok=True)
    writer: Optional["pq.ParquetWriter"] = None
    rows = 0
    byte_estimate = 0
    try:
        with connect_cm() as conn:
            try:
                conn = conn.execution_options(stream_results=True)  # server-side cursor
            except Exception:
                pass  # driver doesn't support it; chunking still bounds peak
            for chunk in pd.read_sql(text(sql), conn, chunksize=config.chunksize):
                rows += len(chunk)
                byte_estimate += int(chunk.memory_usage(deep=True).sum())
                if rows > config.max_rows or byte_estimate > config.max_bytes:
                    raise ResultTooLargeError(
                        rows=rows, byte_estimate=byte_estimate, limit_desc=config.limit_desc()
                    )
                table = pa.Table.from_pandas(chunk, preserve_index=False)
                if writer is None:
                    writer = pq.ParquetWriter(str(path), table.schema)
                writer.write_table(table)
        if writer is None:
            # Empty result with no chunk yielded: write an empty frame so the
            # LazyFrame still has a readable (0-row) Parquet to open.
            pd.DataFrame().to_parquet(path, index=False)
    except BaseException:
        if writer is not None:
            try:
                writer.close()
            except Exception:
                pass
            writer = None
        try:
            path.unlink(missing_ok=True)
        except Exception:
            pass
        raise
    finally:
        if writer is not None:
            writer.close()
    return path


def lazy_from_dataframe(df: pd.DataFrame, config: Optional[StreamConfig] = None) -> LazyFrame:
    """Wrap an already-materialized DataFrame as a LazyFrame by spilling it to a
    temp Parquet file.

    NOTE: this does NOT reduce ingest peak memory — the DataFrame is already fully
    in RAM. It provides uniform out-of-core *downstream* compute (filter/aggregate
    via DuckDB) and disk-backed reuse across every client. Clients that can stream
    should override execute_query_lazy to bound the ingest peak instead.
    """
    config = config or StreamConfig()
    config.root.mkdir(parents=True, exist_ok=True)
    path = config.root / f"lazy_{uuid.uuid4().hex}.parquet"
    df.to_parquet(path, index=False)
    return LazyFrame.from_parquet(path, owns_source=True)


def lazy_query_via_sqlalchemy(
    connect_cm: Callable[[], ContextManager],
    sql: str,
    config: Optional[StreamConfig] = None,
) -> LazyFrame:
    """Convenience: stream `sql` to a temp Parquet and return a LazyFrame over it.

    The returned LazyFrame owns the temp file and deletes it on `.close()`.
    """
    config = config or StreamConfig()
    path = config.root / f"lazy_{uuid.uuid4().hex}.parquet"
    stream_sqlalchemy_to_parquet(connect_cm, sql, path, config)
    return LazyFrame.from_parquet(path, owns_source=True)


def _consume_chunks_to_parquet(chunks, path: Path, config: StreamConfig) -> Path:
    """Write an iterable of DataFrame chunks to one Parquet file, enforcing the
    row/byte cap and aborting (with cleanup) if exceeded. Shared by the DBAPI
    streamers below."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    path.parent.mkdir(parents=True, exist_ok=True)
    writer: Optional["pq.ParquetWriter"] = None
    rows = 0
    byte_estimate = 0
    try:
        for chunk in chunks:
            rows += len(chunk)
            byte_estimate += int(chunk.memory_usage(deep=True).sum())
            if rows > config.max_rows or byte_estimate > config.max_bytes:
                raise ResultTooLargeError(
                    rows=rows, byte_estimate=byte_estimate, limit_desc=config.limit_desc()
                )
            table = pa.Table.from_pandas(chunk, preserve_index=False)
            if writer is None:
                writer = pq.ParquetWriter(str(path), table.schema)
            writer.write_table(table)
        if writer is None:
            pd.DataFrame().to_parquet(path, index=False)
    except BaseException:
        if writer is not None:
            try:
                writer.close()
            except Exception:
                pass
            writer = None
        try:
            path.unlink(missing_ok=True)
        except Exception:
            pass
        raise
    finally:
        if writer is not None:
            writer.close()
    return path


def stream_dbapi_readsql_to_parquet(connect_cm, sql, path, config):
    """Stream via pandas.read_sql over a *raw DBAPI* connection (no SQLAlchemy
    text() wrapping). For clients whose connect() yields a DBAPI connection that
    pandas reads directly: sqlite3, teradatasql, pyodbc."""
    def chunks():
        with connect_cm() as conn:
            yield from pd.read_sql(sql, conn, chunksize=config.chunksize)
    return _consume_chunks_to_parquet(chunks(), path, config)


def stream_dbapi_cursor_to_parquet(connect_cm, sql, path, config):
    """Stream via an explicit DBAPI cursor + fetchmany, for clients that drive a
    cursor directly (psycopg2, databricks-sql, pyodbc)."""
    def chunks():
        with connect_cm() as conn:
            cursor = conn.cursor()
            try:
                cursor.execute(sql)
                columns = [d[0] for d in cursor.description] if cursor.description else []
                produced = False
                while True:
                    batch = cursor.fetchmany(config.chunksize)
                    if not batch:
                        break
                    produced = True
                    yield pd.DataFrame.from_records(batch, columns=columns or None)
                if not produced and columns:
                    yield pd.DataFrame(columns=columns)  # keep schema for empty result
            finally:
                try:
                    cursor.close()
                except Exception:
                    pass
    return _consume_chunks_to_parquet(chunks(), path, config)


def stream_duckdb_to_parquet(connect_cm, sql, path, config):
    """Native DuckDB path: COPY the query result straight to Parquet. DuckDB
    streams to disk and never materializes the result in Python — zero ingest
    memory. Used by DuckDB-backed clients (duckdb, qvd)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    inner = sql.strip().rstrip(";")
    target = str(path).replace("'", "''")
    with connect_cm() as con:
        con.execute(f"COPY ({inner}) TO '{target}' (FORMAT PARQUET)")
    return path


def _lazy_via(stream_fn, connect_cm, sql, config) -> LazyFrame:
    config = config or StreamConfig()
    path = config.root / f"lazy_{uuid.uuid4().hex}.parquet"
    stream_fn(connect_cm, sql, path, config)
    return LazyFrame.from_parquet(path, owns_source=True)


def lazy_query_via_dbapi_readsql(connect_cm, sql, config=None) -> LazyFrame:
    return _lazy_via(stream_dbapi_readsql_to_parquet, connect_cm, sql, config)


def lazy_query_via_dbapi_cursor(connect_cm, sql, config=None) -> LazyFrame:
    return _lazy_via(stream_dbapi_cursor_to_parquet, connect_cm, sql, config)


def lazy_query_via_duckdb(connect_cm, sql, config=None) -> LazyFrame:
    return _lazy_via(stream_duckdb_to_parquet, connect_cm, sql, config)


# --- generic consumers: a client builds its own native iterator (Arrow batches,
#     DataFrame chunks, or row dicts) and hands it here to spill to a LazyFrame.

def _consume_arrow_to_parquet(arrow_iter, path: Path, config: StreamConfig) -> Path:
    """Write an iterable of pyarrow Tables/RecordBatches to one Parquet file,
    enforcing the cap. For clients with native Arrow streams (BigQuery, ClickHouse)."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    path.parent.mkdir(parents=True, exist_ok=True)
    writer: Optional["pq.ParquetWriter"] = None
    rows = 0
    byte_estimate = 0
    try:
        for obj in arrow_iter:
            table = pa.Table.from_batches([obj]) if isinstance(obj, pa.RecordBatch) else obj
            if table.num_rows == 0 and writer is not None:
                continue
            rows += table.num_rows
            byte_estimate += table.nbytes
            if rows > config.max_rows or byte_estimate > config.max_bytes:
                raise ResultTooLargeError(
                    rows=rows, byte_estimate=byte_estimate, limit_desc=config.limit_desc()
                )
            if writer is None:
                writer = pq.ParquetWriter(str(path), table.schema)
            writer.write_table(table)
        if writer is None:
            pd.DataFrame().to_parquet(path, index=False)
    except BaseException:
        if writer is not None:
            try:
                writer.close()
            except Exception:
                pass
            writer = None
        try:
            path.unlink(missing_ok=True)
        except Exception:
            pass
        raise
    finally:
        if writer is not None:
            writer.close()
    return path


def consume_chunks_to_lazyframe(chunks, config: Optional[StreamConfig] = None) -> LazyFrame:
    """Spill an iterable of DataFrame chunks to a LazyFrame (e.g. Athena wrangler
    chunksize iterator)."""
    config = config or StreamConfig()
    path = config.root / f"lazy_{uuid.uuid4().hex}.parquet"
    _consume_chunks_to_parquet(chunks, path, config)
    return LazyFrame.from_parquet(path, owns_source=True)


def consume_arrow_to_lazyframe(arrow_iter, config: Optional[StreamConfig] = None) -> LazyFrame:
    """Spill an iterable of pyarrow Tables/RecordBatches to a LazyFrame."""
    config = config or StreamConfig()
    path = config.root / f"lazy_{uuid.uuid4().hex}.parquet"
    _consume_arrow_to_parquet(arrow_iter, path, config)
    return LazyFrame.from_parquet(path, owns_source=True)


def consume_row_dicts_to_lazyframe(
    rows, columns=None, config: Optional[StreamConfig] = None
) -> LazyFrame:
    """Spill an iterable of row dicts/tuples to a LazyFrame, batching into chunks.
    For cursor/pagination sources (MongoDB, Spark toLocalIterator)."""
    config = config or StreamConfig()

    def chunks():
        buf = []
        for r in rows:
            buf.append(r)
            if len(buf) >= config.chunksize:
                yield pd.DataFrame(buf, columns=columns)
                buf = []
        if buf:
            yield pd.DataFrame(buf, columns=columns)

    return consume_chunks_to_lazyframe(chunks(), config)
