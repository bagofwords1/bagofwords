"""Row bounding and cost estimation, per dialect.

The custom-query extractor was written against Postgres and its two
non-portable pieces silently misbehave elsewhere:

  * `SELECT * FROM (…) AS t LIMIT n` — Oracle rejects both the `AS` and the
    `LIMIT`; SQL Server rejects the `LIMIT`. A preview that "auto-limits to 100
    rows" therefore did not run at all on the two databases this whole feature
    exists for. Wrapping turned out to be unsafe even where it parses (MySQL
    discards a derived table's ORDER BY), so `preview` now bounds the fetch
    instead and `bounded_sql` survives only for clients that cannot stream.
  * `EXPLAIN` — the pre-flight refusal downgrades to `supported=False` on any
    error, so a wrong dialect does not raise. It just stops refusing huge
    queries, and nobody finds out until an extraction runs for half an hour and
    trips a hard cap.

Both failure modes are quiet, which is exactly why they get asserted per
dialect here rather than "it didn't crash".
"""

import pytest

from app.data_sources.fast import sql_dialect


class FakeClient:
    def __init__(self, **attrs):
        for k, v in attrs.items():
            setattr(self, k, v)


# --------------------------------------------------------------------------
# Dialect detection
# --------------------------------------------------------------------------

@pytest.mark.parametrize("attr,expected", [
    ("pg_uri", "postgresql"),
    ("mysql_uri", "mysql"),
    ("mariadb_uri", "mysql"),      # same dialect, different client
    ("sql_server_uri", "mssql"),
    ("oracle_uri", "oracle"),
])
def test_dialect_comes_from_the_uri_attribute(attr, expected):
    assert sql_dialect.dialect_of(FakeClient(**{attr: "x://y"})) == expected


def test_a_client_with_no_uri_has_no_dialect():
    assert sql_dialect.dialect_of(FakeClient()) == ""


# --------------------------------------------------------------------------
# Row bounding — the fallback for clients that cannot stream
# --------------------------------------------------------------------------

@pytest.mark.parametrize("dialect,expected", [
    ("postgresql", "SELECT * FROM (SELECT 1) bow_sub LIMIT 100"),
    ("mysql", "SELECT * FROM (SELECT 1) bow_sub LIMIT 100"),
    ("oracle", "SELECT * FROM (SELECT 1) bow_sub WHERE ROWNUM <= 100"),
    ("mssql", "SELECT TOP 100 * FROM (SELECT 1) bow_sub"),
])
def test_each_dialect_gets_syntax_it_accepts(dialect, expected):
    bounded, ok = sql_dialect.bounded_sql("SELECT 1", 100, dialect)
    assert bounded == expected
    assert ok is True


def test_no_dialect_emits_AS_before_the_alias():
    """Oracle is the only dialect that rejects `AS` on a derived table, and
    every other dialect accepts its absence — so it is never emitted."""
    for dialect in ("postgresql", "mysql", "oracle", "mssql"):
        bounded, _ = sql_dialect.bounded_sql("SELECT 1", 10, dialect)
        assert " AS bow_sub" not in bounded


def test_an_unknown_dialect_says_so_instead_of_pretending():
    """The caller must know the bound was not applied so it can enforce one at
    fetch time; silently returning Postgres syntax would run unbounded SQL."""
    bounded, ok = sql_dialect.bounded_sql("SELECT 1", 100, "")
    assert ok is False
    assert "LIMIT" not in bounded.upper()


def test_the_admin_sql_is_never_rewritten_only_wrapped():
    """A query that already carries its own LIMIT/ORDER BY keeps its meaning."""
    sql = "SELECT a FROM t ORDER BY a DESC LIMIT 5"
    bounded, _ = sql_dialect.bounded_sql(sql, 100, "postgresql")
    assert f"({sql})" in bounded


def test_a_trailing_semicolon_does_not_break_the_wrapper():
    bounded, _ = sql_dialect.bounded_sql("SELECT 1 ;  ", 10, "postgresql")
    assert bounded == "SELECT * FROM (SELECT 1) bow_sub LIMIT 10"


# --------------------------------------------------------------------------
# Cost estimation — one explainer per dialect
# --------------------------------------------------------------------------

class FakeResult:
    def __init__(self, scalar=None, mappings=None, first=None):
        self._scalar = scalar
        self._mappings = mappings or []
        self._first = first

    def scalar(self):
        return self._scalar

    def mappings(self):
        return self

    def all(self):
        return self._mappings

    def first(self):
        return self._first


class FakeConn:
    """Records what was sent and replies from a scripted list."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.sent = []

    def _next(self):
        return self.replies.pop(0) if self.replies else FakeResult()

    def execute(self, stmt, params=None):
        self.sent.append((str(stmt), params))
        return self._next()

    def exec_driver_sql(self, stmt):
        self.sent.append((str(stmt), None))
        return self._next()

    def commit(self):
        pass


def test_postgresql_reads_rows_and_width_from_the_json_plan():
    conn = FakeConn([FakeResult(scalar=[{"Plan": {"Plan Rows": 12345, "Plan Width": 48}}])])
    est = sql_dialect.explain_postgresql(conn, "SELECT * FROM t")
    assert (est.rows, est.width_bytes) == (12345, 48)
    # Postgres reports RESULT size, so the byte total is derived and scan cost
    # is left unknown — the two are different questions.
    assert est.total_bytes == 12345 * 48
    assert est.scan_bytes is None
    assert "FORMAT JSON" in conn.sent[0][0]


def test_mysql_takes_the_largest_per_table_row_estimate():
    conn = FakeConn([FakeResult(mappings=[{"rows": 100}, {"rows": 90000}, {"rows": 12}])])
    est = sql_dialect.explain_mysql(conn, "SELECT * FROM a JOIN b")
    assert est.rows == 90000
    # MySQL reports no width; the estimate says so rather than implying precision.
    assert est.width_bytes == sql_dialect.ASSUMED_ROW_WIDTH_BYTES
    assert "estimated" in est.note


MSSQL_PLAN = """<?xml version="1.0"?>
<ShowPlanXML xmlns="http://schemas.microsoft.com/sqlserver/2004/07/showplan">
  <BatchSequence><Batch><Statements>
    <StmtSimple StatementEstRows="8400000" StatementText="SELECT ...">
      <QueryPlan>
        <RelOp NodeId="0" PhysicalOp="Clustered Index Scan" EstimateRows="8400000" AvgRowSize="137"/>
      </QueryPlan>
    </StmtSimple>
  </Statements></Batch></BatchSequence>
</ShowPlanXML>"""


def test_sql_server_parses_showplan_xml():
    conn = FakeConn([FakeResult(), FakeResult(scalar=MSSQL_PLAN), FakeResult()])
    est = sql_dialect.explain_mssql(conn, "SELECT * FROM orders")
    assert (est.rows, est.width_bytes) == (8400000, 137)


def test_sql_server_always_turns_showplan_back_off():
    """SHOWPLAN_XML makes the session return plans instead of results. Leaving
    it on would break every subsequent query on that pooled connection."""
    conn = FakeConn([FakeResult(), FakeResult(scalar=MSSQL_PLAN), FakeResult()])
    sql_dialect.explain_mssql(conn, "SELECT 1")
    statements = [s for s, _ in conn.sent]
    assert statements[0] == "SET SHOWPLAN_XML ON"
    assert statements[-1] == "SET SHOWPLAN_XML OFF"


def test_sql_server_turns_showplan_off_even_when_the_query_fails():
    class Failing(FakeConn):
        def exec_driver_sql(self, stmt):
            self.sent.append((str(stmt), None))
            if stmt.startswith("SELECT"):
                raise RuntimeError("invalid object name")
            return FakeResult()

    conn = Failing([])
    with pytest.raises(RuntimeError):
        sql_dialect.explain_mssql(conn, "SELECT * FROM nope")
    assert conn.sent[-1][0] == "SET SHOWPLAN_XML OFF"


def test_oracle_reads_cardinality_and_bytes_from_plan_table():
    conn = FakeConn([FakeResult(), FakeResult(first=(4200, 4200 * 250)), FakeResult()])
    est = sql_dialect.explain_oracle(conn, "SELECT * FROM orders")
    # Oracle reports total BYTES; width is the derived quantity, not the raw one.
    assert est.rows == 4200
    assert est.width_bytes == 250


def test_oracle_cleans_up_its_plan_table_rows():
    conn = FakeConn([FakeResult(), FakeResult(first=(1, 100)), FakeResult()])
    sql_dialect.explain_oracle(conn, "SELECT 1 FROM dual")
    statements = [s for s, _ in conn.sent]
    assert statements[0].startswith("EXPLAIN PLAN SET STATEMENT_ID = 'bow_")
    assert "DELETE FROM PLAN_TABLE" in statements[-1]


def test_oracle_uses_a_unique_statement_id_per_call():
    """Two concurrent estimates on one schema must not read each other's plan."""
    ids = set()
    for _ in range(5):
        conn = FakeConn([FakeResult(), FakeResult(first=(1, 100)), FakeResult()])
        sql_dialect.explain_oracle(conn, "SELECT 1 FROM dual")
        ids.add(conn.sent[0][0])
    assert len(ids) == 5


def test_oracle_statement_id_cannot_be_injected():
    """It is interpolated into SQL (bind variables are not allowed in EXPLAIN
    PLAN's SET STATEMENT_ID), so it has to be structurally safe."""
    conn = FakeConn([FakeResult(), FakeResult(first=(1, 100)), FakeResult()])
    sql_dialect.explain_oracle(conn, "SELECT 1 FROM dual")
    statement = conn.sent[0][0]
    sid = statement.split("'")[1]
    assert sid.replace("_", "").isalnum()


def test_oracle_survives_a_plan_table_with_no_row():
    conn = FakeConn([FakeResult(), FakeResult(first=None), FakeResult()])
    est = sql_dialect.explain_oracle(conn, "SELECT 1 FROM dual")
    assert est.rows is None
    assert "PLAN_TABLE" in est.note


# --------------------------------------------------------------------------
# How the extractor consumes them
# --------------------------------------------------------------------------

def test_every_accelerable_type_has_an_explainer():
    """A connector offered for acceleration with no estimator loses its
    pre-flight refusal silently — the whole point of the split."""
    from app.services.custom_query_service import ACCELERABLE_TYPES

    # Connection type -> the dialect its client speaks.
    type_to_dialect = {
        "postgresql": "postgresql",
        "mysql": "mysql",
        "mariadb": "mysql",
        "mssql": "mssql",
        "oracledb": "oracle",
        "sqlite": "sqlite",
        "snowflake": "snowflake",
        "ms_fabric": "mssql",     # Fabric speaks T-SQL
    }
    # BigQuery and Sybase do not go through the SQL dialect table at all —
    # each has a native extraction source instead. Assert that explicitly
    # rather than letting them fall through the map lookup.
    from app.data_sources.fast.bigquery_source import BigQuerySource
    from app.data_sources.fast.sybase_source import SybaseSource
    assert "bigquery" in ACCELERABLE_TYPES
    assert hasattr(BigQuerySource, "estimate")
    assert "sybase" in ACCELERABLE_TYPES
    assert hasattr(SybaseSource, "estimate")
    for conn_type in ACCELERABLE_TYPES:
        if conn_type in ("bigquery", "sybase"):
            continue
        dialect = type_to_dialect[conn_type]
        assert dialect in sql_dialect.EXPLAINERS, conn_type
        bounded, ok = sql_dialect.bounded_sql("SELECT 1", 100, dialect)
        assert ok, conn_type


def test_an_unsupported_dialect_reports_why_rather_than_going_quiet():
    from app.data_sources.fast import extractor

    est = extractor.estimate(FakeClient(), "SELECT 1")
    assert est.supported is False
    assert "cannot be materialized" in est.note


def test_a_failed_explain_names_the_dialect_it_tried():
    from app.data_sources.fast import extractor

    class Client:
        pg_uri = "postgresql://u:p@h/d"

        def connect(self):
            raise RuntimeError("permission denied for SHOWPLAN")

    est = extractor.estimate(Client(), "SELECT 1")
    assert est.supported is False
    assert "postgresql EXPLAIN failed" in est.note


# --------------------------------------------------------------------------
# preview() bounds the fetch, not the SQL
# --------------------------------------------------------------------------

class RecordingConn:
    """Captures the SQL actually sent and hands back `rows` in batches."""

    def __init__(self, rows, colnames=("a",)):
        self.rows = rows
        self.colnames = colnames
        self.sent = []
        self.closed = False
        self.invalidated = False
        self.options = {}

    def execution_options(self, **kw):
        self.options.update(kw)
        return self

    def execute(self, stmt):
        self.sent.append(str(stmt))
        return self

    def keys(self):
        return list(self.colnames)

    def fetchmany(self, n):
        out, self.rows = self.rows[:n], self.rows[n:]
        return out

    def close(self):
        self.closed = True

    def invalidate(self):
        self.invalidated = True


class StreamingClient:
    """Minimal client with a dialect and a connect() contextmanager."""

    def __init__(self, conn, uri_attr="pg_uri"):
        self._conn = conn
        setattr(self, uri_attr, "x://y")

    def connect(self):
        from contextlib import contextmanager

        @contextmanager
        def cm():
            yield self._conn

        return cm()


def test_preview_sends_the_admin_sql_unmodified():
    """MySQL discards a derived table's ORDER BY and SQL Server rejects it, so
    a preview of `… ORDER BY amount DESC` showed the *cheapest* rows. The only
    rewrite-proof answer is not to rewrite."""
    from app.data_sources.fast import extractor

    sql = "SELECT order_id, amount FROM orders ORDER BY amount DESC"
    conn = RecordingConn([(1, 9), (2, 8), (3, 7)])
    extractor.preview(StreamingClient(conn), sql, limit=3)
    assert conn.sent == [sql]


def test_preview_strips_only_a_trailing_semicolon():
    from app.data_sources.fast import extractor

    conn = RecordingConn([(1,)])
    extractor.preview(StreamingClient(conn), "SELECT 1 ;", limit=5)
    assert conn.sent == ["SELECT 1"]


def test_preview_streams_rather_than_buffering():
    from app.data_sources.fast import extractor

    conn = RecordingConn([(i,) for i in range(500)])
    extractor.preview(StreamingClient(conn), "SELECT * FROM huge", limit=10)
    assert conn.options.get("stream_results") is True


def test_preview_abandons_the_query_when_more_rows_remain(monkeypatch):
    """Otherwise closing an unbuffered MySQL cursor drains the rest of the
    result set down the wire — the whole scan we were trying to avoid."""
    from app.data_sources.fast import extractor
    from app.data_sources import query_cancellation

    cancelled = []
    monkeypatch.setattr(query_cancellation, "cancel_thread",
                        lambda c, t: cancelled.append(t) or "cancelled (postgresql cancel)")

    conn = RecordingConn([(i,) for i in range(500)])
    _, rows = extractor.preview(StreamingClient(conn), "SELECT * FROM huge", limit=10)
    assert len(rows) == 10
    assert cancelled, "a partially-read query was left running"
    assert conn.invalidated, "a connection with an unread result went back to the pool"


def test_preview_does_not_cancel_a_query_it_read_to_the_end(monkeypatch):
    """A small result is complete; cancelling would burn a pooled connection
    for nothing."""
    from app.data_sources.fast import extractor
    from app.data_sources import query_cancellation

    def boom(*a, **k):
        raise AssertionError("cancelled a fully-read query")

    monkeypatch.setattr(query_cancellation, "cancel_thread", boom)

    conn = RecordingConn([(1,), (2,)])
    _, rows = extractor.preview(StreamingClient(conn), "SELECT 1", limit=100)
    assert len(rows) == 2
    assert conn.invalidated is False


def test_preview_falls_back_to_sql_bounding_only_without_a_dialect():
    """Non-streaming clients have no fetch to bound, so there the SQL wrap is
    the only tool — with the caveats it carries."""
    from app.data_sources.fast import extractor
    import pandas as pd

    sent = {}

    class NoDialect:
        def execute_query(self, sql):
            sent["sql"] = sql
            return pd.DataFrame({"a": [1, 2, 3]})

    extractor.preview(NoDialect(), "SELECT a FROM t", limit=2)
    # No dialect means no expressible bound; it is passed through, and the
    # DataFrame is truncated after the fact.
    assert sent["sql"] == "SELECT a FROM t"


# --------------------------------------------------------------------------
# SQLite
# --------------------------------------------------------------------------

def test_sqlite_is_detected_from_its_uri():
    from app.data_sources.clients.sqlite_client import SqliteClient

    assert sql_dialect.dialect_of(SqliteClient(database="/tmp/x.db")) == "sqlite"


def test_an_in_memory_sqlite_is_not_accelerable():
    """A second handle to ':memory:' opens a *different*, empty database, so
    extraction would silently materialize nothing."""
    from app.data_sources.clients.sqlite_client import SqliteClient

    assert SqliteClient(database=":memory:").sqlite_uri == ""
    assert sql_dialect.dialect_of(SqliteClient(database=":memory:")) == ""


def test_sqlite_bounds_rows_with_limit():
    bounded, ok = sql_dialect.bounded_sql("SELECT 1", 100, "sqlite")
    assert bounded == "SELECT * FROM (SELECT 1) bow_sub LIMIT 100"
    assert ok is True


def test_sqlite_reports_that_it_has_no_row_estimate():
    """SQLite's EXPLAIN QUERY PLAN describes *how* a query runs, never how many
    rows. Saying so beats a bare failure — and it is materially fine, because a
    SQLite database is a local file, not the shared server the pre-flight
    refusal exists to protect."""
    est = sql_dialect.explain_sqlite(None, "SELECT 1")
    assert est.rows is None and est.width_bytes is None
    assert "no row estimate" in est.note


def test_sqlite_estimate_degrades_without_raising():
    from app.data_sources.clients.sqlite_client import SqliteClient
    from app.data_sources.fast import extractor

    est = extractor.estimate(SqliteClient(database="/tmp/does-not-matter.db"), "SELECT 1")
    assert est.supported is False
    assert "no row estimate" in est.note


def test_sqlite_is_an_accelerable_type():
    from app.services.custom_query_service import ACCELERABLE_TYPES, VERIFIED_TYPES

    assert "sqlite" in ACCELERABLE_TYPES
    assert "sqlite" in VERIFIED_TYPES


def test_sql_server_is_accelerable_under_its_registry_casing():
    """The registry stores SQL Server as "MSSQL" — its one uppercase key —
    so a literal membership test against the lowercase ACCELERABLE_TYPES
    silently hid acceleration from every SQL Server connection. The gate goes
    through is_accelerable_type, which normalizes case; assert against the
    registry key itself so a future rename is caught too."""
    from app.schemas.data_source_registry import REGISTRY
    from app.services.custom_query_service import is_accelerable_type

    (sql_server_type,) = [t for t, e in REGISTRY.items()
                          if e.title == "Microsoft SQL Server"]
    assert is_accelerable_type(sql_server_type)


def test_is_accelerable_type_still_excludes_the_rest():
    from app.services.custom_query_service import is_accelerable_type

    assert not is_accelerable_type("clickhouse")
    assert not is_accelerable_type(None)


def test_accelerable_types_are_all_lowercase():
    """is_accelerable_type lowercases its input, so an uppercase entry in the
    set itself would be unreachable."""
    from app.services.custom_query_service import ACCELERABLE_TYPES

    assert all(t == t.lower() for t in ACCELERABLE_TYPES)
