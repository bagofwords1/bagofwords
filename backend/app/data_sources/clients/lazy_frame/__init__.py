"""Out-of-core query results (v2, opt-in) — `execute_query_lazy`.

The default `execute_query` path is unchanged: it runs the query and returns a
full `pandas.DataFrame` held in memory. This package adds a *separate*, opt-in
path that never materializes the whole result:

  1. Stream the source result in chunks (server-side cursor) straight to a
     Parquet file on disk. Peak memory during ingest is one chunk, not the whole
     result. An early byte/row cap aborts oversized scans *before* they OOM.
  2. Return a `LazyFrame` — a thin handle over a DuckDB relation backed by that
     Parquet file. Filtering / aggregation run out-of-core inside DuckDB (which
     spills to disk); only the final, presumably-small result enters RAM, when
     the caller explicitly calls `.to_df()`.

Nothing here changes existing behavior. A client opts in by setting
`_lazy_strategy` (base-class dispatch to a streamer in `streamers`) or
overriding `execute_query_lazy` for bespoke streams; callers opt in by calling
it instead of `execute_query`.

Layout (each layer only imports the ones above it):

  errors          error types shared across the path
  sql_guard       read-only gate for SQL that reaches DuckDB
  storage         SpillStorage backends — where spill bytes live (the seam for
                  future non-local backends, e.g. S3-compatible object stores)
  config          StreamConfig — budgets/policy, applied against a storage
  duckdb_session  hardened DuckDB connections + temp-spill reservations
  frame           the LazyFrame handle
  ingest          chunk/Arrow spill writers, normalizers, cancellation
  streamers       per-driver streamers and the public client entry points
"""

from .config import StreamConfig, _materialization_limits
from .duckdb_session import _open_duckdb
from .errors import (
    LazyComputeTimeoutError,
    QueryAbandonedError,
    ResultTooLargeError,
)
from .frame import LazyFrame
from .ingest import (
    _cancelled,
    _consume_arrow_to_parquet,
    _consume_chunks_to_parquet,
    arrow_safe_cell,
    set_cancel_event,
)
from .storage import (
    _SWEEP_INTERVAL_SECONDS,
    LocalSpillStorage,
    SpillStorage,
    _last_sweep,
    _release_lazy_resources,
)
from .streamers import (
    _strip_sql_tail,
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

# Underscore names are legacy re-exports: tests and a few clients reached for
# these internals when this package was a single module.
__all__ = [
    "_SWEEP_INTERVAL_SECONDS",
    "_cancelled",
    "_consume_arrow_to_parquet",
    "_consume_chunks_to_parquet",
    "_last_sweep",
    "_materialization_limits",
    "_open_duckdb",
    "_release_lazy_resources",
    "_strip_sql_tail",
    "LazyComputeTimeoutError",
    "LazyFrame",
    "LocalSpillStorage",
    "QueryAbandonedError",
    "ResultTooLargeError",
    "SpillStorage",
    "StreamConfig",
    "arrow_safe_cell",
    "consume_arrow_to_lazyframe",
    "consume_chunks_to_lazyframe",
    "consume_row_dicts_to_lazyframe",
    "lazy_from_dataframe",
    "lazy_query_via_dbapi_cursor",
    "lazy_query_via_dbapi_readsql",
    "lazy_query_via_duckdb",
    "lazy_query_via_sqlalchemy",
    "set_cancel_event",
    "stream_dbapi_cursor_to_parquet",
    "stream_dbapi_readsql_to_parquet",
    "stream_duckdb_to_parquet",
    "stream_sqlalchemy_to_parquet",
]
