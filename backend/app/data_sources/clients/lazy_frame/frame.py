"""The LazyFrame handle: an out-of-core view over spilled Parquet via DuckDB."""

from __future__ import annotations

import logging
import threading
import weakref
from contextlib import suppress
from pathlib import Path

import pandas as pd

from .config import StreamConfig, _env_float, _env_int, _materialization_limits
from .duckdb_session import _open_duckdb
from .errors import LazyComputeTimeoutError, ResultTooLargeError
from .sql_guard import ensure_single_read_statement
from .storage import _release_lazy_resources

logger = logging.getLogger(__name__)

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
        parent: LazyFrame | None = None,
        hidden_columns=None,
        stream_config: StreamConfig | None = None,
        compute_timeout_seconds: float | None = None,
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
    def from_parquet(cls, path, owns_source: bool = True) -> LazyFrame:
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
        stream_config: StreamConfig | None = None,
    ) -> LazyFrame:
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

    def sql(self, sql_query: str, table_name: str = "data") -> LazyFrame:
        """Run SQL against this result, referring to it as `table_name`.

        e.g. lf.sql("SELECT region, SUM(amount) FROM data GROUP BY region")
        """
        # This API is directly exposed to generated code. Directory confinement
        # limits WHERE a query can write, but DuckDB COPY can still fill that
        # private directory outside every spill budget. Require exactly one
        # read-only SELECT/WITH statement at this boundary as defense in depth.
        ensure_single_read_statement(
            sql_query,
            allowed_keywords={"SELECT", "WITH"},
            error_message="LazyFrame.sql accepts one read-only SELECT/WITH query",
        )

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

    def limit(self, n: int) -> LazyFrame:
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
        # pandas' RangeIndex and manager metadata are small, but non-zero.
        # Keep this proportional so a one-row numeric aggregate is not rejected
        # solely because of an arbitrary fixed floor.
        estimated = 128 + len(table.schema) * 64
        for field, column in zip(table.schema, table.columns, strict=True):
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
            with suppress(Exception):
                total += int(p.stat().st_size)
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

    def __enter__(self) -> LazyFrame:
        return self

    def __exit__(self, *exc) -> None:
        self.close()
