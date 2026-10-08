"""Materializing from ClickHouse through clickhouse-connect rather than SQL.

ClickHouse has no SQLAlchemy engine here — the client speaks HTTP through
clickhouse-connect — so it gets a native source, like BigQuery. On each of the
extractor's three questions the native API is the better answer anyway:

**Estimating.** `EXPLAIN ESTIMATE` reports, per table, how many rows and parts
the query will read, without running it. It reports no bytes, so the scan size
is derived from `system.parts`: rows read × the table's compressed bytes per
row. That counts every column, so it is an upper bound on what a columnar read
of a few columns actually costs — the right direction for a ceiling.

Why scan cost matters on ClickHouse even though Cloud bills compute-hours, not
bytes: every refresh wakes an idle service. The estimate is what tells an admin
a schedule is expensive before it repeats.

**Streaming.** `query_arrow_stream` yields Arrow record batches — exactly what
the extractor appends to DuckDB, with no tuple round-trip.

**Cancelling.** Every extraction query carries its own `query_id`, so stopping
early (a cap breached, the caller gone) can `KILL QUERY` it on the server
instead of leaving it to run for nobody. `max_execution_time` is set too, as a
server-side ceiling that holds even if the kill never arrives.

Two things learned against a live server rather than a fake:

* The connection's shared client carries a session id, and ClickHouse allows
  one in-flight query per session. A stream abandoned mid-read keeps that
  session locked (`SESSION_IS_LOCKED`) and the kill — or the agent's next
  query — is refused. Extraction therefore runs on its own session-less client.
* Arrow has no enum type, and ClickHouse emits `Enum8/16` as their raw integer
  codes: a `status` column materialized as 1/2/3 instead of 'new'/'paid'.
  Enum columns are found with `DESCRIBE` and cast to String on the way out.
"""

import logging
import re
import uuid
from collections.abc import Iterator

logger = logging.getLogger(__name__)

# Server-side wall clock for any single extraction query. The extractor's own
# elapsed guard aborts first in the normal case; this is the backstop that
# holds even if the client process dies mid-stream.
DEFAULT_MAX_EXECUTION_SECONDS = 1800

_ENUM_RE = re.compile(r"^(Nullable\()?(LowCardinality\()?Enum(8|16)\(")


class ClickHouseSource:
    """Extraction over clickhouse-connect."""

    def __init__(self, client):
        self.client = client
        self._ch = None

    # -- connection ------------------------------------------------------

    def _conn(self):
        """A session-less clickhouse-connect client for extraction.

        Built from the BOW client's own connection fields so database, TLS and
        credentials match; see the module docstring for why it is not the
        shared client.
        """
        if self._ch is None:
            import clickhouse_connect

            c = self.client
            kw = {
                "host": c.host,
                "port": c.port,
                "username": c.user,
                "password": c.password,
                "secure": c.secure,
                "autogenerate_session_id": False,
            }
            db = getattr(c, "_primary_database", None)
            if db:
                kw["database"] = db
            self._ch = clickhouse_connect.get_client(**kw)
        return self._ch

    # -- the protocol ----------------------------------------------------

    def estimate(self, sql: str):
        """Rows read from `EXPLAIN ESTIMATE`, priced in bytes via `system.parts`.

        Reported as `scan_bytes`, never as result size: an aggregate over 20M
        rows reads the whole table and returns four rows.
        """
        from app.data_sources.fast.extractor import Estimate
        from app.data_sources.fast.sql_dialect import strip_trailing_semicolon

        inner = strip_trailing_semicolon(sql)
        try:
            ch = self._conn()
            plan = ch.query(f"EXPLAIN ESTIMATE {inner}")
            cols = list(plan.column_names)
            per_table = [dict(zip(cols, r, strict=False)) for r in plan.result_rows]
            rows_read = sum(int(t.get("rows") or 0) for t in per_table)

            scan = 0
            for t in per_table:
                stats = ch.query(
                    "SELECT sum(rows), sum(data_compressed_bytes) FROM system.parts "
                    "WHERE active AND database = {db:String} AND table = {tbl:String}",
                    parameters={"db": t.get("database"), "tbl": t.get("table")},
                ).result_rows
                total_rows, total_bytes = (stats[0] if stats else (0, 0))
                if total_rows:
                    scan += int(int(t.get("rows") or 0) * (int(total_bytes or 0) / int(total_rows)))
        except Exception as e:
            return Estimate(supported=False, note=f"ClickHouse EXPLAIN ESTIMATE failed: {e}")

        return Estimate(
            scan_bytes=scan,
            note=(
                f"ClickHouse EXPLAIN ESTIMATE: ~{rows_read:,} rows read; scan size is "
                "an upper bound (compressed bytes of all columns of the rows read); "
                "result size is bounded by the hard caps instead"
            ),
        )

    def preview(self, sql: str, limit: int) -> tuple[list[dict], list[list]]:
        """First `limit` rows, bounded by the server rather than by rewriting.

        `max_result_rows` + `result_overflow_mode=break` stops the query once it
        has enough, so the admin's own ORDER BY / LIMIT keep their meaning.
        """
        ch = self._conn()
        q = self._with_enum_strings(sql)
        res = ch.query(
            q,
            settings={
                "max_result_rows": int(limit),
                "result_overflow_mode": "break",
                "max_execution_time": DEFAULT_MAX_EXECUTION_SECONDS,
            },
        )
        cols = [
            {"name": n, "dtype": getattr(t, "name", str(t))}
            for n, t in zip(res.column_names, res.column_types, strict=False)
        ]
        rows = [[_jsonable(v) for v in r] for r in res.result_rows[:limit]]
        return cols, rows

    def stream_batches(self, sql: str, batch_rows: int) -> Iterator["object"]:
        """Arrow batches straight from ClickHouse into the artifact."""
        import pyarrow as pa

        from app.data_sources.fast.sources import arrow_table

        ch = self._conn()
        q = self._with_enum_strings(sql)
        query_id = f"bow-extract-{uuid.uuid4()}"
        settings = {
            "max_block_size": int(batch_rows),
            "max_execution_time": DEFAULT_MAX_EXECUTION_SECONDS,
            "query_id": query_id,
        }

        completed = False
        try:
            with ch.query_arrow_stream(q, settings=settings) as stream:
                emitted = False
                for batch in stream:
                    emitted = True
                    tbl = pa.Table.from_batches([batch])
                    # Decimal256 is wider than DuckDB can store; widen as Oracle does.
                    yield arrow_table({n: tbl.column(n) for n in tbl.column_names}) \
                        if _has_wide_decimal(tbl.schema) else tbl
                if not emitted:
                    # Zero rows: still emit the shape so the relation exists.
                    yield self._empty_table(q)
            completed = True
        finally:
            if not completed:
                self._kill(query_id)

    # -- helpers ----------------------------------------------------------

    def _describe(self, sql: str) -> list[tuple[str, str]]:
        from app.data_sources.fast.sql_dialect import strip_trailing_semicolon

        res = self._conn().query(f"DESCRIBE ({strip_trailing_semicolon(sql)})")
        return [(r[0], r[1]) for r in res.result_rows]

    def _with_enum_strings(self, sql: str) -> str:
        """`sql`, with any Enum column cast to its label.

        Untouched when there are no enums, so the common case runs the admin's
        SQL byte-for-byte. A failed DESCRIBE also leaves it untouched — the
        query itself will then report the real error.
        """
        from app.data_sources.fast.sql_dialect import strip_trailing_semicolon

        try:
            enums = [n for n, t in self._describe(sql) if _ENUM_RE.match(t)]
        except Exception:
            return sql
        if not enums:
            return sql
        repl = ", ".join(
            f"toString({_ident(n)}) AS {_ident(n)}" for n in enums
        )
        return f"SELECT * REPLACE ({repl}) FROM ({strip_trailing_semicolon(sql)})"

    def _empty_table(self, sql: str):
        import pyarrow as pa

        try:
            names = [n for n, _ in self._describe(sql)]
        except Exception:
            names = []
        return pa.Table.from_pydict(
            {n: [] for n in names}, schema=pa.schema([(n, pa.string()) for n in names])
        )

    def _kill(self, query_id: str) -> None:
        try:
            self._conn().command(
                "KILL QUERY WHERE query_id = {qid:String} ASYNC",
                parameters={"qid": query_id},
            )
            logger.info("clickhouse.extraction.cancelled", extra={"query_id": query_id})
        except Exception:
            logger.debug("Could not kill ClickHouse query", exc_info=True)


def _ident(name: str) -> str:
    return "`" + name.replace("\\", "\\\\").replace("`", "\\`") + "`"


def _has_wide_decimal(schema) -> bool:
    import pyarrow as pa

    from app.data_sources.fast.sources import DUCKDB_MAX_DECIMAL_PRECISION

    return any(
        pa.types.is_decimal(f.type) and f.type.precision > DUCKDB_MAX_DECIMAL_PRECISION
        for f in schema
    )


def _jsonable(v):
    """Preview rows go straight to JSON; clickhouse-connect returns rich types."""
    import datetime as _dt
    import decimal
    import ipaddress

    if v is None or isinstance(v, (str, bool, float)):
        return v
    if isinstance(v, int):
        # UInt64/Int128 can exceed what a JS number holds exactly.
        return v if abs(v) <= 2**53 else str(v)
    if isinstance(v, decimal.Decimal):
        return float(v)
    if isinstance(v, (_dt.datetime, _dt.date, _dt.time)):
        return v.isoformat()
    if isinstance(v, (bytes, bytearray)):
        return v.decode("utf-8", "replace")
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    if isinstance(v, dict):
        return {str(k): _jsonable(x) for k, x in v.items()}
    if isinstance(v, (uuid.UUID, ipaddress.IPv4Address, ipaddress.IPv6Address)):
        return str(v)
    return str(v)
