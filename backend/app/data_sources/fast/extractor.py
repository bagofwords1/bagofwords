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
import time
from dataclasses import dataclass, field
from typing import Optional

import pyarrow as pa

from app.data_sources.fast import artifacts, sql_dialect

logger = logging.getLogger(__name__)

# Defaults. Overridable per-connection via config later; conservative enough
# that a careless SELECT * is refused rather than absorbed.
DEFAULT_MAX_ROWS = 5_000_000
DEFAULT_MAX_BYTES = 2 * 1024 * 1024 * 1024      # 2 GiB estimated source bytes
DEFAULT_MAX_SECONDS = 1800                       # 30 min per refresh
# Bytes the SOURCE may read per refresh, where it can tell us in advance
# (Snowflake, BigQuery). Deliberately much larger than the artifact ceiling: a
# rollup that scans 50 GB to produce 5,000 rows is the best possible custom
# query, not one to refuse. This exists to catch the refresh that quietly costs
# real money every hour.
DEFAULT_MAX_SCAN_BYTES = 100 * 1024 * 1024 * 1024   # 100 GiB
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
    """What a source can tell us about a query before it runs.

    Two different quantities live here and they must not be confused:

    * ``rows`` / ``width_bytes`` / ``total_bytes`` describe the RESULT — how
      big the artifact will be. Postgres and MySQL report this (planner row
      counts and widths), and it is what the row/byte caps guard.
    * ``scan_bytes`` describes the COST — how much data the source must read
      to answer. Snowflake reports it as ``bytesAssigned`` and BigQuery as
      ``total_bytes_processed``; it is what those warehouses bill for.

    They diverge hardest on exactly the queries most worth caching: an
    aggregate over a billion rows scans gigabytes and returns twelve rows.
    Treating scan bytes as result size would refuse that query, which would be
    precisely backwards — a cheap artifact standing in for an expensive scan
    is the whole point.
    """

    rows: Optional[int] = None
    width_bytes: Optional[int] = None
    total_bytes: Optional[int] = None
    # Bytes the SOURCE reads to answer, when it will tell us. Not the result
    # size; see above.
    scan_bytes: Optional[int] = None
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


def _source(client):
    """The extraction source for `client`, or None if it cannot be materialized."""
    from app.data_sources.fast.sources import source_for

    return source_for(client)


def estimate(client, sql: str) -> Estimate:
    """What `sql` will cost / return, WITHOUT executing it.

    Delegated to the source, because no two answer the same question: Postgres
    and MySQL report result size from the planner, Snowflake and BigQuery report
    scan cost (BigQuery exactly, for free, via a dry run), and SQLite reports
    nothing at all.

    A failure here is not fatal — it downgrades to `supported=False` and the
    caller falls back to the hard caps. But it downgrades *silently*, so the
    note records what was tried and why it gave up.
    """
    src = _source(client)
    if src is None:
        return Estimate(supported=False, note="this connection cannot be materialized")
    try:
        return src.estimate(sql)
    except Exception as e:      # a source must not break the save path
        return Estimate(supported=False, note=f"estimate failed: {e}")


def check_budget(
    est: Estimate,
    max_rows: int = DEFAULT_MAX_ROWS,
    max_bytes: int = DEFAULT_MAX_BYTES,
    max_scan_bytes: int = DEFAULT_MAX_SCAN_BYTES,
) -> None:
    """Raise ExtractionRefused when an estimate blows a budget.

    Three separate ceilings, because they fail in different ways: too many
    rows or too many result bytes means the artifact will not fit, while too
    many scanned bytes means the refresh is expensive to run — on a
    consumption-priced warehouse, expensive in money, every time it repeats.
    """
    if not est.supported:
        return
    if est.scan_bytes and est.scan_bytes > max_scan_bytes:
        raise ExtractionRefused(
            f"This query would scan {est.scan_bytes / (1024**3):.1f} GB at the source "
            f"on every refresh, over the {max_scan_bytes / (1024**3):.1f} GB limit. "
            f"Narrow it with a WHERE clause, select fewer columns, or refresh less often."
        )
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
    reinterpret, and the source stops the query once it has enough.
    """
    src = _source(client)
    if src is None:
        # No extraction source: the client's execute_query returns a whole
        # DataFrame by contract, so the only bound available is in the SQL.
        # Weaker and subject to the caveats above, but these client types are
        # not accelerable today (see ACCELERABLE_TYPES).
        bounded, _ = sql_dialect.bounded_sql(sql, limit, "")
        df = client.execute_query(bounded)
        cols = [{"name": c, "dtype": str(df[c].dtype)} for c in df.columns]
        return cols, df.head(limit).astype(object).where(df.notna(), None).values.tolist()
    return src.preview(sql, limit)


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
    src = _source(client)
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

        if src is not None:
            # --- streaming path -------------------------------------------
            # Arrow batches from the source, appended straight into DuckDB.
            # Peak memory is O(batch), independent of result size, and a
            # source whose API is already Arrow (BigQuery) pays no conversion.
            first = True
            batches = src.stream_batches(sql, BATCH_ROWS)
            try:
                for tbl in batches:
                    _elapsed_guard()
                    row_count += tbl.num_rows
                    if row_count > max_rows:
                        raise ExtractionAborted(
                            f"Query returned more than the {max_rows:,}-row limit "
                            f"for a custom query and was aborted."
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
                    elif tbl.num_rows:
                        con.execute(
                            f'INSERT INTO "{safe_rel}" SELECT * FROM bow_batch'
                        )
                    con.unregister("bow_batch")

                    if artifacts.artifact_size(str(tmp_path)) > max_bytes:
                        raise ExtractionAborted(
                            f"Artifact exceeded the "
                            f"{max_bytes / (1024**3):.1f} GB limit and was aborted."
                        )
            finally:
                # Closing the generator runs its cleanup — for BigQuery that
                # cancels the still-running (still-billing) job. Without this,
                # aborting on a cap would leave the source working for nobody.
                batches.close()
        else:
            # --- fallback for clients we cannot stream ----------------------
            # Bounded by an injected row limit so a non-streaming client still
            # cannot pull an unbounded result into memory. Unreachable for the
            # dialects in ACCELERABLE_TYPES, all of which stream.
            bounded, _ = sql_dialect.bounded_sql(sql, max_rows, "")
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
