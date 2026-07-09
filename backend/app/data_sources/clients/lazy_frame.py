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

Nothing here changes existing behavior. A client opts in by setting
`_lazy_strategy` (base-class dispatch to a streamer below) or overriding
`execute_query_lazy` for bespoke streams; callers opt in by calling it instead
of `execute_query`.
"""

from __future__ import annotations

import logging
import os
import tempfile
import uuid
import weakref
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
# Re-sweep a root at most this often. Once-per-process is not enough for a
# long-lived server: files orphaned *after* startup would never be reclaimed
# until the next restart.
_SWEEP_INTERVAL_SECONDS = 3600
_last_sweep: dict = {}


def _sweep_stale_files(root: Path) -> None:
    """Best-effort orphan cleanup for the lazy spill dir, at most once per
    _SWEEP_INTERVAL_SECONDS per root. LazyFrame's finalizer deletes its own
    file, but a crashed/killed run never gets there and would leak Parquet
    files forever. Anything older than 24h is long past any live query's
    lifetime, so delete it. Only files matching our own naming pattern are
    touched, and errors are swallowed — this must never break a query."""
    import time

    now = time.monotonic()
    last = _last_sweep.get(root)
    if last is not None and now - last < _SWEEP_INTERVAL_SECONDS:
        return
    _last_sweep[root] = now

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


def _release_lazy_resources(con, paths) -> None:
    """Close the DuckDB connection and unlink the spill file(s). Module-level
    (not a method) so weakref.finalize doesn't hold a reference back to the
    LazyFrame, which would keep it alive forever."""
    if con is not None:
        try:
            con.close()
        except Exception:
            logger.debug("LazyFrame: failed to close duckdb connection", exc_info=True)
    for p in paths:
        try:
            Path(p).unlink(missing_ok=True)
        except Exception:
            logger.debug("LazyFrame: failed to unlink %s", p, exc_info=True)


class LazyFrame:
    """An out-of-core handle over a Parquet file via a DuckDB relation.

    Data stays on disk; DuckDB executes filters/aggregations and spills as
    needed. Only `.to_df()` / `.to_arrow()` pull results into memory — so keep
    those for the *reduced* result, not the raw scan.
    """

    def __init__(self, con, relation, source_path, owns_source: bool = True, parent: "LazyFrame" = None):
        self._con = con
        # None when the backing file has zero columns (empty result from a
        # schemaless source): valid Parquet, but DuckDB can't read a file with
        # no columns, so we short-circuit every accessor to an empty result.
        self._rel = relation
        # One or more backing Parquet files — several when the stream's column
        # set drifted mid-way and the writer rolled a new part file.
        if isinstance(source_path, (list, tuple)):
            self._source_paths = [Path(p) for p in source_path]
        else:
            self._source_paths = [Path(source_path)]
        self._owns_source = owns_source
        # Derived frames (owns_source=False) pin their parent: without this,
        # `execute_query_lazy(q).sql(...)` drops the owning frame immediately
        # and its finalizer would close the shared connection out from under
        # the derived frame.
        self._parent = parent
        # GC safety net: callers (LLM-generated code especially) routinely
        # never call close(), and the spill can be gigabytes. finalize() is
        # idempotent, so an explicit close() simply runs it early.
        self._finalizer = (
            weakref.finalize(self, _release_lazy_resources, con, list(self._source_paths))
            if owns_source
            else None
        )

    @classmethod
    def from_parquet(cls, path, owns_source: bool = True) -> "LazyFrame":
        import duckdb
        import pyarrow.parquet as pq

        paths = [Path(p) for p in path] if isinstance(path, (list, tuple)) else [Path(path)]
        # DuckDB cannot read a zero-column Parquet ("need at least one non-root
        # column"), which is what an empty result from a schemaless source
        # produces. Read only column-bearing files; with none left, fall back
        # to the schemaless-empty mode (every accessor short-circuits). All
        # paths stay tracked either way so close() removes every spill file.
        readable = [p for p in paths if pq.read_schema(p).names]
        if not readable:
            return cls(None, None, paths, owns_source=owns_source)
        con = duckdb.connect(database=":memory:")  # DuckDB spills to disk on its own
        # union_by_name reconciles part files whose column sets drifted:
        # missing columns become NULL, matching the eager pandas behavior.
        rel = con.read_parquet([str(p) for p in readable], union_by_name=len(readable) > 1)
        return cls(con, rel, paths, owns_source=owns_source)

    # -- lazy transforms (return new handles, no materialization) -----------

    def sql(self, sql_query: str, table_name: str = "data") -> "LazyFrame":
        """Run SQL against this result, referring to it as `table_name`.

        e.g. lf.sql("SELECT region, SUM(amount) FROM data GROUP BY region")
        """
        if self._rel is None:
            # Schemaless-empty mode: run the SQL against a 0-row relation so
            # aggregates keep SQL semantics (COUNT(*) returns one row with 0,
            # not an empty frame). The dummy column is the only way to build a
            # 0-row relation in DuckDB; a query referencing a real column fails
            # with a binder error naming it, which is honest — the schema is
            # unknown. SELECT * exposes `_bow_empty` with zero rows.
            import duckdb

            if self._con is None:
                self._con = duckdb.connect(database=":memory:")
            empty_rel = self._con.sql("SELECT NULL AS _bow_empty WHERE 1=0")
            new_rel = empty_rel.query(table_name, sql_query)
            return LazyFrame(self._con, new_rel, self._source_paths, owns_source=False, parent=self)
        new_rel = self._rel.query(table_name, sql_query)
        return LazyFrame(self._con, new_rel, self._source_paths, owns_source=False, parent=self)

    def limit(self, n: int) -> "LazyFrame":
        if self._rel is None:
            return LazyFrame(None, None, self._source_paths, owns_source=False, parent=self)
        return LazyFrame(self._con, self._rel.limit(n), self._source_paths, owns_source=False, parent=self)

    # -- materialization (explicit; this is where memory is spent) ----------

    def to_df(self) -> pd.DataFrame:
        if self._rel is None:
            return pd.DataFrame()
        return self._rel.df()

    def to_arrow(self):
        if self._rel is None:
            import pyarrow as pa

            return pa.table({})
        return self._rel.arrow()

    def row_count(self) -> int:
        if self._rel is None:
            return 0
        return int(self._rel.aggregate("count(*) AS n").fetchone()[0])

    def head(self, n: int = 10) -> pd.DataFrame:
        if self._rel is None:
            return pd.DataFrame()
        return self._rel.limit(n).df()

    @property
    def columns(self) -> list:
        if self._rel is None:
            return []
        return list(self._rel.columns)

    def byte_size(self) -> int:
        """On-disk size of the backing Parquet file(s), used for usage accounting
        without materializing the frame. Returns 0 if unavailable."""
        total = 0
        for p in self._source_paths:
            try:
                total += int(p.stat().st_size)
            except Exception:
                pass
        return total

    def uncompressed_byte_size(self) -> int:
        """Uncompressed columnar size from Parquet row-group metadata — the
        usage-accounting metric. The compressed on-disk size (byte_size) would
        under-charge lazy queries 3-10x relative to the eager path's
        materialized-size metering, silently loosening admin byte quotas.
        Metadata only; nothing is materialized. Falls back to on-disk size per
        part when metadata can't be read. Floored at the on-disk size: row-group
        total_byte_size is post-encoding (RLE/dictionary), which can collapse
        below the file size for low-cardinality data."""
        import pyarrow.parquet as pq

        total = 0
        for p in self._source_paths:
            try:
                md = pq.ParquetFile(str(p)).metadata
                total += sum(md.row_group(i).total_byte_size for i in range(md.num_row_groups))
            except Exception:
                try:
                    total += int(p.stat().st_size)
                except Exception:
                    pass
        return max(total, self.byte_size())

    # -- lifecycle ----------------------------------------------------------

    def close(self) -> None:
        # Derived frames from .sql()/.limit() share the parent's DuckDB connection
        # and backing file (owns_source=False). They must tear down neither, or a
        # `with lf.sql(...) as d:` block would close the connection the parent still
        # needs and any later parent.to_df() would fail on a closed connection.
        if not self._owns_source:
            return
        if self._finalizer is not None:
            self._finalizer()  # runs _release_lazy_resources at most once

    def __enter__(self) -> "LazyFrame":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


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
            try:
                conn = conn.execution_options(stream_results=True)  # server-side cursor
            except Exception:
                pass  # driver doesn't support it; chunking still bounds peak
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


def _dedupe_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Rename duplicate column names (id, id → id, id_1): pyarrow refuses to
    convert a frame with duplicates, which would kill every streamed lazy query
    whose SELECT list repeats a name (e.g. `SELECT *` over a join) — the eager
    path returns those fine. The rename matches DuckDB's own dedup convention.
    Chunks of one stream share a fixed column list, so renames are stable."""
    cols = [str(c) for c in df.columns]
    if len(set(cols)) == len(cols):
        return df
    seen: dict = {}
    renamed = []
    for c in cols:
        n = seen.get(c, 0)
        seen[c] = n + 1
        renamed.append(c if n == 0 else f"{c}_{n}")
    df = df.copy(deep=False)
    df.columns = renamed
    return df


def _jsonify_nested_cells(df: pd.DataFrame) -> pd.DataFrame:
    """JSON-encode dict/list cells so the Parquet write can't fail on nested
    values (pyarrow raises ArrowInvalid on dict cells — e.g. ADX dynamic
    columns, Mongo sub-documents). Clients with bespoke streaming overrides do
    this themselves; this covers the generic materialize-then-spill default.
    Returns the input unchanged when there is nothing to encode."""
    import json

    out = None
    for col in df.columns:
        series = df[col]
        if series.dtype != object:
            continue
        if not series.map(lambda v: isinstance(v, (dict, list))).any():
            continue
        if out is None:
            out = df.copy(deep=False)
        out[col] = series.map(
            lambda v: json.dumps(v, default=str) if isinstance(v, (dict, list)) else v
        )
    return df if out is None else out


def lazy_from_dataframe(df: pd.DataFrame, config: Optional[StreamConfig] = None) -> LazyFrame:
    """Wrap an already-materialized DataFrame as a LazyFrame by spilling it to a
    temp Parquet file. Enforces the same row/byte budget as the streamers so the
    generic fallback can't silently spill an unbounded result to the shared dir.

    NOTE: this does NOT reduce ingest peak memory — the DataFrame is already fully
    in RAM. It provides uniform out-of-core *downstream* compute (filter/aggregate
    via DuckDB) and disk-backed reuse across every client. Clients that can stream
    should override execute_query_lazy to bound the ingest peak instead.
    """
    config = config or StreamConfig()
    rows = len(df)
    byte_estimate = int(df.memory_usage(deep=True).sum())
    if rows > config.max_rows or byte_estimate > config.max_bytes:
        raise ResultTooLargeError(
            rows=rows, byte_estimate=byte_estimate, limit_desc=config.limit_desc()
        )
    config.root.mkdir(parents=True, exist_ok=True)
    path = config.root / f"lazy_{uuid.uuid4().hex}.parquet"
    _jsonify_nested_cells(_dedupe_columns(df)).to_parquet(path, index=False)
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
    paths = stream_sqlalchemy_to_parquet(connect_cm, sql, path, config)
    return LazyFrame.from_parquet(paths, owns_source=True)


def _consume_chunks_to_parquet(chunks, path: Path, config: StreamConfig, columns=None) -> list:
    """Write an iterable of DataFrame chunks to Parquet, enforcing the row/byte
    cap and aborting (with cleanup) if exceeded. Shared by the DBAPI streamers
    below. `columns`, when known up front, keeps the real schema in the Parquet
    even if the iterable yields no chunks at all.

    Returns the list of Parquet files written — usually one. A ParquetWriter's
    schema is frozen at the first chunk, but schemaless sources (e.g. Mongo)
    can grow or lose top-level columns between chunks; when the column *set*
    changes we roll a new part file and the reader unions parts by name
    (missing columns become NULL, matching eager pandas behavior)."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    path.parent.mkdir(parents=True, exist_ok=True)
    writer: Optional["pq.ParquetWriter"] = None
    schema = None
    paths = [path]
    rows = 0
    byte_estimate = 0

    def start_writer(table):
        nonlocal writer, schema
        table = _widen_null_columns(table)
        schema = table.schema
        writer = pq.ParquetWriter(str(paths[-1]), schema)
        return table

    def roll_part(table):
        nonlocal writer
        writer.close()
        writer = None
        paths.append(path.with_name(f"{path.stem}_part{len(paths)}.parquet"))
        return start_writer(table)

    try:
        for chunk in chunks:
            rows += len(chunk)
            byte_estimate += int(chunk.memory_usage(deep=True).sum())
            if rows > config.max_rows or byte_estimate > config.max_bytes:
                raise ResultTooLargeError(
                    rows=rows, byte_estimate=byte_estimate, limit_desc=config.limit_desc()
                )
            # _jsonify_nested_cells: SQL JSON/JSONB columns arrive as dict/list
            # cells (psycopg2, pymysql, ...); heterogeneous values within one
            # chunk make pa.Table.from_pandas raise ArrowInvalid and kill the
            # whole stream. Encode them like the eager-spill path does.
            chunk = _jsonify_nested_cells(_dedupe_columns(chunk))
            table = pa.Table.from_pandas(chunk, preserve_index=False)
            if table.num_columns == 0:
                # Rows carrying no fields at all (e.g. Mongo docs projected to
                # {}): nothing columnar to write, and a zero-column part file
                # would be unreadable by DuckDB. Budget-count them and move on.
                continue
            if writer is None:
                table = start_writer(table)
            elif set(table.schema.names) != set(schema.names):
                table = roll_part(table)
            else:
                if table.schema.names != schema.names:
                    table = table.select(schema.names)  # same columns, drifted order
                try:
                    table = _cast_chunk_to_schema(table, schema)
                except ValueError:
                    # Irreconcilable types — e.g. a sparse text column whose
                    # first 50k rows were all NULL locked the file schema to
                    # double, and now real strings arrive. Roll a new part;
                    # the read side's union_by_name promotes to a common type
                    # (double + varchar → varchar), matching eager pandas.
                    table = roll_part(table)
            writer.write_table(table)
        if writer is None:
            # Empty result with no chunk yielded: write a 0-row Parquet that
            # still carries the real column names (when known) so downstream
            # `.sql("SELECT col ...")` keeps working.
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
        for p in paths:
            try:
                p.unlink(missing_ok=True)
            except Exception:
                pass
        raise
    finally:
        if writer is not None:
            writer.close()
        _close_quietly(chunks)  # release the source connection promptly
    return paths


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
    memory. Used by DuckDB-backed clients (duckdb, qvd, csv).

    The query runs exactly once: a LIMIT max_rows+1 bound on the COPY enforces
    the row cap, then row count (from Parquet footer metadata — no re-scan) and
    on-disk size are checked post-hoc. An earlier version pre-estimated
    bytes/row with a LIMIT-1024 sample, but that re-executed the full inner
    pipeline (aggregations/joins don't short-circuit under LIMIT), doubling
    the source scan on every lazy query. The trade-off: a result that blows
    max_bytes is now detected after the write, so disk can transiently hold an
    oversized file (bounded by the row cap) before it is deleted here.

    Note: the SQL below interpolates `inner` (the client-built query, same string
    that all other streamers run) and the single-quote-escaped output path. No
    end-user-supplied value is interpolated unescaped, so this is not an
    injection surface beyond what the eager query path already trusts.
    """
    import pyarrow.parquet as pq

    path.parent.mkdir(parents=True, exist_ok=True)
    inner = _strip_sql_tail(sql)
    target = str(path).replace("'", "''")
    row_cap = int(config.max_rows)
    try:
        with connect_cm() as con:
            # The newline before the closing paren guards against a trailing
            # `-- comment` on the query's last code line swallowing the paren.
            # LIMIT row_cap + 1 so overflow is detectable without writing an
            # unbounded number of rows.
            bounded = f"SELECT * FROM (\n{inner}\n) AS _bow_src LIMIT {row_cap + 1}"
            con.execute(f"COPY ({bounded}) TO '{target}' (FORMAT PARQUET)")
        rows = int(pq.ParquetFile(str(path)).metadata.num_rows)
        disk_bytes = path.stat().st_size if path.exists() else 0
        if rows > row_cap or disk_bytes > config.max_bytes:
            raise ResultTooLargeError(
                rows=rows, byte_estimate=int(disk_bytes),
                limit_desc=config.limit_desc(),
            )
    except BaseException:
        try:
            path.unlink(missing_ok=True)
        except Exception:
            pass
        raise
    return path


def _lazy_via(stream_fn, connect_cm, sql, config) -> LazyFrame:
    config = config or StreamConfig()
    path = config.root / f"lazy_{uuid.uuid4().hex}.parquet"
    written = stream_fn(connect_cm, sql, path, config)
    return LazyFrame.from_parquet(written, owns_source=True)


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


def consume_chunks_to_lazyframe(chunks, config: Optional[StreamConfig] = None, columns=None) -> LazyFrame:
    """Spill an iterable of DataFrame chunks to a LazyFrame (e.g. Athena wrangler
    chunksize iterator). `columns`, when known, preserves the schema even for a
    fully-empty iterable."""
    config = config or StreamConfig()
    path = config.root / f"lazy_{uuid.uuid4().hex}.parquet"
    paths = _consume_chunks_to_parquet(chunks, path, config, columns=columns)
    return LazyFrame.from_parquet(paths, owns_source=True)


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

    return consume_chunks_to_lazyframe(chunks(), config, columns=columns)
