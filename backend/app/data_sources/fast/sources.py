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
from typing import Iterator, List, NamedTuple, Optional, Protocol, Tuple

logger = logging.getLogger(__name__)


# DuckDB's widest DECIMAL is 38 digits. Arrow will happily infer decimal256 past
# that, and the mismatch surfaces as an opaque failure at register() time:
#   Not implemented Error: Unsupported Internal Arrow Type for Decimal d:39,36,256
DUCKDB_MAX_DECIMAL_PRECISION = 38


class Declared(NamedTuple):
    """A column's declared decimal type, from the DB-API description."""

    precision: int
    scale: int
    # The driver's type code is Python's `Decimal` (pyodbc), so the column is
    # a decimal even when this batch has no value to show it.
    decimal_typed: bool = False


def declared_decimals(description) -> list:
    """The `Declared` precision and scale of each column, or None.

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

    A scale of 0 does not make a column a decimal: PyMySQL and pyodbc report
    (20, 0) and (19, 0) for BIGINT, and Snowflake and Oracle declare their
    integers as NUMBER(38,0). `_array` keeps those integers (see there).
    """
    from decimal import Decimal

    out: list = []
    for d in description or ():
        try:
            type_code, precision, scale = d[1], d[4], d[5]
        except (IndexError, TypeError, KeyError):
            out.append(None)
            continue
        usable = (
            isinstance(precision, int) and isinstance(scale, int)
            and not isinstance(precision, bool) and not isinstance(scale, bool)
            and 0 < precision <= DUCKDB_MAX_DECIMAL_PRECISION and 0 <= scale <= precision
        )
        out.append(Declared(precision, scale, type_code is Decimal) if usable else None)
    return out


def _integers(values: list):
    """`values` as an int64 array when every one is a Python int (NULLs aside)
    and at least one is present; None otherwise, or when one is past int64."""
    import pyarrow as pa

    present = [v for v in values if v is not None]
    if not present or not all(type(v) is int for v in present):
        return None
    try:
        return pa.array(values, type=pa.int64())
    except (pa.ArrowInvalid, OverflowError):
        return None


def _array(name: str, values: list, declared):
    """One column's Arrow array: the declared decimal type when the column is one
    and its values fit it.

    A declaration with scale 0 is also what drivers report for integer
    columns, so it is used only when the column shows a decimal: a `Decimal`
    value, or a `Decimal` type code. Integers stay int64, as they were before
    declarations were read — a DECIMAL column reaches a query as float64
    (DuckDB's `.df()`), and an id past 2**53 would come back rounded. Integers
    past int64 take the declared type, which holds them exactly. A column with
    no values in this batch keeps the inferred type (NULL), which the extractor
    widens to whatever arrives later.
    """
    from decimal import Decimal

    import pyarrow as pa

    if declared is not None and declared.scale == 0 and not declared.decimal_typed \
            and not any(isinstance(v, Decimal) for v in values):
        ints = _integers(values)
        if ints is not None or all(v is None for v in values):
            return ints if ints is not None else pa.array(values)
        if not all(type(v) is int for v in values if v is not None):
            declared = None   # floats, text, dates: not this declaration's business
    if declared is not None:
        try:
            return pa.array(values, type=pa.decimal128(declared.precision, declared.scale))
        except (pa.ArrowInvalid, pa.ArrowTypeError):
            # A driver under-reported the declaration (or returned floats).
            # Keep the exact inferred type; the extractor widens the column
            # to hold both.
            logger.info("extraction.decimal_declaration_unused",
                        extra={"column": name,
                               "declared": f"decimal128({declared.precision}, {declared.scale})"})
    try:
        return pa.array(values)
    except (pa.ArrowInvalid, OverflowError):
        # Oracle's bare NUMBER comes back as int when whole and Decimal
        # otherwise, and Arrow sizes a decimal from the first one it sees; an
        # int past int64 does not fit Arrow's integers at all. Integers are
        # exact decimals, so build the column as one.
        if not any(isinstance(v, Decimal) or (type(v) is int and not -2**63 <= v < 2**63)
                   for v in values):
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
    if dialect:
        return SqlAlchemySource(client, dialect)

    from app.data_sources.fast import incremental

    declared = incremental.json_window_of(client)
    if declared is not None:
        return RequestSource(client, declared)
    if callable(getattr(client, "execute_query", None)):
        return QuerySource(client)
    return None


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
            from app.data_sources.fast.normalize import rows_to_arrow

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
                # By position, so two columns sharing a name are both kept;
                # with the declared decimals, so every batch gets one type.
                yield rows_to_arrow(colnames, batch, decimals)
            if not emitted:
                # Zero rows: still emit the shape so the relation exists.
                yield rows_to_arrow(colnames, [], decimals)

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


# --------------------------------------------------------------------------
# Connectors that answer a JSON request, not SQL
# --------------------------------------------------------------------------

class QuerySource:
    """Drives any client through the one thing every connector has:
    `execute_query(query)` returning a DataFrame.

    This is what makes every queryable connector cacheable. It knows nothing
    about the query's language, so it cannot bound a preview at the source,
    estimate a cost, or stream: the connector runs the query as its agent
    would, with the connector's own row limits, and the result is stored.
    A connector that deserves better than that gets a source of its own.
    """

    def __init__(self, client):
        self.client = client

    def estimate(self, sql: str):
        from app.data_sources.fast.extractor import Estimate

        return Estimate(supported=False, note="this source cannot estimate a query before running it")

    def preview(self, sql: str, limit: int):
        df = self.client.execute_query(sql).head(limit)
        cols = [{"name": str(c), "dtype": str(df[c].dtype)} for c in df.columns]
        return cols, df.astype(object).where(df.notna(), None).values.tolist()

    def stream_batches(self, sql: str, batch_rows: int):
        from app.data_sources.fast.normalize import rows_to_arrow

        df = self.client.execute_query(sql)
        columns = [str(c) for c in df.columns]
        if not columns:
            raise RuntimeError(
                "This query returned no rows and no columns, so the table has no "
                "shape to be created with."
            )
        rows = df.astype(object).where(df.notna(), None).values.tolist()
        if not rows:
            yield rows_to_arrow(columns, [])
            return
        for i in range(0, len(rows), batch_rows):
            yield rows_to_arrow(columns, rows[i:i + batch_rows])


class RequestSource:
    """Drives a client whose "query" is a JSON request with a time window.

    Monitoring and metrics APIs do not speak SQL: the query is an object
    naming what to fetch and from when to when, and `execute_query` answers
    with a DataFrame. A client opts in by declaring where its request takes
    that window (`INCREMENTAL_WINDOW`), which is everything needed to
    materialize it — and to refresh it incrementally.

    Such APIs also cap what one request returns, and truncate without saying
    so. A response at the declared ceiling is therefore never trusted: its
    window is halved and both halves fetched, until every response is under
    the ceiling. Reaching the end of the batches means the result is whole.
    """

    MAX_REQUESTS = 2000

    def __init__(self, client, declared: dict):
        self.client = client
        self.declared = declared

    def estimate(self, sql: str):
        from app.data_sources.fast.extractor import Estimate

        return Estimate(supported=False, note="this source cannot estimate a request before running it")

    def _body(self, spec: str) -> dict:
        import json

        body = json.loads(spec) if isinstance(spec, str) else dict(spec)
        if not isinstance(body, dict):
            raise ValueError("The query must be a JSON object.")
        return body

    def _run(self, body: dict):
        import json

        return self.client.execute_query(json.dumps(body))

    def preview(self, sql: str, limit: int):
        body = self._body(sql)
        field = self.declared.get("limit")
        if field:
            body[field] = min(int(body.get(field) or limit), limit)
        df = self._run(body).head(limit)
        cols = [{"name": str(c), "dtype": str(df[c].dtype)} for c in df.columns]
        return cols, df.astype(object).where(df.notna(), None).values.tolist()

    def stream_batches(self, sql: str, batch_rows: int):
        from app.data_sources.fast.normalize import rows_to_arrow

        body = self._body(sql)
        ceiling = int(self.declared.get("row_ceiling") or 0)
        limit_field = self.declared.get("limit")
        if ceiling and limit_field:
            body[limit_field] = ceiling
        start, end = self.declared.get("start"), self.declared.get("end")

        pending = [body]
        requests = 0
        columns = None
        emitted = False
        while pending:
            current = pending.pop()
            requests += 1
            if requests > self.MAX_REQUESTS:
                raise RuntimeError(
                    f"Extraction did not converge after {self.MAX_REQUESTS} requests."
                )
            df = self._run(current)
            if columns is None and len(df.columns):
                columns = [str(c) for c in df.columns]
            if ceiling and len(df) >= ceiling:
                from app.data_sources.fast import incremental

                lo, hi = current.get(start), current.get(end)
                mid = _midpoint(lo, hi, incremental.json_bucket(self.declared, current))
                if mid is None:
                    raise RuntimeError(
                        f"This source returns at most {ceiling:,} rows per request and "
                        f"this one is at that limit, with no time window left to split. "
                        f"Narrow the request (fewer objects or metrics) or give it an "
                        f"explicit start and end."
                    )
                # The halves share no instant; processed oldest first. Where
                # the API includes its end bound, the left half stops one
                # unit short of the cut.
                left_end = mid - 1 if self.declared.get("end_inclusive") and isinstance(mid, int) else mid
                pending.append({**current, start: mid})
                pending.append({**current, end: left_end})
                continue
            if not len(df):
                continue
            rows = df.astype(object).where(df.notna(), None).values.tolist()
            for i in range(0, len(rows), batch_rows):
                emitted = True
                yield rows_to_arrow([str(c) for c in df.columns], rows[i:i + batch_rows])
        if not emitted:
            if not columns:
                raise RuntimeError(
                    "This request returned no rows and no columns, so the table has no "
                    "shape to be created with. Widen the time window or check the request."
                )
            # Zero rows: still emit the shape so the relation exists.
            yield rows_to_arrow(columns, [])


def _midpoint(lo, hi, bucket=None):
    """Where to cut a numeric window in two, or None when it cannot be cut.

    When the API rolls points up into buckets, the cut lands on a bucket
    boundary: cutting inside one would return it twice, each half built from
    part of its points.
    """
    if isinstance(lo, bool) or isinstance(hi, bool):
        return None
    if not isinstance(lo, (int, float)) or not isinstance(hi, (int, float)):
        return None
    mid = (lo + hi) // 2 if isinstance(lo, int) and isinstance(hi, int) else (lo + hi) / 2
    if bucket:
        mid = int(mid // bucket * bucket)
    return mid if lo < mid < hi else None
