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

import datetime as _dt
import logging
import os
import stat as _stat
import tempfile
import threading
import uuid
import weakref
from decimal import Decimal
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


def _ensure_secure_root(root: Path) -> None:
    """Create/verify the spill root safely. Spill files hold complete query
    results, so the directory must be ours alone: created 0700, not a symlink
    (squatting attack: another user pre-creates or symlinks the path and reads
    every tenant's spills), and owned by the current uid."""
    try:
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
    except FileExistsError:
        pass
    st = os.lstat(root)
    if _stat.S_ISLNK(st.st_mode) or not _stat.S_ISDIR(st.st_mode):
        raise RuntimeError(
            f"Lazy spill dir {root} is a symlink or not a directory; refusing to "
            "spill query results into it. Set BOW_LAZY_DIR to a private directory."
        )
    if hasattr(os, "getuid") and st.st_uid != os.getuid():
        raise RuntimeError(
            f"Lazy spill dir {root} is owned by another user; refusing to spill "
            "query results into it. Set BOW_LAZY_DIR to a private directory."
        )
    try:
        os.chmod(root, 0o700)
    except OSError:
        logger.debug("Could not chmod spill root %s", root, exc_info=True)


def _restrict_file(path: Path) -> None:
    """chmod a freshly-created spill file to owner-only. Writers create files
    at the process umask (typically 0644 → world-readable query results)."""
    try:
        os.chmod(path, 0o600)
    except OSError:
        logger.debug("Could not chmod spill file %s", path, exc_info=True)


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
        # Aggregate guards: per-query budgets don't bound N concurrent
        # queries. dir_max_bytes caps the whole spill root; min_free_bytes
        # keeps a floor of free disk so one tenant's spill can't ENOSPC the
        # box for everyone.
        self.dir_max_bytes = _env_int("BOW_LAZY_DIR_MAX_BYTES", 32 * 1024 * 1024 * 1024)
        self.min_free_bytes = _env_int("BOW_LAZY_MIN_FREE_BYTES", 1024 * 1024 * 1024)
        root = os.environ.get("BOW_LAZY_DIR")
        if root:
            self.root = Path(root)
        else:
            # Per-uid default: a fixed name under world-writable /tmp is the
            # classic shared-tempdir pitfall (pre-creation/symlink squatting,
            # other users reading spilled query results).
            suffix = f"_{os.getuid()}" if hasattr(os, "getuid") else ""
            self.root = Path(tempfile.gettempdir()) / f"bow_lazy{suffix}"
        _ensure_secure_root(self.root)
        _sweep_stale_files(self.root)

    def limit_desc(self) -> str:
        return f"max_rows={self.max_rows}, max_bytes={self.max_bytes}"

    def check_capacity(self, full: bool = False) -> None:
        """Aggregate spill-root guard, called at stream start (full=True: also
        sum existing spill files) and per chunk (free-space only — one statvfs
        syscall). Raises ResultTooLargeError so the caller's cleanup and the
        413 handling apply unchanged."""
        import shutil

        try:
            free = shutil.disk_usage(self.root.parent if not self.root.exists() else self.root).free
        except Exception:
            return  # never let the guard itself break a query
        if free < self.min_free_bytes:
            raise ResultTooLargeError(
                rows=0, byte_estimate=0,
                limit_desc=f"spill disk free space below floor ({free} < {self.min_free_bytes})",
            )
        if not full:
            return
        try:
            total = sum(f.stat().st_size for f in self.root.glob("lazy_*.parquet"))
        except Exception:
            return
        if total > self.dir_max_bytes:
            raise ResultTooLargeError(
                rows=0, byte_estimate=int(total),
                limit_desc=f"aggregate spill dir over budget (dir_max_bytes={self.dir_max_bytes})",
            )


# --- cooperative cancellation ------------------------------------------------
# The code-exec wrapper runs each query on an abandonable daemon thread; when
# it times out, nothing used to stop the underlying stream from running to its
# full row/byte budget against the source. The wrapper registers its
# abandonment Event for the thread; the chunk consumers poll it between chunks
# and abort (with the normal partial-file cleanup) once it fires.
_thread_cancel = threading.local()


def set_cancel_event(event) -> None:
    """Register a threading.Event for the CURRENT thread's lazy streams."""
    _thread_cancel.event = event


def _cancelled() -> bool:
    e = getattr(_thread_cancel, "event", None)
    return bool(e is not None and e.is_set())


class QueryAbandonedError(RuntimeError):
    """The caller timed out and abandoned this stream; stop consuming."""

    def __init__(self) -> None:
        super().__init__(
            "Lazy stream aborted: the caller timed out and abandoned this query."
        )


def _open_duckdb(allowed_dirs):
    """Hardened DuckDB connection for LazyFrame compute.

    The relation is exposed to sandboxed LLM-generated code via .sql(), so an
    unrestricted connection would be a filesystem read/write escape hatch
    (read_csv('/etc/passwd'), COPY TO ...) around the Python sandbox's
    open/os bans — and each connection defaults to ~80% of system RAM.
    Confinement: file access limited to the spill dir(s), explicit memory
    budget (BOW_LAZY_DUCKDB_MEM, default 2GB), DuckDB's own out-of-core temp
    state under the spill root, and the configuration locked so generated SQL
    can't SET any of it back."""
    import duckdb

    con = duckdb.connect(database=":memory:")

    def _q(s) -> str:
        return str(s).replace("'", "''")

    dirs = sorted({str(Path(d)) for d in allowed_dirs})
    hardened = True
    try:
        quoted = ", ".join(f"'{_q(d)}'" for d in dirs)
        con.execute(f"SET allowed_directories=[{quoted}]")
    except Exception:
        # Older duckdb without allowed_directories: blanket-disabling external
        # access would also block the spill scan itself, so skip that knob and
        # keep the rest of the hardening.
        hardened = False
        logger.warning(
            "duckdb lacks allowed_directories; LazyFrame connection is NOT "
            "filesystem-confined", exc_info=True,
        )
    con.execute(f"SET memory_limit='{_q(os.environ.get('BOW_LAZY_DUCKDB_MEM') or '2GB')}'")
    if dirs:
        tmp_dir = Path(dirs[0]) / "duckdb_tmp"
        try:
            tmp_dir.mkdir(parents=True, exist_ok=True)
            con.execute(f"SET temp_directory='{_q(tmp_dir)}'")
        except Exception:
            logger.debug("Could not set duckdb temp_directory", exc_info=True)
    if hardened:
        con.execute("SET enable_external_access=false")
    con.execute("SET lock_configuration=true")
    return con


def arrow_safe_cell(v):
    """Coerce one row-dict value for the columnar (Parquet) spill. Shared by
    every row-dict lazy override (Salesforce, NetSuite, Spark, Mongo).

    dict/list → JSON string (schemaless nesting can't be a stable Arrow type);
    scalars pyarrow types natively — str/int/float/bool/datetime/date/time/
    bytes — pass through unchanged so timestamps stay timestamps (SUM/ORDER BY
    keep working downstream); Decimal → float (the lazy path's documented
    high-precision divergence, matching Decimal128 handling); anything else —
    driver-specific scalars like bson.Timestamp/Regex/Code — becomes str,
    because pa.Table.from_pandas raises ArrowInvalid on unknown Python types
    and would kill the whole stream. Eager paths are unaffected (pandas keeps
    raw objects)."""
    import json

    if isinstance(v, (dict, list)):
        return json.dumps(v, default=str)
    if v is None or isinstance(v, (str, int, float, bool, bytes, _dt.datetime, _dt.date, _dt.time)):
        return v
    if isinstance(v, Decimal):
        return float(v)
    return str(v)


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
        # Hardened connection: file access confined to the spill dir(s),
        # bounded memory, config locked (see _open_duckdb). DuckDB spills its
        # own compute state to disk under the same root.
        con = _open_duckdb({p.parent for p in readable})
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
            if self._con is None:
                self._con = _open_duckdb({p.parent for p in self._source_paths})
                # The finalizer snapshotted con=None at construction; re-register
                # so close()/GC also closes this lazily-created connection.
                if self._owns_source and self._finalizer is not None:
                    self._finalizer.detach()
                    self._finalizer = weakref.finalize(
                        self, _release_lazy_resources, self._con, list(self._source_paths)
                    )
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

    def spill_stats(self) -> tuple:
        """(rows, uncompressed_bytes, on_disk_bytes) in ONE metadata pass per
        part file — the usage-accounting read. Rows come from the Parquet
        footer (no DuckDB count(*) query), uncompressed bytes from row-group
        metadata. The compressed on-disk size alone would under-charge lazy
        queries 3-10x relative to the eager path's materialized-size metering,
        silently loosening admin byte quotas; but row-group total_byte_size is
        post-encoding (RLE/dictionary) and can collapse below the file size for
        low-cardinality data, so the charge is floored at the on-disk size.
        Metadata only; nothing is materialized. Falls back to on-disk size per
        part when metadata can't be read."""
        import pyarrow.parquet as pq

        rows = 0
        uncompressed = 0
        disk = 0
        for p in self._source_paths:
            try:
                part_disk = int(p.stat().st_size)
            except Exception:
                part_disk = 0
            disk += part_disk
            try:
                md = pq.ParquetFile(str(p)).metadata
                rows += int(md.num_rows)
                uncompressed += sum(
                    md.row_group(i).total_byte_size for i in range(md.num_row_groups)
                )
            except Exception:
                uncompressed += part_disk
        return rows, max(uncompressed, disk), disk

    def uncompressed_byte_size(self) -> int:
        """Usage-accounting byte size; see spill_stats."""
        return self.spill_stats()[1]

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
    and pq.ParquetWriter.write_table would abort the whole stream. SAFE casts
    only: a lossy fallback (cast(safe=False)) would silently floor a float
    chunk into an int64-locked file schema (3.7 → 3) — different numbers than
    the eager path. On failure raise a clear error naming the offending
    column; the chunk consumer rolls a new part file and the read side's
    union_by_name promotes to the common type (int64 + double → double)."""
    if table.schema.equals(schema, check_metadata=False):
        return table
    try:
        return table.cast(schema)
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
                    col.cast(field.type)
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
    config.check_capacity(full=True)
    config.root.mkdir(parents=True, exist_ok=True)
    path = config.root / f"lazy_{uuid.uuid4().hex}.parquet"
    _jsonify_nested_cells(_dedupe_columns(df)).to_parquet(path, index=False)
    _restrict_file(path)
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
        _restrict_file(paths[-1])
        return table

    def roll_part(table):
        nonlocal writer
        writer.close()
        writer = None
        paths.append(path.with_name(f"{path.stem}_part{len(paths)}.parquet"))
        return start_writer(table)

    try:
        config.check_capacity(full=True)
        for chunk in chunks:
            if _cancelled():
                raise QueryAbandonedError()
            config.check_capacity()
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
            _restrict_file(path)
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
                try:
                    cursor.close()
                except Exception:
                    pass
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
    import pyarrow as pa

    inner = _strip_sql_tail(sql)
    row_cap = int(config.max_rows)
    with connect_cm() as con:
        # The newline before the closing paren guards against a trailing
        # `-- comment` on the query's last code line swallowing the paren.
        # LIMIT row_cap + 1 so overflow is detectable without streaming an
        # unbounded number of rows.
        bounded = f"SELECT * FROM (\n{inner}\n) AS _bow_src LIMIT {row_cap + 1}"
        try:
            res = con.execute(bounded)
        except Exception:
            # Statements valid at top level but not as a subquery (SHOW
            # TABLES, PRAGMA, some DESCRIBE forms) — the eager path accepts
            # them, so run them bare. Their results are catalog-sized; the
            # row/byte budgets below still apply per batch.
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


def _lazy_via(stream_fn, connect_cm, sql, config) -> LazyFrame:
    config = config or StreamConfig()
    path = config.root / f"lazy_{uuid.uuid4().hex}.parquet"
    written = stream_fn(connect_cm, sql, path, config)
    return LazyFrame.from_parquet(written, owns_source=True)


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
        config.check_capacity(full=True)
        for obj in arrow_iter:
            if _cancelled():
                raise QueryAbandonedError()
            config.check_capacity()
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
                _restrict_file(path)
            else:
                table = _cast_chunk_to_schema(table, schema)
            writer.write_table(table)
        if writer is None:
            pd.DataFrame().to_parquet(path, index=False)
            _restrict_file(path)
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

    try:
        return consume_chunks_to_lazyframe(chunks(), config, columns=columns)
    finally:
        # Closing the chunks() wrapper doesn't cascade to the source iterator;
        # close it explicitly so `with client.connect()` blocks inside row
        # generators release their connection deterministically on abort
        # (budget errors, abandonment) instead of at GC time.
        _close_quietly(rows)
