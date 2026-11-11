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
    custom table anyone would write.

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
            emitted = False
            while True:
                batch = result.fetchmany(batch_rows)
                if not batch:
                    break
                emitted = True
                # By position, so two columns sharing a name are both kept.
                yield rows_to_arrow(colnames, batch)
            if not emitted:
                # Zero rows: still emit the shape so the relation exists.
                yield rows_to_arrow(colnames, [])

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
