"""Per-driver streamers and the public entry points clients use: stream a
query result (SQLAlchemy / raw DBAPI / native DuckDB / prebuilt iterators)
into a spill file and open it as a LazyFrame."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager, suppress
from pathlib import Path

import pandas as pd

from .config import StreamConfig
from .errors import ResultTooLargeError
from .frame import LazyFrame
from .ingest import (
    _close_quietly,
    _consume_arrow_to_parquet,
    _consume_chunks_to_parquet,
    _dedupe_columns,
    _jsonify_nested_cells,
)
from .sql_guard import ensure_single_read_statement


def stream_sqlalchemy_to_parquet(
    connect_cm: Callable[[], AbstractContextManager],
    sql: str,
    path: Path,
    config: StreamConfig,
) -> list:
    """Stream a SQLAlchemy query result to Parquet, chunk by chunk. Returns the
    list of files written (always one here — SQL results have a fixed schema).

    `connect_cm` is a zero-arg callable returning a context manager that yields a
    SQLAlchemy connection (e.g. a client's `connect` method). Aborts with
    ResultTooLargeError once the row/byte budget is exceeded, deleting the
    partial file.
    """
    from sqlalchemy import text

    def chunks():
        with connect_cm() as conn:
            # If the driver doesn't support server-side cursors, chunking
            # still bounds the peak.
            with suppress(Exception):
                conn = conn.execution_options(stream_results=True)  # server-side cursor
            result = conn.execute(text(sql))
            # The column list is known before any rows arrive, so a 0-row
            # result can still yield a schema-bearing empty frame (same
            # pattern as stream_dbapi_cursor_to_parquet). pd.read_sql's chunk
            # iterator can yield nothing for empty results, which would write
            # a zero-COLUMN Parquet and break later `.sql("SELECT col ...")`.
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


def _strip_sql_tail(sql: str) -> str:
    """Prepare a client query for embedding inside `SELECT * FROM (<sql>)`:
    drop trailing semicolons and trailing line-comment lines. A terminal `;`
    is a syntax error inside the wrapper, and an LLM-style `-- note` as the
    last line would otherwise sit between the query and the closing paren."""
    s = sql.strip()
    while True:
        prev = s
        lines = s.splitlines()
        while lines and lines[-1].lstrip().startswith("--"):
            lines.pop()
        s = "\n".join(lines).rstrip()
        while s.endswith(";"):
            s = s[:-1].rstrip()
        if s == prev:
            return s


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
                    with suppress(Exception):
                        cursor.close()
                if columns:
                    yield pd.DataFrame(columns=columns)
    return _consume_chunks_to_parquet(chunks(), path, config)


def stream_dbapi_cursor_to_parquet(connect_cm, sql, path, config, cursor_factory=None):
    """Stream via an explicit DBAPI cursor + fetchmany, for clients that drive a
    cursor directly (psycopg2, databricks-sql, pyodbc).

    `cursor_factory(conn, config)`, when given, replaces the default
    `conn.cursor()` — e.g. Redshift's named server-side cursor, whose
    description is only populated after the first fetch declares the portal
    (hence columns being read lazily inside the loop)."""
    def chunks():
        with connect_cm() as conn:
            cursor = cursor_factory(conn, config) if cursor_factory else conn.cursor()
            try:
                cursor.execute(sql)
                columns = None
                produced = False
                while True:
                    batch = cursor.fetchmany(config.chunksize)
                    if columns is None:
                        columns = [d[0] for d in cursor.description] if cursor.description else []
                    if not batch:
                        break
                    produced = True
                    yield pd.DataFrame.from_records(batch, columns=columns or None)
                if not produced and columns:
                    yield pd.DataFrame(columns=columns)  # keep schema for empty result
            finally:
                with suppress(Exception):
                    cursor.close()
    return _consume_chunks_to_parquet(chunks(), path, config)


def stream_duckdb_to_parquet(connect_cm, sql, path, config):
    """Native DuckDB path: execute once, stream Arrow record batches through
    the shared Arrow consumer. Used by DuckDB-backed clients (duckdb, qvd, csv).

    The query runs exactly once (an earlier version pre-sampled with LIMIT 1024
    to derive a byte cap, doubling the source scan; a later one used a single
    COPY, but that enforced max_bytes only after the full file was written — a
    wide 50M-row result could fill the shared disk before the check fired).
    Streaming batches through _consume_arrow_to_parquet enforces both budgets
    incrementally: an oversized result is rejected at ~max_bytes written, like
    every other streamer. Peak memory is one record batch.

    Note: the SQL below interpolates `inner` (the client-built query, same string
    that all other streamers run). No end-user-supplied value is interpolated
    unescaped, so this is not an injection surface beyond what the eager query
    path already trusts.
    """
    import duckdb
    import pyarrow as pa

    inner = _strip_sql_tail(sql)
    # This strategy sometimes executes the source statement bare (SHOW /
    # DESCRIBE cannot be nested under SELECT). Parse before that fallback and
    # allow only read statements; otherwise COPY/ATTACH/DML could write through
    # the source DuckDB connection before a LazyFrame even exists.
    ensure_single_read_statement(
        inner,
        allowed_keywords={"SELECT", "WITH", "SHOW", "DESCRIBE", "DESC"},
        error_message="DuckDB lazy queries accept one read-only query",
    )
    row_cap = int(config.max_rows)
    with connect_cm() as con:
        # The newline before the closing paren guards against a trailing
        # `-- comment` on the query's last code line swallowing the paren.
        # LIMIT row_cap + 1 so overflow is detectable without streaming an
        # unbounded number of rows.
        bounded = f"SELECT * FROM (\n{inner}\n) AS _bow_src LIMIT {row_cap + 1}"
        try:
            res = con.execute(bounded)
        except duckdb.ParserException:
            # ParserException ONLY: statements valid at top level but not as
            # a subquery (SHOW TABLES, PRAGMA, some DESCRIBE forms) — the
            # eager path accepts them, so run them bare; their results are
            # catalog-sized and the budgets below still apply per batch. Any
            # other failure (OOM, IO, binder) must propagate — retrying it
            # bare would re-execute a genuinely failing query without the
            # row LIMIT.
            res = con.execute(inner)
        # to_arrow_reader() replaced fetch_record_batch() in newer duckdb
        make_reader = getattr(res, "to_arrow_reader", None) or res.fetch_record_batch
        reader = make_reader(config.chunksize)

        def batches():
            produced = False
            for batch in reader:
                produced = True
                yield batch
            if not produced:
                # 0-row result: keep the real column schema in the spill.
                yield pa.Table.from_batches([], schema=reader.schema)

        _consume_arrow_to_parquet(batches(), path, config)
    return path


def _from_parquet_or_cleanup(paths, config: StreamConfig) -> LazyFrame:
    """Open the freshly-written spill as a LazyFrame; if opening fails
    (corrupt footer, fs error), delete the spill instead of orphaning
    a multi-GB file until the stale sweep."""
    try:
        return LazyFrame._from_parquet(
            paths,
            owns_source=True,
            stream_config=config,
        )
    except BaseException:
        config.storage.release(paths if isinstance(paths, (list, tuple)) else [paths])
        raise


def _lazy_via(stream_fn, connect_cm, sql, config) -> LazyFrame:
    config = config or StreamConfig()
    path = config.new_spill_path()
    try:
        written = stream_fn(connect_cm, sql, path, config)
        return _from_parquet_or_cleanup(written, config)
    except BaseException:
        # Covers setup failures before a consumer installs its own cleanup
        # (connect/execute/reader creation), and removes every part/temp file by
        # deleting the private q_* parent.
        config.storage.release([path])
        raise


def lazy_from_dataframe(df: pd.DataFrame, config: StreamConfig | None = None) -> LazyFrame:
    """Wrap an already-materialized DataFrame as a LazyFrame by spilling it to a
    temp Parquet file. Enforces the same row/byte budget as the streamers so the
    generic fallback can't silently spill an unbounded result to the shared dir.

    NOTE: this does NOT reduce ingest peak memory — the DataFrame is already fully
    in RAM. It provides uniform out-of-core *downstream* compute (filter/aggregate
    via DuckDB) and disk-backed reuse across every client. Clients that can stream
    should override execute_query_lazy to bound the ingest peak instead.
    """
    config = config or StreamConfig()
    normalized = _jsonify_nested_cells(_dedupe_columns(df))
    rows = len(normalized)
    # Meter the representation that is actually written. A dict object can
    # report a few hundred pandas bytes yet JSON-expand to megabytes.
    byte_estimate = int(normalized.memory_usage(deep=True).sum())
    if rows > config.max_rows or byte_estimate > config.max_bytes:
        raise ResultTooLargeError(
            rows=rows, byte_estimate=byte_estimate, limit_desc=config.limit_desc()
        )
    config.check_capacity(full=True)
    path = config.new_spill_path()
    try:
        normalized.to_parquet(path, index=False)
        config.storage.restrict_file(path)
        config.check_capacity(full=True)
        return _from_parquet_or_cleanup(path, config)
    except BaseException:
        config.storage.release([path])
        raise


def lazy_query_via_sqlalchemy(
    connect_cm: Callable[[], AbstractContextManager],
    sql: str,
    config: StreamConfig | None = None,
) -> LazyFrame:
    """Convenience: stream `sql` to a temp Parquet and return a LazyFrame over it.

    The returned LazyFrame owns the temp file and deletes it on `.close()`.
    """
    return _lazy_via(stream_sqlalchemy_to_parquet, connect_cm, sql, config)


def lazy_query_via_dbapi_readsql(connect_cm, sql, config=None) -> LazyFrame:
    return _lazy_via(stream_dbapi_readsql_to_parquet, connect_cm, sql, config)


def lazy_query_via_dbapi_cursor(connect_cm, sql, config=None, cursor_factory=None) -> LazyFrame:
    def stream(cm, s, p, cfg):
        return stream_dbapi_cursor_to_parquet(cm, s, p, cfg, cursor_factory=cursor_factory)

    return _lazy_via(stream, connect_cm, sql, config)


def lazy_query_via_duckdb(connect_cm, sql, config=None) -> LazyFrame:
    return _lazy_via(stream_duckdb_to_parquet, connect_cm, sql, config)


# --- generic consumers: a client builds its own native iterator (Arrow batches,
#     DataFrame chunks, or row dicts) and hands it here to spill to a LazyFrame.

def consume_chunks_to_lazyframe(chunks, config: StreamConfig | None = None, columns=None) -> LazyFrame:
    """Spill an iterable of DataFrame chunks to a LazyFrame (e.g. Athena wrangler
    chunksize iterator). `columns`, when known, preserves the schema even for a
    fully-empty iterable."""
    config = config or StreamConfig()
    path = config.new_spill_path()
    paths = _consume_chunks_to_parquet(chunks, path, config, columns=columns)
    return _from_parquet_or_cleanup(paths, config)


def consume_arrow_to_lazyframe(arrow_iter, config: StreamConfig | None = None) -> LazyFrame:
    """Spill an iterable of pyarrow Tables/RecordBatches to a LazyFrame."""
    config = config or StreamConfig()
    path = config.new_spill_path()
    _consume_arrow_to_parquet(arrow_iter, path, config)
    return _from_parquet_or_cleanup(path, config)


def consume_row_dicts_to_lazyframe(
    rows, columns=None, config: StreamConfig | None = None
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

    try:
        return consume_chunks_to_lazyframe(chunks(), config, columns=columns)
    finally:
        # Closing the chunks() wrapper doesn't cascade to the source iterator;
        # close it explicitly so `with client.connect()` blocks inside row
        # generators release their connection deterministically on abort
        # (budget errors, abandonment) instead of at GC time.
        _close_quietly(rows)
