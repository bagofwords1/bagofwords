"""Dialect differences the custom-query extractor cannot paper over.

Two things in the extraction path are not portable SQL, and both were written
against Postgres first:

**Row bounding.** `SELECT * FROM (…) LIMIT n` is Postgres/MySQL syntax. Oracle
has no LIMIT before 12c and needs `ROWNUM`; SQL Server needs `TOP`. Even the
alias differs — Oracle rejects `AS` before a derived-table alias, which every
other dialect accepts, so the wrapper never emits it.

**Cost estimation.** The pre-flight refusal ("this query would return 800M
rows, narrow it") only works if we can ask the planner. `EXPLAIN` exists
everywhere in name and nowhere in shape: Postgres has `FORMAT JSON`, MySQL
returns a row per table with a `rows` column, SQL Server needs
`SET SHOWPLAN_XML ON` and returns a plan document, and Oracle writes into
`PLAN_TABLE` and expects you to query it afterwards.

Getting this wrong is not a syntax error the admin sees — `estimate()` catches
failures and reports `supported=False`, so a broken dialect silently downgrades
to "no pre-flight check at all" and the first anyone hears about it is a
half-hour extraction that trips the hard caps. That is why the estimator says
*which* dialect it used, and why the tests assert per dialect rather than
asserting "it didn't crash".
"""

import logging
import re
import uuid
from typing import TYPE_CHECKING, Tuple

from sqlalchemy import text

if TYPE_CHECKING:
    from app.data_sources.fast.extractor import Estimate

logger = logging.getLogger(__name__)

# Which client attribute implies which dialect. Ordered: mariadb and mysql
# share a dialect, so the value matters more than the key.
# A client may declare its dialect outright. Preferred over sniffing a URI
# attribute: Snowflake builds its engine from a URL *plus* connect_args (the
# private key cannot live in a URL), so it has no single URI to detect, and
# BigQuery has no URI at all.
DIALECT_ATTR = "EXTRACTION_DIALECT"

URI_ATTR_TO_DIALECT = (
    ("pg_uri", "postgresql"),
    ("mysql_uri", "mysql"),
    ("mariadb_uri", "mysql"),
    ("sql_server_uri", "mssql"),
    ("oracle_uri", "oracle"),
    ("sqlite_uri", "sqlite"),
)


def dialect_of(client) -> str:
    """The SQL dialect a client speaks, or "" if we don't know it.

    Derived from the URI attribute the client exposes rather than from its
    class name, because that is the same signal the extractor uses to decide
    whether it can stream at all.
    """
    declared = getattr(client, DIALECT_ATTR, None)
    if declared:
        return str(declared)
    for attr, dialect in URI_ATTR_TO_DIALECT:
        if getattr(client, attr, None):
            return dialect
    return ""


def strip_trailing_semicolon(sql: str) -> str:
    return sql.rstrip().rstrip(";").rstrip()


def bounded_sql(sql: str, limit: int, dialect: str) -> Tuple[str, bool]:
    """Wrap `sql` so it can return at most `limit` rows.

    Returns (sql, bounded). `bounded=False` means we could not express the
    bound for this dialect and the caller must enforce it another way — by
    stopping the fetch — rather than assume the query is capped.

    The admin's SQL goes inside a derived table untouched: rewriting it (say,
    appending LIMIT) would change the meaning of a query that already has one.
    No `AS` before the alias — Oracle is the only dialect that rejects it and
    every other dialect accepts its absence.
    """
    inner = strip_trailing_semicolon(sql)
    n = int(limit)

    if dialect in ("postgresql", "mysql", "sqlite", "snowflake"):
        return f"SELECT * FROM ({inner}) bow_sub LIMIT {n}", True
    if dialect == "oracle":
        # ROWNUM rather than FETCH FIRST: identical result and it works on 11g,
        # which is exactly the vintage of box this feature exists for.
        return f"SELECT * FROM ({inner}) bow_sub WHERE ROWNUM <= {n}", True
    if dialect == "mssql":
        return f"SELECT TOP {n} * FROM ({inner}) bow_sub", True
    return inner, False


# --------------------------------------------------------------------------
# Cost estimation
# --------------------------------------------------------------------------

# A plausible average row width for dialects whose planner reports a row count
# but no width. Only used to give the byte ceiling something to bite on; the
# estimate says so in its note rather than pretending to precision.
ASSUMED_ROW_WIDTH_BYTES = 100


def _estimate(**kw) -> "Estimate":
    from app.data_sources.fast.extractor import Estimate

    rows = kw.get("rows")
    width = kw.get("width_bytes")
    total = rows * max(width or 0, 1) if (rows and width) else None
    return Estimate(total_bytes=total, **kw)


def explain_postgresql(conn, sql: str) -> "Estimate":
    """Result-size estimate from `EXPLAIN (FORMAT JSON)`."""
    row = conn.execute(text(f"EXPLAIN (FORMAT JSON) {strip_trailing_semicolon(sql)}")).scalar()
    plan = row[0]["Plan"] if isinstance(row, list) else row["Plan"]
    return _estimate(
        rows=int(plan.get("Plan Rows") or 0),
        width_bytes=int(plan.get("Plan Width") or 0),
    )


def explain_mysql(conn, sql: str) -> "Estimate":
    """Result-size estimate from classic `EXPLAIN`.

    One row per accessed table; the largest `rows` is the closest thing to a
    result-size estimate MySQL offers. It reports no width.
    """
    res = conn.execute(text(f"EXPLAIN {strip_trailing_semicolon(sql)}")).mappings().all()
    rows = 0
    for r in res:
        v = r.get("rows") or r.get("ROWS")
        if v:
            rows = max(rows, int(v))
    return _estimate(rows=rows, width_bytes=ASSUMED_ROW_WIDTH_BYTES,
                     note="row width estimated (EXPLAIN provides none)")


def explain_mssql(conn, sql: str) -> "Estimate":
    """(rows, width_bytes, note) from `SET SHOWPLAN_XML ON`.

    SHOWPLAN_XML compiles the statement without running it and returns the plan
    as XML. `StatementEstRows` on the statement node is the estimated result
    cardinality; `AvgRowSize` on the outermost operator is its width.

    SHOWPLAN_XML must be alone in its batch, so it is set and cleared around the
    statement rather than prefixed to it. It also needs SHOWPLAN permission — a
    denial surfaces as an exception and the caller reports the estimate as
    unsupported, which is the honest outcome.
    """
    import xml.etree.ElementTree as ET

    conn.exec_driver_sql("SET SHOWPLAN_XML ON")
    try:
        plan_xml = conn.exec_driver_sql(strip_trailing_semicolon(sql)).scalar()
    finally:
        conn.exec_driver_sql("SET SHOWPLAN_XML OFF")

    if not plan_xml:
        return _estimate(note="SHOWPLAN_XML returned no plan")

    root = ET.fromstring(plan_xml)
    rows = None
    width = None
    for el in root.iter():
        tag = el.tag.rsplit("}", 1)[-1]
        if rows is None and "StatementEstRows" in el.attrib:
            rows = int(float(el.attrib["StatementEstRows"]))
        if width is None and tag == "RelOp" and "AvgRowSize" in el.attrib:
            width = int(float(el.attrib["AvgRowSize"]))
        if rows is not None and width is not None:
            break
    if rows is None:
        return _estimate(note="no StatementEstRows in the plan")
    return _estimate(rows=rows, width_bytes=width,
                     note="" if width else "row width unavailable in the plan")


_STATEMENT_ID_SAFE = re.compile(r"[^A-Za-z0-9_]")


def explain_oracle(conn, sql: str) -> "Estimate":
    """(rows, width_bytes, note) from `EXPLAIN PLAN` + `PLAN_TABLE`.

    Oracle's EXPLAIN PLAN has no result set — it writes rows into PLAN_TABLE,
    which then has to be read and cleaned up. Row 0 (`ID = 0`) is the whole
    statement: `CARDINALITY` is the estimated rows and `BYTES` the estimated
    total size, so unlike the other dialects Oracle gives total bytes directly
    and width is derived rather than the other way round.

    PLAN_TABLE is a session-private global temporary table via the public
    synonym, present by default since 10g. The DELETE is belt-and-braces for
    the case where it has been replaced by a real shared table.
    """
    statement_id = "bow_" + _STATEMENT_ID_SAFE.sub("", uuid.uuid4().hex)[:20]
    inner = strip_trailing_semicolon(sql)
    conn.exec_driver_sql(f"EXPLAIN PLAN SET STATEMENT_ID = '{statement_id}' FOR {inner}")
    try:
        row = conn.execute(
            text(
                "SELECT CARDINALITY, BYTES FROM PLAN_TABLE "
                "WHERE STATEMENT_ID = :sid AND ID = 0"
            ),
            {"sid": statement_id},
        ).first()
    finally:
        try:
            conn.execute(
                text("DELETE FROM PLAN_TABLE WHERE STATEMENT_ID = :sid"),
                {"sid": statement_id},
            )
            conn.commit()
        except Exception:
            logger.debug("Could not clean PLAN_TABLE for %s", statement_id, exc_info=True)

    if row is None:
        return _estimate(note="no PLAN_TABLE row for the statement")
    cardinality, total_bytes = row[0], row[1]
    rows = int(cardinality) if cardinality is not None else None
    if total_bytes is None or not rows:
        return _estimate(rows=rows, note="plan reported no BYTES")
    # Oracle reports total bytes; width is the derived quantity here.
    return _estimate(rows=rows, width_bytes=max(int(int(total_bytes) / rows), 1))


def explain_sqlite(conn, sql: str) -> "Estimate":
    """SQLite has no cardinality estimate to give.

    `EXPLAIN QUERY PLAN` describes *how* the query runs (which index, which
    scan) and never *how many rows*; the optimizer works from a handful of
    ANALYZE-time heuristics rather than the statistics the other engines
    expose. So there is nothing to refuse a query on before running it.

    That is materially fine here and nowhere else: a SQLite database is a local
    file, so the failure this pre-flight check exists to prevent — a runaway
    scan saturating a shared on-prem server — does not apply. Extraction is
    still bounded by the row, byte and wall-clock caps, which abort mid-flight.

    It is spelled out as a real explainer rather than left out of the table so
    the "every accelerable type has an estimator" check keeps its meaning, and
    so the reason shows up in the estimate's note instead of as a bare failure.
    """
    return _estimate(note="SQLite exposes no row estimate; hard caps apply instead")


def explain_snowflake(conn, sql: str) -> "Estimate":
    """Scan-cost estimate from `EXPLAIN USING JSON`.

    Snowflake reports the opposite half of what Postgres does. `GlobalStats`
    gives `bytesAssigned` — how much data the warehouse must read — and no row
    or width estimate for the result at all.

    That is deliberately NOT mapped onto the result-size fields. The query most
    worth caching on Snowflake is a rollup that reads gigabytes and returns a
    handful of rows; calling those scanned bytes the "result size" would refuse
    exactly the queries this feature exists to serve. It lands in `scan_bytes`,
    which is the cost ceiling, and the row/byte caps simply have nothing to
    check — the mid-flight limits still bound the artifact.
    """
    import json as _json

    raw = conn.execute(text(f"EXPLAIN USING JSON {strip_trailing_semicolon(sql)}")).scalar()
    plan = _json.loads(raw) if isinstance(raw, (str, bytes)) else raw
    stats = (plan or {}).get("GlobalStats") or {}
    scanned = stats.get("bytesAssigned")
    if scanned is None:
        return _estimate(note="no bytesAssigned in the Snowflake plan")
    return _estimate(
        scan_bytes=int(scanned),
        note=(
            f"Snowflake reports scan size only "
            f"({stats.get('partitionsAssigned')}/{stats.get('partitionsTotal')} partitions); "
            f"result size is bounded by the hard caps instead"
        ),
    )


EXPLAINERS = {
    "snowflake": explain_snowflake,
    "sqlite": explain_sqlite,
    "postgresql": explain_postgresql,
    "mysql": explain_mysql,
    "mssql": explain_mssql,
    "oracle": explain_oracle,
}
