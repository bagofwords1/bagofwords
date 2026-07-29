"""How the extractor talks to a source it is materializing from.

Everything the extractor needs is three questions:

    estimate(sql)                 what will this cost / how big is it?
    preview(sql, limit)           show me the first n rows
    stream_batches(sql, n)        give me the whole result n rows at a time

For most sources those are answered through SQLAlchemy — a server-side cursor
and a dialect-specific EXPLAIN — and `SqlAlchemySource` below is that default.

BigQuery is the reason this is a protocol rather than a chain of special cases.
It has no SQLAlchemy engine at all, and for every one of the three questions its
native API is not a workaround but a straight improvement:

  * a **dry run** returns the exact bytes the query will scan, for free, without
    executing — better than any planner estimate, which is a guess
  * `to_arrow_iterable()` yields Arrow record batches, which is precisely what
    the extractor turns rows into anyway; going through SQLAlchemy would mean
    Arrow → tuples → Arrow
  * `query_job.cancel()` cancels the actual job server-side

Forcing that through a SQL-shaped interface would have meant losing all three to
keep one abstraction. The protocol is narrow enough that a source implementing
it natively is a page of code, and wide enough that nothing else has to know.
"""

import logging
from typing import Iterator, List, Optional, Protocol, Tuple

logger = logging.getLogger(__name__)


# DuckDB's widest DECIMAL is 38 digits. Arrow will happily infer decimal256 past
# that, and the mismatch surfaces as an opaque failure at register() time:
#   Not implemented Error: Unsupported Internal Arrow Type for Decimal d:39,36,256
DUCKDB_MAX_DECIMAL_PRECISION = 38


def arrow_table(columns: dict):
    """Build a pyarrow Table the artifact can actually accept.

    Oracle is the reason this exists. A NUMBER declared without precision — which
    is what `AVG(x)` and most arithmetic produce — comes back as a decimal of
    precision 39 or more, and DuckDB cannot store it. Extraction died on
    `SELECT AVG(amount) ... GROUP BY ...`, which is close to the most ordinary
    custom query anyone would write.

    Such columns are widened to float64. That trades exactness for the ability
    to store the value at all, and it only happens for values that had no
    representable decimal form in the first place — a column declared
    NUMBER(12,2) keeps its exact decimal type and is untouched.
    """
    import pyarrow as pa

    tbl = pa.Table.from_pydict(columns)
    over = [
        f.name for f in tbl.schema
        if pa.types.is_decimal(f.type) and f.type.precision > DUCKDB_MAX_DECIMAL_PRECISION
    ]
    if not over:
        return tbl
    logger.info("extraction.decimal_widened", extra={"columns": over})
    for name in over:
        i = tbl.schema.get_field_index(name)
        tbl = tbl.set_column(i, pa.field(name, pa.float64()),
                             tbl.column(i).cast(pa.float64()))
    return tbl


class ExtractionSource(Protocol):
    """What a source must answer to be materializable."""

    def estimate(self, sql: str):
        """An `Estimate` for `sql` WITHOUT running it. Never raises."""

    def preview(self, sql: str, limit: int) -> Tuple[List[dict], List[list]]:
        """(columns, rows) — at most `limit` rows, stopping the source early."""

    def stream_batches(self, sql: str, batch_rows: int) -> Iterator["object"]:
        """Yield pyarrow.Table batches covering the whole result."""


def source_for(client) -> Optional[ExtractionSource]:
    """The extraction source for `client`, or None if it cannot be materialized.

    Resolution is by declaration, not by guessing: a client either names its
    native source or speaks a dialect we can drive over SQLAlchemy.
    """
    native = getattr(client, "EXTRACTION_SOURCE", None)
    if native is not None:
        return native(client)

    from app.data_sources.fast import sql_dialect

    dialect = sql_dialect.dialect_of(client)
    if not dialect:
        return None
    return SqlAlchemySource(client, dialect)


# --------------------------------------------------------------------------
# The default: SQLAlchemy + a dialect-specific EXPLAIN
# --------------------------------------------------------------------------

class SqlAlchemySource:
    """Drives any client that can hand over a SQLAlchemy Connection.

    That is most of them directly, and the two that cannot (SQLite, Fabric)
    via `extraction_connect()` — see `_open`.
    """

    def __init__(self, client, dialect: str):
        self.client = client
        self.dialect = dialect

    # -- connection ------------------------------------------------------

    def _open(self):
        """A SQLAlchemy Connection for this client.

        Most clients' own `connect()` already yields one, and using it is
        strictly better: it is pooled, carries the connection's schema or
        search_path and its Kerberos identity, and registers the connection so
        a stuck extraction can be cancelled on the source.

        Two clients cannot. SQLite opens a raw `sqlite3` connection because its
        catalog reads use PRAGMA and `row_factory`; Fabric opens raw pyodbc
        because its Entra token is handed to the driver through `attrs_before`
        and cannot live in a URL. Both provide `extraction_connect()` instead —
        one contract rather than a flag per exception.
        """
        opener = getattr(self.client, "extraction_connect", None)
        if opener is not None:
            return opener()
        return self.client.connect()

    # -- the protocol ----------------------------------------------------

    def estimate(self, sql: str):
        from app.data_sources.fast import sql_dialect
        from app.data_sources.fast.extractor import Estimate

        explainer = sql_dialect.EXPLAINERS.get(self.dialect)
        if explainer is None:
            return Estimate(supported=False,
                            note=f"no cost estimator for {self.dialect or 'this client'}")
        try:
            with self._open() as conn:
                est = explainer(conn, sql)
        except Exception as e:
            return Estimate(supported=False, note=f"{self.dialect} EXPLAIN failed: {e}")
        if est.rows is None and est.scan_bytes is None:
            return Estimate(supported=False,
                            note=est.note or f"{self.dialect} plan had no estimate")
        return est

    def preview(self, sql: str, limit: int):
        import threading

        from sqlalchemy import text

        from app.data_sources.fast import sql_dialect

        with self._open() as conn:
            result = conn.execution_options(stream_results=True).execute(
                text(sql_dialect.strip_trailing_semicolon(sql))
            )
            colnames = list(result.keys())
            rows = [list(r) for r in result.fetchmany(limit)]
            if len(rows) == limit:
                # There may be more. Stop the source rather than draining the
                # rest of the result set down the wire (which is what closing
                # an unbuffered MySQL cursor would otherwise do).
                self._abandon(result, conn, threading.get_ident())
        return [{"name": c, "dtype": None} for c in colnames], rows

    def stream_batches(self, sql: str, batch_rows: int):
        from sqlalchemy import text

        with self._open() as conn:
            result = conn.execution_options(
                stream_results=True, yield_per=batch_rows
            ).execute(text(sql))
            colnames = list(result.keys())
            emitted = False
            while True:
                batch = result.fetchmany(batch_rows)
                if not batch:
                    break
                emitted = True
                yield arrow_table(
                    {name: [r[i] for r in batch] for i, name in enumerate(colnames)}
                )
            if not emitted:
                # Zero rows: still emit the shape so the relation exists.
                yield arrow_table({c: [] for c in colnames})

    # -- helpers ---------------------------------------------------------

    def _abandon(self, result, conn, thread_ident: int) -> None:
        """Stop a partially-read query without reading the rest of it."""
        from app.data_sources import query_cancellation

        try:
            outcome = query_cancellation.cancel_thread(self.client, thread_ident)
            logger.debug("Preview cancelled early: %s", outcome)
        except Exception:
            logger.debug("Preview cancellation failed", exc_info=True)
        for step in (result.close, conn.invalidate):
            try:
                step()
            except Exception:
                pass
