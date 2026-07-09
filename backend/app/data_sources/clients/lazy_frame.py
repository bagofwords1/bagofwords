"""Streaming query results to Parquet (out-of-core ingest, step 1 of 2).

The default `execute_query` path is unchanged: it runs the query and returns a
full `pandas.DataFrame` held in memory. This module adds the ingest half of a
*separate*, opt-in path that never materializes the whole result:

  Stream the source result in chunks (server-side cursor) straight to a
  Parquet file on disk. Peak memory during ingest is one chunk, not the whole
  result. An early byte/row cap aborts oversized scans *before* they OOM.

A follow-up adds the read half: a `LazyFrame` handle over the written Parquet
so filtering/aggregation run out-of-core in DuckDB. Nothing here changes
existing behavior; nothing calls these writers yet.
"""

from __future__ import annotations

import logging
import os
import tempfile
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


# Spill files older than this are considered orphans (see _sweep_stale_files).
_STALE_AFTER_SECONDS = 24 * 3600
_swept_roots: set = set()


def _sweep_stale_files(root: Path) -> None:
    """Best-effort orphan cleanup for the lazy spill dir, once per root per
    process. Consumers delete their own files, but a crashed/killed run
    never gets there and would leak Parquet files forever. Anything older than
    24h is long past any live query's lifetime, so delete it. Only files
    matching our own naming pattern are touched, and errors are swallowed —
    this must never break a query."""
    if root in _swept_roots:
        return
    _swept_roots.add(root)
    import time

    cutoff = time.time() - _STALE_AFTER_SECONDS
    try:
        for f in root.glob("lazy_*.parquet"):
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink(missing_ok=True)
            except OSError:
                continue
    except Exception:
        logger.debug("lazy_frame: stale-file sweep of %s failed", root, exc_info=True)


class StreamConfig:
    """Caps for streaming ingest. Generous defaults; tune via env."""

    def __init__(self) -> None:
        self.chunksize = _env_int("BOW_LAZY_CHUNKSIZE", 50_000)
        self.max_rows = _env_int("BOW_LAZY_MAX_ROWS", 50_000_000)
        self.max_bytes = _env_int("BOW_LAZY_MAX_BYTES", 8 * 1024 * 1024 * 1024)
        root = os.environ.get("BOW_LAZY_DIR")
        self.root = Path(root) if root else Path(tempfile.gettempdir()) / "bow_lazy"
        _sweep_stale_files(self.root)

    def limit_desc(self) -> str:
        return f"max_rows={self.max_rows}, max_bytes={self.max_bytes}"


def _close_quietly(it) -> None:
    """Explicitly close a generator so any `with connect_cm()` block inside it
    releases its connection *now*, not whenever refcount GC gets around to the
    suspended frame after an exception unwinds."""
    close = getattr(it, "close", None)
    if close is not None:
        try:
            close()
        except Exception:
            pass


def _widen_null_columns(table):
    """Prepare the FIRST chunk's schema for the ParquetWriter: a column that is
    all-NULL within that chunk infers pa.null(), which would lock the file
    schema to null and make every later non-null chunk unwritable. Widen null
    columns to nullable float64 — the common cause is a nullable numeric column
    — so later int/float chunks cast cleanly. (A later *string* chunk still
    fails the cast, with an error naming the column; that beats silently
    stringifying numbers.)"""
    import pyarrow as pa

    fields = [
        f.with_type(pa.float64()) if pa.types.is_null(f.type) else f
        for f in table.schema
    ]
    schema = pa.schema(fields, metadata=table.schema.metadata)
    return table if schema.equals(table.schema) else table.cast(schema)


def _cast_chunk_to_schema(table, schema):
    """Reconcile a later chunk's inferred schema with the writer's. Chunks are
    typed independently, so a nullable numeric column that happens to be
    all-NULL (or all-int) in one 50k-row chunk infers a different Arrow dtype
    and pq.ParquetWriter.write_table would abort the whole stream. Try a safe
    cast first, fall back to a lossy one, and if even that fails raise a clear
    error naming the offending column instead of pyarrow's opaque failure."""
    if table.schema.equals(schema, check_metadata=False):
        return table
    try:
        return table.cast(schema)
    except Exception:
        pass  # e.g. float chunk into an int column; retry lossy below
    try:
        return table.cast(schema, safe=False)
    except Exception as exc:
        if table.schema.names != schema.names:
            detail = (
                f"chunk columns {table.schema.names} do not match "
                f"file columns {schema.names}"
            )
        else:
            detail = "schemas differ"
            for field in schema:
                col = table.column(field.name)
                try:
                    col.cast(field.type, safe=False)
                except Exception:
                    detail = (
                        f"column '{field.name}' is {col.type} in this chunk "
                        f"but {field.type} in the file"
                    )
                    break
        raise ValueError(
            f"Streamed result chunks have irreconcilable schemas: {detail}. "
            "Cast the column to one type in the query (e.g. CAST(col AS DOUBLE))."
        ) from exc


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
    from sqlalchemy import text

    def chunks():
        with connect_cm() as conn:
            try:
                conn = conn.execution_options(stream_results=True)  # server-side cursor
            except Exception:
                pass  # driver doesn't support it; chunking still bounds peak
            result = conn.execute(text(sql))
            # The column list is known before any rows arrive, so a 0-row
            # result can still yield a schema-bearing empty frame (same
            # pattern as stream_dbapi_cursor_to_parquet). pd.read_sql's chunk
            # iterator can yield nothing for empty results, which would write
            # a zero-COLUMN Parquet and break later reads of specific columns.
            columns = list(result.keys())
            produced = False
            while True:
                batch = result.fetchmany(config.chunksize)
                if not batch:
                    break
                produced = True
                yield pd.DataFrame.from_records(batch, columns=columns or None)
            if not produced and columns:
                yield pd.DataFrame(columns=columns)  # keep schema for empty result

    gen = chunks()
    try:
        return _consume_chunks_to_parquet(gen, path, config)
    finally:
        gen.close()


def _consume_chunks_to_parquet(chunks, path: Path, config: StreamConfig, columns=None) -> Path:
    """Write an iterable of DataFrame chunks to one Parquet file, enforcing the
    row/byte cap and aborting (with cleanup) if exceeded. Shared by the DBAPI
    streamers below. `columns`, when known up front, keeps the real schema in
    the Parquet even if the iterable yields no chunks at all."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    path.parent.mkdir(parents=True, exist_ok=True)
    writer: Optional["pq.ParquetWriter"] = None
    schema = None
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
                table = _widen_null_columns(table)
                schema = table.schema
                writer = pq.ParquetWriter(str(path), schema)
            else:
                table = _cast_chunk_to_schema(table, schema)
            writer.write_table(table)
        if writer is None:
            # Empty result with no chunk yielded: write a 0-row Parquet that
            # still carries the real column names (when known) so downstream
            # reads of specific columns keep working.
            empty = pd.DataFrame(columns=list(columns)) if columns else pd.DataFrame()
            table = _widen_null_columns(pa.Table.from_pandas(empty, preserve_index=False))
            pq.write_table(table, str(path))
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
        _close_quietly(chunks)  # release the source connection promptly
    return path


def stream_dbapi_readsql_to_parquet(connect_cm, sql, path, config):
    """Stream via pandas.read_sql over a *raw DBAPI* connection (no SQLAlchemy
    text() wrapping). For clients whose connect() yields a DBAPI connection that
    pandas reads directly: sqlite3, teradatasql, pyodbc."""
    def chunks():
        with connect_cm() as conn:
            produced = False
            for chunk in pd.read_sql(sql, conn, chunksize=config.chunksize):
                produced = True
                yield chunk
            if not produced:
                # Older pandas yields no chunks at all for a 0-row result;
                # recover the column list from a cursor so the Parquet keeps
                # the real schema (cheap: the result is empty).
                cursor = conn.cursor()
                try:
                    cursor.execute(sql)
                    columns = [d[0] for d in cursor.description] if cursor.description else []
                finally:
                    try:
                        cursor.close()
                    except Exception:
                        pass
                if columns:
                    yield pd.DataFrame(columns=columns)
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
    memory. Used by DuckDB-backed clients (duckdb, qvd).

    Enforces the same StreamConfig caps as the chunked streamers: the COPY is
    bounded by `LIMIT max_rows + 1` so it can never write an unbounded file,
    then the actual row count and on-disk byte size are checked. If either cap
    is exceeded the partial file is deleted and ResultTooLargeError is raised.

    Note: the SQL below interpolates `inner` (the client-built query, same string
    that all other streamers run) and the single-quote-escaped output path. No
    end-user-supplied value is interpolated unescaped, so this is not an
    injection surface beyond what the eager query path already trusts.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    inner = sql.strip().rstrip(";")
    target = str(path).replace("'", "''")
    # Cap the result at max_rows + 1 so we can detect (and reject) overflow
    # without ever writing an unbounded file to disk.
    bounded = f"SELECT * FROM ({inner}) AS _bow_src LIMIT {int(config.max_rows) + 1}"
    try:
        with connect_cm() as con:
            con.execute(f"COPY ({bounded}) TO '{target}' (FORMAT PARQUET)")
            rows = con.execute(
                f"SELECT COUNT(*) FROM read_parquet('{target}')"
            ).fetchone()[0]
        byte_estimate = path.stat().st_size if path.exists() else 0
        if rows > config.max_rows or byte_estimate > config.max_bytes:
            raise ResultTooLargeError(
                rows=int(rows), byte_estimate=int(byte_estimate),
                limit_desc=config.limit_desc(),
            )
    except BaseException:
        try:
            path.unlink(missing_ok=True)
        except Exception:
            pass
        raise
    return path


# --- generic consumers: a client builds its own native iterator (Arrow batches,
#     DataFrame chunks, or row dicts) and hands it here to spill to Parquet.

def _consume_arrow_to_parquet(arrow_iter, path: Path, config: StreamConfig) -> Path:
    """Write an iterable of pyarrow Tables/RecordBatches to one Parquet file,
    enforcing the cap. For clients with native Arrow streams (BigQuery, ClickHouse)."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    path.parent.mkdir(parents=True, exist_ok=True)
    writer: Optional["pq.ParquetWriter"] = None
    schema = None
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
                table = _widen_null_columns(table)
                schema = table.schema
                writer = pq.ParquetWriter(str(path), schema)
            else:
                table = _cast_chunk_to_schema(table, schema)
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
        _close_quietly(arrow_iter)  # release the source stream promptly
    return path
