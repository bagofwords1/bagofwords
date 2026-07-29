"""Bounded, streaming extraction of a custom query into an encrypted artifact.

A custom query is the one place in the product where a deliberately huge scan
can happen — an admin can write `SELECT * FROM orders` against a 2-billion-row
table. Three independent layers stop that taking the process down:

1. **Refuse before running.** `EXPLAIN` gives an estimated row count and row
   width without executing. Checked at save time and before every refresh.
2. **Never materialize.** Rows stream from a server-side cursor in batches and
   are appended straight into the DuckDB artifact. Peak memory is O(batch),
   independent of result size.
3. **Hard caps with graceful abort.** Row / byte / wall-clock ceilings. On
   breach the partial artifact is discarded and the previous one keeps serving.

This is a NEW extraction path, deliberately not a change to
`DataSourceClient.execute_query` — that returns a full DataFrame by contract and
every existing caller depends on it.
"""

import logging
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import pyarrow as pa
from sqlalchemy import text

from app.data_sources.engine_pool import get_engine
from app.data_sources.fast import artifacts, sql_dialect

logger = logging.getLogger(__name__)

# Defaults. Overridable per-connection via config later; conservative enough
# that a careless SELECT * is refused rather than absorbed.
DEFAULT_MAX_ROWS = 5_000_000
DEFAULT_MAX_BYTES = 2 * 1024 * 1024 * 1024      # 2 GiB estimated source bytes
DEFAULT_MAX_SECONDS = 1800                       # 30 min per refresh
BATCH_ROWS = 50_000

# The preview shown in the authoring modal is always bounded, no matter what the
# admin typed. Never run their raw SQL unbounded from a UI action.
PREVIEW_ROW_LIMIT = 100


class ExtractionRefused(Exception):
    """Raised before execution when the estimated result exceeds a budget."""


class ExtractionAborted(Exception):
    """Raised mid-flight when a hard cap is breached."""


@dataclass
class Estimate:
    rows: Optional[int] = None
    width_bytes: Optional[int] = None
    total_bytes: Optional[int] = None
    supported: bool = True
    note: str = ""


@dataclass
class ExtractResult:
    row_count: int = 0
    columns: list = field(default_factory=list)
    artifact_path: str = ""
    artifact_key: str = ""
    artifact_bytes: int = 0
    elapsed_ms: int = 0


def _dialect(client) -> str:
    """The SQL dialect this client speaks, or "" if we cannot stream it.

    Doubles as the streaming-support test: a client we can name a dialect for
    is one we can drive with a server-side cursor through `_open` below.
    """
    return sql_dialect.dialect_of(client)


@contextmanager
def _open(client):
    """Yield a SQLAlchemy Connection for `client`.

    Most SQL clients' own `connect()` already yields one, and using it is
    strictly better: it is pooled, carries the connection's schema/search_path
    and Kerberos identity, and registers the connection so a stuck extraction
    can be cancelled on the source.

    SQLite is the exception. Its client opens a raw `sqlite3.Connection`
    because its catalog reads use PRAGMA and `row_factory`, so extraction
    addresses the same file through the URI instead. That path gives up
    cancellation, which costs nothing here — a SQLite database is a local file
    with no server-side query to stop.
    """
    if not getattr(client, "EXTRACTION_VIA_URI", False):
        with client.connect() as conn:
            yield conn
        return

    uri = next(
        (getattr(client, attr) for attr, _ in sql_dialect.URI_ATTR_TO_DIALECT
         if getattr(client, attr, None)),
        None,
    )
    if not uri:
        raise RuntimeError("This connection cannot be read for extraction")
    with get_engine(uri).connect() as conn:
        yield conn


def estimate(client, sql: str) -> Estimate:
    """Estimated rows/bytes for `sql` WITHOUT executing it.

    Every supported dialect can be asked what a query will cost, but no two
    agree on how: Postgres has EXPLAIN (FORMAT JSON), MySQL a row per table,
    SQL Server SHOWPLAN_XML, Oracle a write into PLAN_TABLE. `sql_dialect`
    holds one explainer per dialect; this picks the right one.

    A failure here is not fatal — it downgrades to `supported=False` and the
    caller falls back to the hard caps. But it downgrades *silently*, so the
    note records which dialect was tried and why it gave up.
    """
    dialect = _dialect(client)
    explainer = sql_dialect.EXPLAINERS.get(dialect)
    if explainer is None:
        return Estimate(
            supported=False,
            note=f"no cost estimator for this client ({dialect or 'unknown dialect'})",
        )

    try:
        with _open(client) as conn:
            rows, width, note = explainer(conn, sql)
    except Exception as e:
        return Estimate(supported=False, note=f"{dialect} EXPLAIN failed: {e}")

    if rows is None:
        return Estimate(supported=False, note=note or f"{dialect} plan had no estimate")
    total = rows * max(width or 0, 1) if width else None
    return Estimate(rows=rows, width_bytes=width, total_bytes=total, note=note)


def check_budget(
    est: Estimate,
    max_rows: int = DEFAULT_MAX_ROWS,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> None:
    """Raise ExtractionRefused when an estimate blows a budget."""
    if not est.supported:
        return
    if est.rows and est.rows > max_rows:
        raise ExtractionRefused(
            f"Estimated {est.rows:,} rows exceeds the {max_rows:,}-row limit for a "
            f"custom query. Narrow the query with a WHERE clause or fewer columns."
        )
    if est.total_bytes and est.total_bytes > max_bytes:
        raise ExtractionRefused(
            f"Estimated {est.total_bytes / (1024**3):.1f} GB exceeds the "
            f"{max_bytes / (1024**3):.1f} GB limit for a custom query. "
            f"Narrow the query with a WHERE clause or fewer columns."
        )


def preview(client, sql: str, limit: int = PREVIEW_ROW_LIMIT) -> tuple[list, list]:
    """Run `sql` and return at most `limit` rows. Returns (columns, rows).

    **The admin's SQL is executed exactly as written.** The obvious
    implementation — wrap it in `SELECT * FROM (…) LIMIT n` — is wrong in ways
    that are invisible until someone trusts the preview:

      * MySQL and MariaDB are free to discard a derived table's `ORDER BY`.
        A preview of `… ORDER BY amount DESC` showed the three *cheapest*
        orders, not the three most expensive. Verified against MariaDB 10.11.
      * SQL Server rejects `ORDER BY` inside a derived table outright.
      * A query selecting two columns of the same name (`a.id, b.id`) is legal
        on its own and a duplicate-column error once wrapped.

    So the bound is applied to the *fetch* instead, which no dialect can
    reinterpret. To stop the server continuing to produce rows nobody will
    read, the query is then cancelled through the same driver-level path a
    timeout uses — a preview of a huge table costs the source the work done in
    the moment it takes to hand back 100 rows, not the whole scan.
    """
    dialect = _dialect(client)

    if not dialect:
        # No streaming path for this client: its execute_query returns a whole
        # DataFrame by contract, so the only bound available is in the SQL.
        # Weaker and subject to the caveats above, but these client types are
        # not accelerable today (see ACCELERABLE_TYPES).
        bounded, _ = sql_dialect.bounded_sql(sql, limit, dialect)
        df = client.execute_query(bounded)
        cols = [{"name": c, "dtype": str(df[c].dtype)} for c in df.columns]
        return cols, df.head(limit).astype(object).where(df.notna(), None).values.tolist()

    with _open(client) as conn:
        result = conn.execution_options(stream_results=True).execute(
            text(sql_dialect.strip_trailing_semicolon(sql))
        )
        colnames = list(result.keys())
        rows = [list(r) for r in result.fetchmany(limit)]
        if len(rows) == limit:
            # There may be more. Stop the source rather than draining the rest
            # of the result set down the wire (which is what closing an
            # unbuffered MySQL cursor would otherwise do).
            _abandon(client, result, conn)
    return [{"name": c, "dtype": None} for c in colnames], rows


def _abandon(client, result, conn) -> None:
    """Stop a partially-read query without reading the rest of it.

    Cancellation is best effort; whether or not it lands, the connection is
    invalidated rather than returned to the pool, because a pooled connection
    with an unread result set on it is worse than the cost of a reconnect.
    """
    from app.data_sources import query_cancellation

    try:
        outcome = query_cancellation.cancel_thread(client, threading.get_ident())
        logger.debug("Preview cancelled after %s rows: %s", PREVIEW_ROW_LIMIT, outcome)
    except Exception:
        logger.debug("Preview cancellation failed", exc_info=True)
    for step in (result.close, conn.invalidate):
        try:
            step()
        except Exception:
            pass


def _arrow_type_name(t: pa.DataType) -> str:
    return str(t)


def extract_to_artifact(
    client,
    sql: str,
    relation_name: str,
    connection_id: str,
    *,
    max_rows: int = DEFAULT_MAX_ROWS,
    max_bytes: int = DEFAULT_MAX_BYTES,
    max_seconds: int = DEFAULT_MAX_SECONDS,
) -> ExtractResult:
    """Stream `sql` into a fresh encrypted DuckDB artifact.

    Writes to a `.tmp` sibling and atomically renames on success, so a crashed
    or aborted refresh can never leave a half-written artifact in place. The
    caller keeps serving the previous artifact until the swap lands.
    """
    started = time.monotonic()
    dialect = _dialect(client)
    key = artifacts.new_artifact_key()
    final_path = artifacts.new_artifact_path(connection_id)
    tmp_path = final_path.with_suffix(".tmp")

    row_count = 0
    columns: list = []
    con = None

    def _elapsed_guard():
        if time.monotonic() - started > max_seconds:
            raise ExtractionAborted(
                f"Refresh exceeded the {max_seconds}s limit and was aborted; "
                f"the previous data is still being served."
            )

    try:
        con = artifacts.connect_encrypted(tmp_path, key)
        safe_rel = relation_name.replace('"', '""')

        if dialect:
            # --- streaming path -------------------------------------------
            # `_open` prefers the client's own pooled connect(); see its
            # docstring for why SQLite goes a different way.
            with _open(client) as conn:
                result = conn.execution_options(
                    stream_results=True, yield_per=BATCH_ROWS
                ).execute(text(sql))
                colnames = list(result.keys())
                first = True
                while True:
                    _elapsed_guard()
                    batch = result.fetchmany(BATCH_ROWS)
                    if not batch:
                        break
                    row_count += len(batch)
                    if row_count > max_rows:
                        raise ExtractionAborted(
                            f"Query returned more than the {max_rows:,}-row limit "
                            f"for a custom query and was aborted."
                        )
                    tbl = pa.Table.from_pydict(
                        {
                            name: [r[i] for r in batch]
                            for i, name in enumerate(colnames)
                        }
                    )
                    con.register("bow_batch", tbl)
                    if first:
                        con.execute(
                            f'CREATE TABLE "{safe_rel}" AS SELECT * FROM bow_batch'
                        )
                        columns = [
                            {"name": f.name, "dtype": _arrow_type_name(f.type)}
                            for f in tbl.schema
                        ]
                        first = False
                    else:
                        con.execute(
                            f'INSERT INTO "{safe_rel}" SELECT * FROM bow_batch'
                        )
                    con.unregister("bow_batch")

                    if artifacts.artifact_size(str(tmp_path)) > max_bytes:
                        raise ExtractionAborted(
                            f"Artifact exceeded the "
                            f"{max_bytes / (1024**3):.1f} GB limit and was aborted."
                        )

                if first:
                    # Zero rows: still create the relation so the shape exists.
                    empty = pa.Table.from_pydict({c: [] for c in colnames})
                    con.register("bow_batch", empty)
                    con.execute(
                        f'CREATE TABLE "{safe_rel}" AS SELECT * FROM bow_batch'
                    )
                    columns = [
                        {"name": f.name, "dtype": _arrow_type_name(f.type)}
                        for f in empty.schema
                    ]
                    con.unregister("bow_batch")
        else:
            # --- fallback for clients we cannot stream ----------------------
            # Bounded by an injected row limit so a non-streaming client still
            # cannot pull an unbounded result into memory. Unreachable for the
            # dialects in ACCELERABLE_TYPES, all of which stream.
            bounded, _ = sql_dialect.bounded_sql(sql, max_rows, dialect)
            df = client.execute_query(bounded)
            row_count = len(df)
            tbl = pa.Table.from_pandas(df, preserve_index=False)
            con.register("bow_batch", tbl)
            con.execute(f'CREATE TABLE "{safe_rel}" AS SELECT * FROM bow_batch')
            con.unregister("bow_batch")
            columns = [
                {"name": f.name, "dtype": _arrow_type_name(f.type)}
                for f in tbl.schema
            ]

        con.close()
        con = None
        tmp_path.replace(final_path)

        return ExtractResult(
            row_count=row_count,
            columns=columns,
            artifact_path=str(final_path),
            artifact_key=key,
            artifact_bytes=artifacts.artifact_size(str(final_path)),
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )
    except BaseException:
        if con is not None:
            try:
                con.close()
            except Exception:
                pass
        artifacts.delete_artifact(str(tmp_path))
        raise
