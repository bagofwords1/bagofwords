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
from contextlib import suppress
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


class LazyComputeTimeoutError(AppError):
    """Raised when downstream DuckDB work exceeds its wall-clock budget."""

    def __init__(self, timeout_seconds: float) -> None:
        message = (
            f"Lazy result computation exceeded {timeout_seconds:g}s. Narrow the "
            "query, apply filters/aggregation before materializing, or use LIMIT."
        )
        super().__init__(
            ErrorCode.QUERY_TIMEOUT,
            message,
            status_code=408,
            params={"timeout_seconds": timeout_seconds},
        )
        self.timeout_seconds = timeout_seconds


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ[name])
    except (KeyError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ[name])
    except (KeyError, ValueError):
        return default


def _materialization_limits() -> tuple[int, int]:
    """Operator ceilings shared by every public materialization surface."""
    max_rows = max(0, _env_int("BOW_LAZY_RESULT_MATERIALIZE_CAP", 1_000_000))
    max_bytes = max(
        0,
        _env_int(
            "BOW_LAZY_RESULT_MATERIALIZE_MAX_BYTES",
            512 * 1024 * 1024,
        ),
    )
    return max_rows, max_bytes


def _ensure_secure_root(root: Path, strict: bool) -> None:
    """Create/verify the spill root. Spill files hold complete query results.

    strict=True (the DEFAULT root under the shared system tempdir): created
    0700 and verified — not a symlink (squatting attack: another user
    pre-creates or symlinks the path and reads every tenant's spills) and
    owned by the current uid.

    strict=False (an explicit BOW_LAZY_DIR): the operator chose the path, and
    legitimate deployments break under the strict rules (root-owned k8s/docker
    volume mount points, symlinks onto a big disk) — and chmod'ing an
    operator-owned dir could strip access other processes rely on. Just create
    it if missing; per-query subdirectories are still created 0700/owned by
    us, which is what actually protects spill contents."""
    if not strict:
        root.mkdir(parents=True, exist_ok=True)
        return
    with suppress(FileExistsError):
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
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

# A Parquet file cannot represent N rows with zero columns. Schemaless sources
# can nevertheless yield real rows such as Mongo projections to ``{}``, so a
# per-stream marker carries those rows across part-file unioning. Field metadata
# proves the marker is internal; a user column merely resembling its name must
# never be hidden.
_EMPTY_ROW_SENTINEL_PREFIX = "__bow_internal_empty_row_"
_EMPTY_ROW_METADATA_KEY = b"bow.empty_row_sentinel"
_EMPTY_ROW_METADATA_VALUE = b"1"

# Only trusted spill factories may create a frame whose finalizer can delete
# source files. Generated code cannot import this module or access private
# attributes, and an arbitrary object cannot satisfy identity comparison.
_LAZYFRAME_OWNERSHIP_TOKEN = object()


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

    import shutil

    cutoff = time.time() - _STALE_AFTER_SECONDS
    try:
        for f in root.glob("lazy_*.parquet"):  # legacy flat spills
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink(missing_ok=True)
            except OSError:
                continue
        # Per-query q_* dirs hold spill parts AND DuckDB temp state; a
        # crashed/killed process orphans the whole dir, so sweep it as a unit.
        for d in root.glob("q_*"):
            try:
                if d.is_dir() and d.stat().st_mtime < cutoff:
                    shutil.rmtree(d, ignore_errors=True)
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
            _ensure_secure_root(self.root, strict=False)
        else:
            # Per-uid default: a fixed name under world-writable /tmp is the
            # classic shared-tempdir pitfall (pre-creation/symlink squatting,
            # other users reading spilled query results).
            suffix = f"_{os.getuid()}" if hasattr(os, "getuid") else ""
            self.root = Path(tempfile.gettempdir()) / f"bow_lazy{suffix}"
            _ensure_secure_root(self.root, strict=True)
        _sweep_stale_files(self.root)

    def new_spill_path(self) -> Path:
        """Reserve a spill location inside a PRIVATE per-query subdirectory
        (root/q_<hex>/lazy_<hex>.parquet, dir mode 0700).

        The subdirectory is the isolation boundary: the LazyFrame's DuckDB
        connection is confined (allowed_directories) to it, so sandboxed code
        holding one frame cannot glob-read or COPY-overwrite OTHER queries'
        in-flight spills — which it could when every query spilled flat into
        one shared root. DuckDB's own temp state lives here too, so releasing
        the frame (or sweeping a crashed query's leftovers) is one rmtree."""
        q_dir = self.root / f"q_{uuid.uuid4().hex}"
        q_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        return q_dir / f"lazy_{uuid.uuid4().hex}.parquet"

    def limit_desc(self) -> str:
        return f"max_rows={self.max_rows}, max_bytes={self.max_bytes}"

    def check_capacity(self, full: bool = False) -> None:
        """Enforce aggregate spill-root and free-space limits.

        ``full=True`` sums every file under the dedicated spill root, including
        Parquet parts and DuckDB temp state. Consumers call it both before and
        after growth so a query cannot start below the cap and then silently
        push the root over it.
        """
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
            total = sum(f.stat().st_size for f in self.root.rglob("*") if f.is_file())
        except Exception:
            return
        if total > self.dir_max_bytes:
            raise ResultTooLargeError(
                rows=0, byte_estimate=int(total),
                limit_desc=f"aggregate spill dir over budget (dir_max_bytes={self.dir_max_bytes})",
            )

    def remaining_capacity_bytes(self) -> int:
        """Best available DuckDB temp allowance under aggregate/free-space caps.

        DuckDB otherwise defaults to 90% of the whole filesystem. Even if a
        stat call fails, the configured aggregate cap remains an upper bound.
        """
        import shutil

        try:
            total = sum(
                f.stat().st_size for f in self.root.rglob("*") if f.is_file()
            )
        except Exception:
            total = 0
        remaining = self.dir_max_bytes - total
        try:
            free = shutil.disk_usage(
                self.root.parent if not self.root.exists() else self.root
            ).free
            remaining = min(remaining, free - self.min_free_bytes)
        except Exception:
            pass
        if remaining <= 0:
            raise ResultTooLargeError(
                rows=0,
                byte_estimate=max(0, int(total)),
                limit_desc=(
                    "no spill capacity remains under aggregate/free-space budget "
                    f"(dir_max_bytes={self.dir_max_bytes}, "
                    f"min_free_bytes={self.min_free_bytes})"
                ),
            )
        return int(remaining)


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


_spill_reservation_lock = threading.Lock()
_spill_reserved_bytes: dict[Path, int] = {}


def _reserve_duckdb_temp(config: StreamConfig) -> tuple[Path, int]:
    """Atomically reserve one connection's potential DuckDB spill growth.

    A capacity snapshot alone races: several connections can all observe the
    same remaining bytes and each configure DuckDB to consume the full amount.
    Reservations make those live maxima disjoint within this server process.
    The physical root scan still guards capacity shared with other processes.
    """
    root = config.root.resolve()
    max_connections = max(
        1,
        _env_int("BOW_LAZY_MAX_CONCURRENT_COMPUTES", 8),
    )
    per_connection_limit = min(
        max(0, int(config.max_bytes)),
        max(0, int(config.dir_max_bytes)) // max_connections,
    )
    if per_connection_limit <= 0:
        raise ResultTooLargeError(
            rows=0,
            byte_estimate=0,
            limit_desc="DuckDB temp spill budget is zero",
        )

    with _spill_reservation_lock:
        remaining = config.remaining_capacity_bytes()
        already_reserved = _spill_reserved_bytes.get(root, 0)
        available = remaining - already_reserved
        budget = min(per_connection_limit, available)
        if budget <= 0:
            raise ResultTooLargeError(
                rows=0,
                byte_estimate=max(0, int(already_reserved)),
                limit_desc=(
                    "no unreserved DuckDB spill capacity remains "
                    f"(dir_max_bytes={config.dir_max_bytes})"
                ),
            )
        _spill_reserved_bytes[root] = already_reserved + int(budget)
        return root, int(budget)


def _release_duckdb_temp(root: Path, budget: int) -> None:
    with _spill_reservation_lock:
        remaining = _spill_reserved_bytes.get(root, 0) - int(budget)
        if remaining > 0:
            _spill_reserved_bytes[root] = remaining
        else:
            _spill_reserved_bytes.pop(root, None)


class _ReservedDuckDBConnection:
    """Connection proxy that releases its spill reservation exactly once."""

    def __init__(self, inner, reservation_root: Path, reservation_bytes: int):
        self._inner = inner
        self._reservation_root = reservation_root
        self._reservation_bytes = reservation_bytes
        self._closed = False
        self._close_lock = threading.Lock()

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()
        return False

    def close(self) -> None:
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
            try:
                self._inner.close()
            finally:
                _release_duckdb_temp(
                    self._reservation_root,
                    self._reservation_bytes,
                )


def _open_duckdb(allowed_dirs, config: Optional[StreamConfig] = None):
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

    config = config or StreamConfig()
    config.check_capacity(full=True)
    reservation_root, temp_budget = _reserve_duckdb_temp(config)
    try:
        con = duckdb.connect(database=":memory:")
    except BaseException:
        _release_duckdb_temp(reservation_root, temp_budget)
        raise

    def _q(s) -> str:
        return str(s).replace("'", "''")

    dirs = sorted({str(Path(d)) for d in allowed_dirs})
    try:
        quoted = ", ".join(f"'{_q(d)}'" for d in dirs)
        con.execute(f"SET allowed_directories=[{quoted}]")
        con.execute(
            f"SET memory_limit='{_q(os.environ.get('BOW_LAZY_DUCKDB_MEM') or '2GB')}'"
        )
        con.execute(f"SET max_temp_directory_size='{temp_budget}B'")
        if dirs:
            tmp_dir = Path(dirs[0]) / "duckdb_tmp"
            tmp_dir.mkdir(parents=True, exist_ok=True)
            con.execute(f"SET temp_directory='{_q(tmp_dir)}'")
        con.execute("SET enable_external_access=false")
        con.execute("SET lock_configuration=true")
    except Exception as exc:
        # This connection is exposed through LazyFrame.sql() to generated code.
        # Returning it without every confinement knob would turn a config/version
        # problem into a filesystem sandbox escape, so fail closed.
        with suppress(Exception):
            con.close()
        _release_duckdb_temp(reservation_root, temp_budget)
        raise RuntimeError("DuckDB filesystem confinement could not be configured") from exc
    return _ReservedDuckDBConnection(con, reservation_root, temp_budget)


def arrow_safe_cell(v):
    """Coerce one row-dict value for the columnar (Parquet) spill. Shared by
    every row-dict lazy override (Salesforce, NetSuite, Spark, Mongo).

    dict/list → JSON string (schemaless nesting can't be a stable Arrow type);
    scalars pyarrow types natively — str/int/float/bool/datetime/date/time/
    bytes/Decimal — pass through unchanged so timestamps stay timestamps and
    exact financial values stay exact; anything else —
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
        return v
    return str(v)


def _release_lazy_resources(con, paths) -> None:
    """Close the DuckDB connection and remove the spill file(s) plus their
    private q_* directory (which also holds DuckDB's temp state). Module-level
    (not a method) so weakref.finalize doesn't hold a reference back to the
    LazyFrame, which would keep it alive forever."""
    import shutil

    if con is not None:
        try:
            con.close()
        except Exception:
            logger.debug("LazyFrame: failed to close duckdb connection", exc_info=True)
    parents = set()
    for p in paths:
        p = Path(p)
        try:
            p.unlink(missing_ok=True)
        except Exception:
            logger.debug("LazyFrame: failed to unlink %s", p, exc_info=True)
        if p.parent.name.startswith("q_"):
            parents.add(p.parent)
    for d in parents:
        shutil.rmtree(d, ignore_errors=True)


class LazyFrame:
    """An out-of-core handle over a Parquet file via a DuckDB relation.

    Data stays on disk; DuckDB executes filters/aggregations and spills as
    needed. Only `.to_df()` / `.to_arrow()` pull results into memory — so keep
    those for the *reduced* result, not the raw scan.
    """

    def __init__(
        self,
        con,
        relation,
        source_path,
        owns_source: bool = True,
        parent: Optional[LazyFrame] = None,
        hidden_columns=None,
        stream_config: Optional[StreamConfig] = None,
        compute_timeout_seconds: Optional[float] = None,
        _ownership_token=None,
    ):
        if owns_source and _ownership_token is not _LAZYFRAME_OWNERSHIP_TOKEN:
            raise PermissionError(
                "Owning LazyFrame construction is restricted to trusted factories"
            )
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
        self._hidden_columns = frozenset(hidden_columns or ())
        # Derived frames (owns_source=False) pin their parent: without this,
        # `execute_query_lazy(q).sql(...)` drops the owning frame immediately
        # and its finalizer would close the shared connection out from under
        # the derived frame.
        self._parent = parent
        self._stream_config = (
            stream_config
            or (parent._stream_config if parent is not None else None)
            or StreamConfig()
        )
        inherited_timeout = (
            parent._compute_timeout_seconds if parent is not None else None
        )
        timeout = (
            compute_timeout_seconds
            if compute_timeout_seconds is not None
            else inherited_timeout
        )
        if timeout is None:
            timeout = _env_float("BOW_LAZY_COMPUTE_TIMEOUT_SECONDS", 60.0)
        self._compute_timeout_seconds = float(timeout) if float(timeout) > 0 else 60.0
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
        """Reject caller-selected files.

        Spill opening grants a DuckDB connection access to the file's parent
        and ownership may delete the file on close. Only the trusted stream
        helpers may cross that boundary through ``_from_parquet``.
        """
        raise PermissionError("LazyFrame.from_parquet is internal-only")

    @classmethod
    def _from_parquet(
        cls,
        path,
        owns_source: bool = True,
        stream_config: Optional[StreamConfig] = None,
    ) -> "LazyFrame":
        import pyarrow.parquet as pq

        stream_config = stream_config or StreamConfig()

        paths = [Path(p) for p in path] if isinstance(path, (list, tuple)) else [Path(path)]
        # DuckDB cannot read a zero-column Parquet ("need at least one non-root
        # column"), which is what an empty result from a schemaless source
        # produces. Read only column-bearing files; with none left, fall back
        # to the schemaless-empty mode (every accessor short-circuits). All
        # paths stay tracked either way so close() removes every spill file.
        schemas = [(p, pq.read_schema(p)) for p in paths]
        readable = [p for p, schema in schemas if schema.names]
        hidden_columns = {
            field.name
            for _, schema in schemas
            for field in schema
            if (field.metadata or {}).get(_EMPTY_ROW_METADATA_KEY)
            == _EMPTY_ROW_METADATA_VALUE
        }
        if not readable:
            return cls(
                None,
                None,
                paths,
                owns_source=owns_source,
                hidden_columns=hidden_columns,
                stream_config=stream_config,
                _ownership_token=_LAZYFRAME_OWNERSHIP_TOKEN,
            )
        # Hardened connection: file access confined to the spill dir(s),
        # bounded memory, config locked (see _open_duckdb). DuckDB spills its
        # own compute state to disk under the same root.
        con = _open_duckdb({p.parent for p in readable}, stream_config)
        # union_by_name reconciles part files whose column sets drifted:
        # missing columns become NULL, matching the eager pandas behavior.
        try:
            rel = con.read_parquet(
                [str(p) for p in readable],
                union_by_name=len(readable) > 1,
            )
        except BaseException:
            con.close()
            raise
        return cls(
            con,
            rel,
            paths,
            owns_source=owns_source,
            hidden_columns=hidden_columns,
            stream_config=stream_config,
            _ownership_token=_LAZYFRAME_OWNERSHIP_TOKEN,
        )

    def _set_compute_timeout(self, timeout_seconds: float) -> None:
        """Apply the wrapper's per-connection timeout to downstream compute."""
        if isinstance(timeout_seconds, (int, float)) and timeout_seconds > 0:
            self._compute_timeout_seconds = float(timeout_seconds)

    # -- lazy transforms (return new handles, no materialization) -----------

    def sql(self, sql_query: str, table_name: str = "data") -> "LazyFrame":
        """Run SQL against this result, referring to it as `table_name`.

        e.g. lf.sql("SELECT region, SUM(amount) FROM data GROUP BY region")
        """
        import re

        import duckdb

        # This API is directly exposed to generated code. Directory confinement
        # limits WHERE a query can write, but DuckDB COPY can still fill that
        # private directory outside every spill budget. Require exactly one
        # read-only SELECT/WITH statement at this boundary as defense in depth.
        stripped = re.sub(r"\A(?:\s|--[^\n]*(?:\n|$)|/\*.*?\*/)*", "", sql_query, flags=re.S)
        first = re.match(r"[A-Za-z]+", stripped)
        statements = duckdb.extract_statements(sql_query)
        if (
            first is None
            or first.group(0).upper() not in {"SELECT", "WITH"}
            or len(statements) != 1
            or statements[0].type != duckdb.StatementType.SELECT
        ):
            raise ValueError("LazyFrame.sql accepts one read-only SELECT/WITH query")

        if self._rel is None:
            # Schemaless-empty mode: run the SQL against a 0-row relation so
            # aggregates keep SQL semantics (COUNT(*) returns one row with 0,
            # not an empty frame). The dummy column is the only way to build a
            # 0-row relation in DuckDB; a query referencing a real column fails
            # with a binder error naming it, which is honest — the schema is
            # unknown. SELECT * exposes `_bow_empty` with zero rows.
            if self._con is None:
                self._con = _open_duckdb(
                    {p.parent for p in self._source_paths},
                    self._stream_config,
                )
                # The finalizer snapshotted con=None at construction (or was
                # never registered, for a derived frame); re-register so
                # close()/GC also closes this lazily-created connection. A
                # derived frame gets a connection-only finalizer — the spill
                # files still belong to the owning frame.
                if self._finalizer is not None:
                    self._finalizer.detach()
                self._finalizer = weakref.finalize(
                    self,
                    _release_lazy_resources,
                    self._con,
                    list(self._source_paths) if self._owns_source else [],
                )
            empty_rel = self._con.sql("SELECT NULL AS _bow_empty WHERE 1=0")
            new_rel = empty_rel.query(table_name, sql_query)
            hidden = self._hidden_columns.intersection(new_rel.columns)
            return LazyFrame(
                self._con,
                new_rel,
                self._source_paths,
                owns_source=False,
                parent=self,
                hidden_columns=hidden,
            )
        new_rel = self._rel.query(table_name, sql_query)
        hidden = self._hidden_columns.intersection(new_rel.columns)
        return LazyFrame(
            self._con,
            new_rel,
            self._source_paths,
            owns_source=False,
            parent=self,
            hidden_columns=hidden,
        )

    def limit(self, n: int) -> "LazyFrame":
        if self._rel is None:
            return LazyFrame(
                None,
                None,
                self._source_paths,
                owns_source=False,
                parent=self,
                hidden_columns=self._hidden_columns,
            )
        return LazyFrame(
            self._con,
            self._rel.limit(n),
            self._source_paths,
            owns_source=False,
            parent=self,
            hidden_columns=self._hidden_columns,
        )

    # -- materialization (explicit; this is where memory is spent) ----------

    def _run_compute(self, operation):
        """Run one DuckDB operation under wall-clock and spill-root guards."""
        config = self._stream_config
        config.check_capacity(full=True)
        interrupt = getattr(self._con, "interrupt", None)
        timeout_seconds = self._compute_timeout_seconds
        state_lock = threading.Lock()
        state = {"finished": False, "timed_out": False}

        def interrupt_on_timeout() -> None:
            with state_lock:
                if state["finished"]:
                    return
                state["timed_out"] = True
            try:
                interrupt()
            except Exception:
                logger.debug("LazyFrame: failed to interrupt timed-out compute", exc_info=True)

        timer = None
        if callable(interrupt) and timeout_seconds > 0:
            timer = threading.Timer(timeout_seconds, interrupt_on_timeout)
            timer.daemon = True
            timer.start()

        try:
            try:
                result = operation()
            except BaseException as exc:
                with state_lock:
                    state["finished"] = True
                    timed_out = state["timed_out"]
                if timed_out:
                    raise LazyComputeTimeoutError(timeout_seconds) from exc
                # A concurrent query may have pushed the aggregate root over
                # budget while this operation was running.
                config.check_capacity(full=True)
                message = str(exc).lower()
                if "max_temp_directory_size" in message or (
                    "temp" in message and "size limit" in message
                ):
                    raise ResultTooLargeError(
                        rows=0,
                        byte_estimate=0,
                        limit_desc=(
                            "DuckDB downstream temp spill exceeded its remaining "
                            "aggregate/free-space budget"
                        ),
                    ) from exc
                raise
            else:
                with state_lock:
                    state["finished"] = True
                    timed_out = state["timed_out"]
                if timed_out:
                    raise LazyComputeTimeoutError(timeout_seconds)
                config.check_capacity(full=True)
                return result
        finally:
            if timer is not None:
                timer.cancel()
                if timer.is_alive():
                    timer.join(timeout=0.2)

    @staticmethod
    def _bounded_materialization_limits(max_rows: int, max_bytes: int) -> tuple[int, int]:
        configured_rows, configured_bytes = _materialization_limits()
        return (
            min(max(0, int(max_rows)), configured_rows),
            min(max(0, int(max_bytes)), configured_bytes),
        )

    def _arrow_table_bounded(self, max_rows: int, max_bytes: int, relation=None):
        import pyarrow as pa

        relation = self._rel if relation is None else relation
        if relation is None:
            return pa.table({})
        max_rows, max_bytes = self._bounded_materialization_limits(
            max_rows, max_bytes
        )

        def materialize():
            batch_size = max(
                1,
                min(_env_int("BOW_LAZY_CHUNKSIZE", 50_000), max_rows + 1),
            )
            reader = relation.arrow(batch_size)
            batches = []
            rows = 0
            arrow_bytes = 0
            try:
                schema = reader.schema
                for batch in reader:
                    rows += int(batch.num_rows)
                    arrow_bytes += int(batch.nbytes)
                    if rows > max_rows or arrow_bytes > max_bytes:
                        raise ValueError(
                            "LazyFrame result is too large to materialize "
                            f"(rows={rows}, bytes={arrow_bytes}, "
                            f"max_rows={max_rows}, max_bytes={max_bytes})"
                        )
                    batches.append(batch)
            finally:
                close = getattr(reader, "close", None)
                if callable(close):
                    close()
            table = pa.Table.from_batches(batches, schema=schema)
            hidden = [
                c for c in self._hidden_columns if c in table.column_names
            ]
            return table.drop_columns(hidden) if hidden else table

        return self._run_compute(materialize)

    def _arrow_table(self, relation=None):
        max_rows, max_bytes = _materialization_limits()
        return self._arrow_table_bounded(
            max_rows,
            max_bytes,
            relation=relation,
        )

    @staticmethod
    def _estimate_pandas_materialization_bytes(table) -> int:
        """Conservatively estimate pandas allocation before conversion.

        Arrow's compact buffers can expand sharply into Python objects for
        strings, binary values, decimals, dates, and nested values. The Arrow
        batch guard therefore cannot safely stand in for a pandas-memory guard.
        """
        import pyarrow as pa

        rows = int(table.num_rows)
        estimated = 1024 + len(table.schema) * 256
        for field, column in zip(table.schema, table.columns):
            column_bytes = int(column.nbytes)
            estimated += column_bytes
            data_type = field.type
            if (
                pa.types.is_string(data_type)
                or pa.types.is_large_string(data_type)
                or pa.types.is_binary(data_type)
                or pa.types.is_large_binary(data_type)
            ):
                estimated += rows * 64
            elif pa.types.is_decimal(data_type):
                estimated += rows * 128
            elif pa.types.is_date(data_type) or pa.types.is_time(data_type):
                estimated += rows * 64
            elif (
                pa.types.is_list(data_type)
                or pa.types.is_large_list(data_type)
                or pa.types.is_fixed_size_list(data_type)
                or pa.types.is_struct(data_type)
                or pa.types.is_map(data_type)
                or pa.types.is_union(data_type)
            ):
                estimated += max(rows * 128, column_bytes * 4)
            elif pa.types.is_dictionary(data_type):
                estimated += max(rows * 8, column_bytes * 2)
            elif pa.types.is_null(data_type):
                estimated += rows * 16
            elif pa.types.is_boolean(data_type) and column.null_count:
                estimated += rows * 40
        return int(estimated)

    def to_df(self) -> pd.DataFrame:
        max_rows, max_bytes = _materialization_limits()
        return self.to_df_bounded(max_rows, max_bytes)

    def to_df_bounded(self, max_rows: int, max_bytes: int) -> pd.DataFrame:
        """Materialize this relation in bounded Arrow batches.

        Unlike spill_stats(), this measures the CURRENT relation (including a
        derived aggregation), not its potentially much larger source spill.
        Batches are retained only up to the byte/row budget before conversion to
        pandas, preventing a wide under-row-cap result from causing an OOM.
        """
        if self._rel is None:
            return pd.DataFrame()
        max_rows, max_bytes = self._bounded_materialization_limits(
            max_rows, max_bytes
        )
        table = self._arrow_table_bounded(max_rows, max_bytes)
        estimated_pandas_bytes = self._estimate_pandas_materialization_bytes(table)
        if estimated_pandas_bytes > max_bytes:
            raise ValueError(
                "LazyFrame result is too large to materialize "
                f"(rows={table.num_rows}, bytes={estimated_pandas_bytes}, "
                f"max_rows={max_rows}, max_bytes={max_bytes})"
            )
        # DuckDB's .df() coerces DECIMAL to float64. Arrow -> pandas preserves
        # Python Decimal values and therefore exact financial precision.
        result = table.to_pandas()
        pandas_bytes = int(result.memory_usage(deep=True).sum())
        if pandas_bytes > max_bytes:
            raise ValueError(
                "LazyFrame result is too large to materialize "
                f"(rows={len(result)}, bytes={pandas_bytes}, "
                f"max_rows={max_rows}, max_bytes={max_bytes})"
            )
        hidden = [c for c in self._hidden_columns if c in result.columns]
        return result.drop(columns=hidden) if hidden else result

    def to_arrow_bounded(self, max_rows: int, max_bytes: int):
        if self._rel is None:
            import pyarrow as pa

            return pa.table({})
        return self._arrow_table_bounded(max_rows, max_bytes)

    def to_arrow(self):
        max_rows, max_bytes = _materialization_limits()
        return self.to_arrow_bounded(max_rows, max_bytes)

    def row_count(self) -> int:
        if self._rel is None:
            return 0
        return self._run_compute(
            lambda: int(self._rel.aggregate("count(*) AS n").fetchone()[0])
        )

    def head(self, n: int = 10) -> pd.DataFrame:
        if self._rel is None:
            return pd.DataFrame()
        return self.limit(n).to_df()

    @property
    def columns(self) -> list:
        if self._rel is None:
            return []
        return [c for c in self._rel.columns if c not in self._hidden_columns]

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
        Metadata only; nothing is materialized. Every tracked part must remain
        readable: silently skipping one would undercharge a partial multi-part
        result while still returning the surviving rows."""
        import pyarrow.parquet as pq

        rows = 0
        uncompressed = 0
        disk = 0
        for p in self._source_paths:
            try:
                part_disk = int(p.stat().st_size)
                md = pq.ParquetFile(str(p)).metadata
            except Exception as exc:
                raise RuntimeError(f"Could not read lazy spill metadata for {p}") from exc
            disk += part_disk
            rows += int(md.num_rows)
            uncompressed += sum(
                md.row_group(i).total_byte_size for i in range(md.num_row_groups)
            )
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
        # Exception: a derived schemaless-empty frame that lazily created its OWN
        # connection in sql() registered a connection-only finalizer — run it.
        if not self._owns_source:
            if self._finalizer is not None:
                self._finalizer()
            return
        if self._finalizer is not None:
            self._finalizer()  # runs _release_lazy_resources at most once

    def close_owner(self) -> None:
        """Release the owning frame behind a derived chain.

        Normal derived ``close()`` remains non-owning so callers can keep using
        the parent. The code-execution return boundary, however, is finished
        with the entire chain and uses this method for deterministic cleanup.
        """
        owner = self
        seen = set()
        while owner._parent is not None and id(owner) not in seen:
            seen.add(id(owner))
            owner = owner._parent
        owner.close()

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
    """Keep unknown all-NULL fields as Arrow null.

    Guessing float64 corrupted later booleans into 1.0. The consumer already
    rolls a new part when a later concrete type cannot cast to the first part;
    DuckDB's union-by-name then promotes null + concrete to the concrete type.
    """
    return table


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
        _restrict_file(path)
        config.check_capacity(full=True)
        return _from_parquet_or_cleanup(path, config)
    except BaseException:
        _release_lazy_resources(None, [path])
        raise


def lazy_query_via_sqlalchemy(
    connect_cm: Callable[[], ContextManager],
    sql: str,
    config: Optional[StreamConfig] = None,
) -> LazyFrame:
    """Convenience: stream `sql` to a temp Parquet and return a LazyFrame over it.

    The returned LazyFrame owns the temp file and deletes it on `.close()`.
    """
    return _lazy_via(stream_sqlalchemy_to_parquet, connect_cm, sql, config)


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
    empty_row_sentinel = f"{_EMPTY_ROW_SENTINEL_PREFIX}{uuid.uuid4().hex}"

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
            chunk = _jsonify_nested_cells(_dedupe_columns(chunk))
            rows += len(chunk)
            byte_estimate += int(chunk.memory_usage(deep=True).sum())
            if rows > config.max_rows or byte_estimate > config.max_bytes:
                raise ResultTooLargeError(
                    rows=rows, byte_estimate=byte_estimate, limit_desc=config.limit_desc()
                )
            table = pa.Table.from_pandas(chunk, preserve_index=False)
            if table.num_columns == 0:
                # Parquet cannot encode N rows with zero fields. Preserve those
                # real rows with an internal marker; LazyFrame hides it from
                # public columns/materialization after union-by-name.
                field = pa.field(
                    empty_row_sentinel,
                    pa.bool_(),
                    metadata={_EMPTY_ROW_METADATA_KEY: _EMPTY_ROW_METADATA_VALUE},
                )
                table = pa.Table.from_arrays(
                    [pa.array([True] * len(chunk), type=pa.bool_())],
                    schema=pa.schema([field]),
                )
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
            config.check_capacity(full=True)
        if writer is None:
            # Empty result with no chunk yielded: write a 0-row Parquet that
            # still carries the real column names (when known) so downstream
            # `.sql("SELECT col ...")` keeps working.
            empty = pd.DataFrame(columns=list(columns)) if columns else pd.DataFrame()
            table = _widen_null_columns(pa.Table.from_pandas(empty, preserve_index=False))
            pq.write_table(table, str(path))
            _restrict_file(path)
            config.check_capacity(full=True)
    except BaseException:
        if writer is not None:
            try:
                writer.close()
            except Exception:
                pass
            writer = None
        _release_lazy_resources(None, paths)  # files + private q_ dir
        raise
    finally:
        if writer is not None:
            writer.close()
        _close_quietly(chunks)  # release the source connection promptly
    try:
        # Parquet footer bytes are appended by writer.close(), after the
        # per-batch checks above. Enforce the aggregate cap once more against
        # the completed file(s).
        config.check_capacity(full=True)
    except BaseException:
        _release_lazy_resources(None, paths)
        raise
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
    import re

    import duckdb
    import pyarrow as pa

    inner = _strip_sql_tail(sql)
    # This strategy sometimes executes the source statement bare (SHOW /
    # DESCRIBE cannot be nested under SELECT). Parse before that fallback and
    # allow only read statements; otherwise COPY/ATTACH/DML could write through
    # the source DuckDB connection before a LazyFrame even exists.
    stripped = re.sub(r"\A(?:\s|--[^\n]*(?:\n|$)|/\*.*?\*/)*", "", inner, flags=re.S)
    first = re.match(r"[A-Za-z]+", stripped)
    statements = duckdb.extract_statements(inner)
    if (
        first is None
        or first.group(0).upper()
        not in {"SELECT", "WITH", "SHOW", "DESCRIBE", "DESC"}
        or len(statements) != 1
        or statements[0].type != duckdb.StatementType.SELECT
    ):
        raise ValueError("DuckDB lazy queries accept one read-only query")
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
        _release_lazy_resources(None, paths if isinstance(paths, (list, tuple)) else [paths])
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
        _release_lazy_resources(None, [path])
        raise


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
            config.check_capacity(full=True)
        if writer is None:
            pd.DataFrame().to_parquet(path, index=False)
            _restrict_file(path)
            config.check_capacity(full=True)
    except BaseException:
        if writer is not None:
            try:
                writer.close()
            except Exception:
                pass
            writer = None
        _release_lazy_resources(None, [path])  # file + private q_ dir
        raise
    finally:
        if writer is not None:
            writer.close()
        _close_quietly(arrow_iter)  # release the source stream promptly
    try:
        config.check_capacity(full=True)  # includes footer bytes from close()
    except BaseException:
        _release_lazy_resources(None, [path])
        raise
    return path


def consume_chunks_to_lazyframe(chunks, config: Optional[StreamConfig] = None, columns=None) -> LazyFrame:
    """Spill an iterable of DataFrame chunks to a LazyFrame (e.g. Athena wrangler
    chunksize iterator). `columns`, when known, preserves the schema even for a
    fully-empty iterable."""
    config = config or StreamConfig()
    path = config.new_spill_path()
    paths = _consume_chunks_to_parquet(chunks, path, config, columns=columns)
    return _from_parquet_or_cleanup(paths, config)


def consume_arrow_to_lazyframe(arrow_iter, config: Optional[StreamConfig] = None) -> LazyFrame:
    """Spill an iterable of pyarrow Tables/RecordBatches to a LazyFrame."""
    config = config or StreamConfig()
    path = config.new_spill_path()
    _consume_arrow_to_parquet(arrow_iter, path, config)
    return _from_parquet_or_cleanup(path, config)


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
