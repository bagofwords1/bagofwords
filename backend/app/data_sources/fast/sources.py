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


def declared_decimals(description) -> list:
    """The (precision, scale) a DB-API description declares for each column, or None.

    The list is aligned with the description by position, not keyed by name:
    SQLAlchemy folds the names it reports (Oracle and Snowflake return
    `AMOUNT`, `result.keys()` says `amount`), and a query may select two
    columns of the same name.

    Without this, a decimal's Arrow type is inferred from the values in each
    batch: `numeric(12,2)` holding nothing over 99,999.99 becomes
    decimal(7,2), the artifact's column is created that narrow, and the first
    later batch with a larger value fails the whole refresh. The declared type
    is the same for every batch and every refresh, including one with no rows
    or only NULLs.

    Only a usable declaration counts — what DuckDB can store, scale within
    precision. Drivers report "undeclared" in their own ways (psycopg2 gives
    65535 for a bare NUMERIC, oracledb 0/-127, some give None), and all of them
    fall outside that range; such a column keeps the inferred type, and the
    extractor widens it if a later batch needs more room. A driver may also
    over-report (MySQL's precision is the display length, so DECIMAL(12,2) can
    arrive as 14): the column is a little wider than declared, never narrower.
    """
    out: list = []
    for d in description or ():
        try:
            precision, scale = d[4], d[5]
        except (IndexError, TypeError, KeyError):
            out.append(None)
            continue
        usable = (
            isinstance(precision, int) and isinstance(scale, int)
            and not isinstance(precision, bool) and not isinstance(scale, bool)
            and 0 < precision <= DUCKDB_MAX_DECIMAL_PRECISION and 0 <= scale <= precision
        )
        out.append((precision, scale) if usable else None)
    return out


def _array(name: str, values: list, declared):
    """One column's Arrow array: the declared decimal type when the values fit it."""
    import pyarrow as pa

    if declared is not None:
        try:
            return pa.array(values, type=pa.decimal128(*declared))
        except (pa.ArrowInvalid, pa.ArrowTypeError):
            # A driver under-reported the declaration (or returned floats).
            # Keep the exact inferred type; the extractor widens the column
            # to hold both.
            logger.info("extraction.decimal_declaration_unused",
                        extra={"column": name, "declared": f"decimal128{declared}"})
    try:
        return pa.array(values)
    except pa.ArrowInvalid:
        # Oracle's bare NUMBER comes back as int when whole and Decimal
        # otherwise, and Arrow sizes a decimal from the first one it sees.
        # Integers are exact decimals, so build the column as one.
        from decimal import Decimal

        if not any(isinstance(v, Decimal) for v in values):
            raise
        return pa.array([Decimal(v) if type(v) is int else v for v in values])


def _unique_names(names: list) -> list:
    """`names` with repeats renamed `name_1`, `name_2`, … as DuckDB would.

    DuckDB compares names case-insensitively and cannot read an Arrow table
    with an exact repeat at all, yet `SELECT a.id, b.id` is a legal query.
    Renaming here keeps both columns and makes the names this table reports
    the names the artifact stores.
    """
    originals = {str(n).lower() for n in names}
    taken: set = set()
    out = []
    for n in map(str, names):
        if n.lower() in taken:
            k = 1
            while f"{n}_{k}".lower() in taken or f"{n}_{k}".lower() in originals:
                k += 1
            n = f"{n}_{k}"
        taken.add(n.lower())
        out.append(n)
    return out


def arrow_table(columns, decimals=None):
    """Build a pyarrow Table the artifact can actually accept.

    ``columns`` is a {name: values} dict, or a sequence of (name, values)
    pairs when names may repeat (see `_unique_names`). ``decimals`` (see
    `declared_decimals`) is aligned with it by position and gives a column the
    decimal type its source declares instead of the narrowest one this batch's
    values fit. A batch whose values do not fit the declaration keeps its
    inferred type rather than being rounded or refused.

    Oracle is the reason this exists. A NUMBER declared without precision — which
    is what `AVG(x)` and most arithmetic produce — comes back as a decimal of
    precision 39 or more, and DuckDB cannot store it. Extraction died on
    `SELECT AVG(amount) ... GROUP BY ...`, which is close to the most ordinary
    custom table anyone would write.

    Such columns are widened to float64. That trades exactness for the ability
    to store the value at all, and it only happens for values that had no
    representable decimal form in the first place — a column declared
    NUMBER(12,2) keeps its exact decimal type and is untouched.
    """
    import pyarrow as pa

    pairs = list(columns.items()) if isinstance(columns, dict) else list(columns)
    declared = (list(decimals or ()) + [None] * len(pairs))[:len(pairs)]
    names = _unique_names([name for name, _ in pairs])
    tbl = pa.Table.from_arrays(
        [values if isinstance(values, (pa.Array, pa.ChunkedArray)) else _array(n, values, dec)
         for n, (_, values), dec in zip(names, pairs, declared, strict=True)],
        names=names,
    )
    over = [
        i for i, f in enumerate(tbl.schema)
        if pa.types.is_decimal(f.type) and f.type.precision > DUCKDB_MAX_DECIMAL_PRECISION
    ]
    if not over:
        return tbl
    logger.info("extraction.decimal_widened",
                extra={"columns": [tbl.schema.field(i).name for i in over]})
    for i in over:
        tbl = tbl.set_column(i, pa.field(tbl.schema.field(i).name, pa.float64()),
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
            # The DB-API description, for the declared types. Its names are
            # the driver's, not necessarily `colnames` (see declared_decimals);
            # its order is the same.
            description = result.cursor.description if result.cursor is not None else None
            if description is None or len(description) != len(colnames):
                logger.warning("extraction.no_column_declarations",
                               extra={"dialect": self.dialect})
                description = None
            decimals = declared_decimals(description)
            emitted = False
            while True:
                batch = result.fetchmany(batch_rows)
                if not batch:
                    break
                emitted = True
                yield arrow_table(
                    [(name, [r[i] for r in batch]) for i, name in enumerate(colnames)],
                    decimals,
                )
            if not emitted:
                # Zero rows: still emit the shape so the relation exists.
                yield arrow_table([(c, []) for c in colnames], decimals)

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
