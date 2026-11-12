"""Connections over local files (CSV, QVD, PBIX, a Power BI Report Server
report's model) run agent SQL only as one SELECT over their own tables, on
an engine that cannot reach other files, extensions or the network.

Contract:
- the statement is read with DuckDB's own parser on a separate locked
  connection, which never opens a file; anything but one SELECT is refused
  (`ReadOnlyViolation`), and so are file readers, settings reads and
  SQL-in-a-string table functions;
- every table the SELECT reads is one of the connection's tables (or a CTE
  of the statement); anything else is refused (`TableScopeViolation`);
- the query connection itself is locked before agent SQL runs: even a
  statement that skipped the check cannot read another file, write one,
  load an extension or change the settings;
- a refusal keeps its type through the client and becomes the normal
  refusal (`UnsafeSQLError` / `TableAccessError`) through the sandbox, in
  both sandbox modes; a broken file still reports a connection error.

Real embedded DuckDB over scratch files under `tmp_path`; nothing touches
the network.
"""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import duckdb
import pandas as pd
import pytest

import app.data_sources.clients.csv_client as csv_client_mod
import app.data_sources.clients.pbix_client as pbix_client_mod
import app.data_sources.clients.powerbi_report_server_client as pbirs_mod
import app.data_sources.clients.qvd_client as qvd_client_mod
from app.ai.code_execution.code_execution import StreamingCodeExecutor, TableAccessError, UnsafeSQLError
from app.ai.code_execution.duckdb_guard import check_duckdb_file_query, inspect_duckdb_sql
from app.ai.code_execution.sql_guard import ReadOnlyViolation
from app.ai.code_execution.table_scope import TableScopeViolation
from app.ai.prompt_formatters import Table
from app.data_sources.clients._duckdb_lockdown import lock_down
from app.data_sources.clients.csv_client import CSVClient
from app.data_sources.clients.pbix_client import PBIXClient
from app.data_sources.clients.powerbi_report_server_client import PowerBIReportServerClient
from app.data_sources.clients.qvd_client import QVDClient

_LINUX = sys.platform.startswith("linux")

_SALES = pd.DataFrame({"id": [1, 2, 3], "amount": [10, 20, 30]})
_CUSTOMERS = pd.DataFrame({"id": [1, 2], "name": ["a", "b"]})


def _sq(path) -> str:
    return str(path).replace("'", "''")


# ------------------------------------------------------------ scratch files

@pytest.fixture
def outside(tmp_path) -> Path:
    """A readable file that belongs to no connection."""
    d = tmp_path / "outside"
    d.mkdir()
    p = d / "other.csv"
    p.write_text("token\nhidden-value\n")
    return p


def _csv(tmp_path) -> CSVClient:
    d = tmp_path / "csv"
    d.mkdir()
    _SALES.to_csv(d / "sales.csv", index=False)
    _CUSTOMERS.to_csv(d / "customers.csv", index=False)
    return CSVClient(file_paths=f"{d}/*.csv")


def _qvd(tmp_path, monkeypatch) -> QVDClient:
    """QVD files whose Parquet cache is already in place (the converter is
    a separate binary; the cache is what queries read)."""
    d = tmp_path / "qvd"
    d.mkdir()
    monkeypatch.setattr(qvd_client_mod, "_CACHE_DIR", tmp_path / "qvd_cache")
    (tmp_path / "qvd_cache").mkdir()
    for name, df in (("sales", _SALES), ("customers", _CUSTOMERS)):
        src = d / f"{name}.qvd"
        src.write_bytes(b"qvd")
        df.to_parquet(QVDClient._cache_key(str(src))[1], index=False)
    return QVDClient(file_paths=f"{d}/*.qvd")


def _pbix(tmp_path, monkeypatch) -> PBIXClient:
    """A .pbix file whose model is already in the Parquet cache."""
    monkeypatch.setattr(pbix_client_mod, "_CACHE_DIR", tmp_path / "pbix_cache")
    f = tmp_path / "model.pbix"
    f.write_bytes(b"pbix")
    c = PBIXClient(str(f))
    cdir = c._cache_key(str(f))[1]
    cdir.mkdir(parents=True)
    for name, df in (("sales", _SALES), ("customers", _CUSTOMERS)):
        df.to_parquet(cdir / f"{name}.parquet", index=False)
    (cdir / "manifest.json").write_text(json.dumps({"sales": "sales.parquet", "customers": "customers.parquet"}))
    return c


class _PbirsReport:
    """A Power BI Report Server connection whose report's model is already in
    the Parquet cache, queried by its table name. Discovery (a server call)
    is the client's cached list."""

    TABLE = "pbix:Shop"

    def __init__(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pbirs_mod, "_PBIX_DATA_CACHE_DIR", tmp_path / "pbirs_cache")
        cdir = pbirs_mod._pbix_data_cache_dir("r1", "2026-01-01")
        cdir.mkdir(parents=True)
        for name, df in (("sales", _SALES), ("customers", _CUSTOMERS)):
            df.to_parquet(cdir / f"{name}.parquet", index=False)
        (cdir / "manifest.json").write_text(json.dumps({"sales": "sales.parquet", "customers": "customers.parquet"}))
        self.client = PowerBIReportServerClient("http://pbirs.invalid", "u", "p")
        self.client._schemas_cache = [Table(
            name=self.TABLE, columns=[], pks=[], fks=[],
            metadata_json={"powerbi_report_server": {
                "report_type": "PowerBIReport", "report_id": "r1",
                "modified_date": "2026-01-01", "report_name": "Shop"}},
        )]

    def execute_query(self, sql):
        return self.client.execute_query(query=sql, table_name=self.TABLE)


_KINDS = ["csv", "qvd", "pbix", "pbirs"]


@pytest.fixture(params=_KINDS)
def files(request, tmp_path, monkeypatch):
    """(kind, client, run): `run(sql)` sends `sql` the way the agent's code
    does for that connection."""
    kind = request.param
    if kind == "csv":
        c = _csv(tmp_path)
        return kind, c, c.execute_query
    if kind == "qvd":
        c = _qvd(tmp_path, monkeypatch)
        return kind, c, c.execute_query
    if kind == "pbix":
        c = _pbix(tmp_path, monkeypatch)
        return kind, c, c.execute_query
    report = _PbirsReport(tmp_path, monkeypatch)
    return kind, report.client, report.execute_query


# ------------------------------------------------------------ reading a statement

def test_inspecting_a_statement_opens_no_file_and_no_connection(tmp_path):
    """The statement is only read: a path that does not exist and an address
    nothing listens on come back as names, with no error."""
    missing = tmp_path / "missing" / "nothing.csv"
    stmt = inspect_duckdb_sql(f"SELECT * FROM '{_sq(missing)}' JOIN read_csv('http://127.0.0.1:9/x.csv') ON true")
    assert ("", "", str(missing).lower()) in stmt.tables
    assert stmt.table_functions == frozenset({"read_csv"})
    assert not missing.parent.exists()


def test_inspecting_reports_tables_functions_and_ctes():
    """`tables` holds what the statement reads from the catalog: a name that
    is one of its CTEs where it stands is not a table read."""
    stmt = inspect_duckdb_sql(
        "WITH recent AS (SELECT * FROM main.sales WHERE amount > 1) "
        "SELECT upper(c.name), count(*) FROM recent JOIN memory.main.customers c USING (id) "
        "JOIN range(3) r ON true GROUP BY 1"
    )
    assert stmt.cte_names == frozenset({"recent"})
    assert stmt.tables == frozenset({("", "main", "sales"), ("memory", "main", "customers")})
    assert stmt.table_functions == frozenset({"range"})
    assert {"upper", "count_star"} <= stmt.functions and "range" not in stmt.functions


@pytest.mark.parametrize("sql", [
    "SELECT 1; SELECT 2",
    "COPY sales TO 'out.csv'",
    "INSTALL httpfs",
    "LOAD httpfs",
    "ATTACH 'other.db'",
    "SET enable_external_access = true",
    "PRAGMA show_tables",
    "EXPLAIN SELECT 1",
    "",
])
def test_anything_but_one_select_is_refused(sql):
    with pytest.raises(ReadOnlyViolation):
        inspect_duckdb_sql(sql)


def _not_a_policy_refusal(err: BaseException) -> bool:
    return isinstance(err, ValueError) and not isinstance(err, (ReadOnlyViolation, TableScopeViolation))


def test_a_query_that_does_not_parse_is_a_fixable_error_with_the_parsers_reason():
    """Nothing ran and nothing was refused: the agent's own text is wrong,
    and DuckDB's reason is quoted back so it can fix it."""
    with pytest.raises(ValueError, match="syntax error") as info:
        inspect_duckdb_sql("SELEC amount FROM sales")
    assert _not_a_policy_refusal(info.value), type(info.value).__mro__


@pytest.mark.parametrize("sql", [
    "DESCRIBE sales", "SHOW TABLES", "SUMMARIZE sales",
    # The connection's own schema, named: the same listing as SHOW TABLES.
    "SHOW TABLES FROM main", "SHOW TABLES FROM memory", "SHOW TABLES FROM memory.main",
])
def test_describe_show_and_summarize_are_a_fixable_error_that_names_the_tables(sql):
    """They read only names the refusal already gives, so they are not a
    policy refusal: the message lists the connection's tables instead."""
    with pytest.raises(ValueError) as info:
        check_duckdb_file_query(sql, _VIEWS)
    assert _not_a_policy_refusal(info.value), type(info.value).__mro__
    assert "customers" in str(info.value) and "sales" in str(info.value)


@pytest.mark.parametrize("sql, refusal", [
    ("SHOW TABLES FROM other_schema", TableScopeViolation),
    ("SHOW TABLES FROM other_db.main", TableScopeViolation),
    ("SHOW TABLES FROM memory.other_schema", TableScopeViolation),
    ("SHOW TABLES FROM main.memory", TableScopeViolation),
    ("SHOW ALL TABLES", ReadOnlyViolation),
])
def test_a_show_of_another_schema_or_of_every_database_is_refused_by_policy(sql, refusal):
    """They name a target beyond the connection's own tables."""
    with pytest.raises(refusal):
        check_duckdb_file_query(sql, _VIEWS)


def test_with_data_hidden_a_table_refusal_keeps_the_list_whatever_the_name():
    """Redaction cuts a refusal at the first quote, ': ' or ' (' of the
    name the agent wrote; the tables list comes before it."""
    from app.ai.data_visibility import redact_error_text

    for name in ('"sa (les"', '"sa: les"', "\"sa'les\""):
        with pytest.raises(TableScopeViolation) as info:
            check_duckdb_file_query(f"SELECT * FROM {name}", _VIEWS)
        assert "customers, sales" in redact_error_text(str(info.value), "TableAccessError"), str(info.value)


def test_inspecting_reports_whether_the_statement_describes():
    assert inspect_duckdb_sql("SELECT * FROM (DESCRIBE sales) d").describes
    assert not inspect_duckdb_sql("SELECT * FROM sales").describes


# A DESCRIBE / SHOW / SUMMARIZE is a fixable error only when nothing else in
# the statement is refused: every policy verdict comes before it.
_DESCRIBE_BESIDE_A_REFUSAL = [
    ("SELECT * FROM (DESCRIBE sales) d WHERE getenv('HOME') IS NOT NULL", ReadOnlyViolation),
    ("SELECT * FROM (SHOW TABLES) s, duckdb_tables()", ReadOnlyViolation),
    ("DESCRIBE SELECT * FROM read_csv_auto('other.csv')", ReadOnlyViolation),
    ("SELECT * FROM (DESCRIBE sales) d, secret", TableScopeViolation),
    ("DESCRIBE SELECT * FROM secret", TableScopeViolation),
    ("WITH pg_settings AS (SELECT 1 AS x) SELECT * FROM (SUMMARIZE sales) d, pg_settings", TableScopeViolation),
]


@pytest.mark.parametrize("sql, refusal", _DESCRIBE_BESIDE_A_REFUSAL)
def test_a_describe_does_not_hide_a_policy_refusal_of_the_rest_of_the_statement(sql, refusal):
    with pytest.raises(refusal):
        check_duckdb_file_query(sql, _VIEWS)


def test_a_cte_name_that_is_not_plain_is_a_fixable_error():
    """Nothing in it is refused by policy: the agent renames the CTE."""
    with pytest.raises(ValueError, match="plain") as info:
        check_duckdb_file_query('WITH "my cte" AS (SELECT * FROM sales) SELECT * FROM "my cte"', _VIEWS)
    assert _not_a_policy_refusal(info.value), type(info.value).__mro__


@pytest.mark.parametrize("sql", [
    # quoted CTE names never let the statement through
    'WITH "x.csv" AS (SELECT 1) SELECT * FROM "x.csv"',
    'WITH "My CTE" AS (SELECT 1 AS x) SELECT * FROM "my cte"',
])
def test_a_statement_with_a_cte_name_that_is_not_plain_never_passes(sql):
    with pytest.raises(ValueError):
        check_duckdb_file_query(sql, _VIEWS)


@pytest.mark.parametrize("sql, refusal", [
    ('WITH "my cte" AS (SELECT 1 AS x) SELECT * FROM "my cte", secret', TableScopeViolation),
    ('SELECT * FROM (WITH "x.csv" AS (SELECT 1 AS x) SELECT * FROM "x.csv") s, "x.csv"', TableScopeViolation),
    ('WITH "my cte" AS (SELECT 1 AS x) SELECT getenv(\'HOME\') FROM "my cte"', ReadOnlyViolation),
])
def test_a_cte_name_that_is_not_plain_does_not_hide_a_policy_refusal(sql, refusal):
    with pytest.raises(refusal):
        check_duckdb_file_query(sql, _VIEWS)


def test_a_query_nested_too_deeply_gets_a_hint_the_connection_can_follow():
    """DuckDB's own hint is a SET, which this connection refuses."""
    with pytest.raises(ValueError) as info:
        check_duckdb_file_query("SELECT " + "+".join(["1"] * 1100), _VIEWS)
    assert _not_a_policy_refusal(info.value), type(info.value).__mro__
    text = str(info.value)
    assert "SET" not in text and "max_expression_depth" not in text
    assert "nests too deeply" in text


def _serializing_as(monkeypatch, tree):
    import app.ai.code_execution.duckdb_guard as guard
    monkeypatch.setattr(guard, "_serialize", lambda sql: tree)


@pytest.mark.parametrize("tree", [
    {"error": False, "statements": [{}]},
    {"error": False, "statements": [{"node": {}}]},
    {"error": False, "statements": [{"node": {"type": "SOME_FUTURE_NODE"}}]},
    {"error": False, "statements": ["SELECT 1"]},
])
def test_a_statement_whose_root_is_not_a_query_node_is_refused(monkeypatch, tree):
    _serializing_as(monkeypatch, tree)
    with pytest.raises(ReadOnlyViolation):
        check_duckdb_file_query("SELECT 1", _VIEWS)


def test_a_broken_inspector_connection_is_logged_and_rebuilt(monkeypatch, caplog):
    """The statement that met it is refused; the next one gets a working
    inspector instead of the same failure for the life of the process."""
    import logging

    import app.ai.code_execution.duckdb_guard as guard

    closed = duckdb.connect(":memory:")
    closed.close()
    monkeypatch.setattr(guard, "_CONNECTION", closed)
    logging.getLogger(guard.__name__).disabled = False
    with caplog.at_level(logging.ERROR, logger=guard.__name__):
        with pytest.raises(ReadOnlyViolation):
            inspect_duckdb_sql("SELECT * FROM sales")
    errors = [r for r in caplog.records if r.name == guard.__name__ and r.levelno == logging.ERROR]
    assert errors and "ConnectionException" in errors[0].getMessage()
    assert inspect_duckdb_sql("SELECT * FROM sales").tables == frozenset({("", "", "sales")})


# ------------------------------------------------------------ the check

_VIEWS = ["sales", "customers"]
# A USING KEY recursive CTE whose recursive part also reads the rows kept so
# far (recurring.r); it stops after n = 3.
_KEYED_RECURSIVE = (
    "WITH RECURSIVE r USING KEY (n) AS (SELECT 1 AS n UNION "
    "SELECT r.n + 1 FROM r JOIN recurring.r k ON k.n = r.n WHERE r.n < 3) SELECT n FROM r ORDER BY n"
)


@pytest.mark.parametrize("sql", [
    "SELECT * FROM sales",
    "SELECT * FROM SALES s JOIN Customers c ON s.id = c.id",
    "SELECT * FROM main.sales UNION ALL SELECT id, id FROM memory.main.customers",
    "WITH t AS (SELECT * FROM sales) SELECT * FROM t",
    "WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM r WHERE n < 3) SELECT * FROM r",
    "SELECT * FROM range(3)",
    "SELECT * FROM generate_series(1, 3)",
    "SELECT * FROM unnest([1, 2])",
    "SELECT * FROM (VALUES (1), (2)) v(x)",
    "SELECT (SELECT max(amount) FROM sales) AS m",
])
def test_a_select_over_the_connections_tables_passes(sql):
    check_duckdb_file_query(sql, _VIEWS)


@pytest.mark.parametrize("sql", [
    "SELECT * FROM secret",
    "SELECT * FROM 'other.csv'",
    "SELECT * FROM other_schema.sales",
    "SELECT * FROM other_db.main.sales",
    "SELECT * FROM information_schema.tables",
    "SELECT * FROM sales WHERE id IN (SELECT id FROM secret)",
    "WITH t AS (SELECT 1) SELECT * FROM main.t",
])
def test_a_table_that_is_not_the_connections_is_refused(sql):
    with pytest.raises(TableScopeViolation):
        check_duckdb_file_query(sql, _VIEWS)


@pytest.mark.parametrize("sql", [
    "SELECT * FROM read_csv_auto('other.csv')",
    "SELECT * FROM read_parquet('x.parquet')",
    "SELECT * FROM parquet_scan('x.parquet')",
    "SELECT * FROM glob('*')",
    "SELECT * FROM query('SELECT 1')",
    "SELECT * FROM query_table('sales')",
    "SELECT * FROM duckdb_settings()",
    "SELECT * FROM duckdb_secrets()",
    "SELECT * FROM duckdb_extensions()",
    "SELECT * FROM duckdb_tables()",
    "SELECT current_setting('allowed_paths')",
    "SELECT getenv('HOME')",
    "SELECT * FROM sales WHERE id = (SELECT count(*) FROM read_text('x'))",
    "SELECT 1; SELECT 2",
    "COPY (SELECT 1) TO 'x.csv'",
])
def test_file_readers_settings_and_statements_are_refused(sql):
    with pytest.raises(ReadOnlyViolation):
        check_duckdb_file_query(sql, _VIEWS)


# Names that look like one of the statement's CTEs but, where they stand,
# DuckDB binds to the catalog: a CTE is visible only in its own scope, to the
# CTEs after it, and to itself only in the recursive part of a recursive one.
_NOT_IN_CTE_SCOPE = [
    # a non-recursive CTE whose body names itself
    "WITH other AS (SELECT * FROM other) SELECT * FROM other",
    "WITH pg_settings AS (SELECT * FROM pg_settings) SELECT * FROM pg_settings",
    "WITH RECURSIVE pg_settings AS (SELECT * FROM pg_settings) SELECT * FROM pg_settings",
    # the anchor of a recursive CTE does not see the CTE either
    "WITH RECURSIVE duckdb_views AS (SELECT sql FROM duckdb_views UNION ALL SELECT sql FROM duckdb_views) "
    "SELECT * FROM duckdb_views",
    # a CTE named by an earlier CTE of the same WITH
    "WITH a AS (SELECT * FROM pg_tables), pg_tables AS (SELECT 1 AS x) SELECT * FROM a",
    # a CTE defined in a nested scope and named outside it
    "SELECT * FROM (WITH other AS (SELECT 1 AS x) SELECT * FROM other) s, other",
    "SELECT * FROM (WITH sqlite_master AS (SELECT 1 AS x) SELECT * FROM sqlite_master) s, sqlite_master",
    "SELECT id FROM sales WHERE id IN (WITH duckdb_databases AS (SELECT 1 AS id) SELECT id FROM duckdb_databases) "
    "UNION ALL SELECT 1 FROM duckdb_databases",
    "WITH x AS (WITH duckdb_databases AS (SELECT 1 AS d) SELECT * FROM duckdb_databases) "
    "SELECT * FROM x, duckdb_databases",
    "SELECT * FROM sales UNION ALL (WITH pg_views AS (SELECT 1, 2) SELECT * FROM pg_views) "
    "UNION ALL SELECT 1, 2 FROM pg_views",
    # recurring.<cte> names a CTE only in the recursive part of a USING KEY CTE
    "WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM recurring.r WHERE n < 3) SELECT * FROM r",
    "WITH RECURSIVE r USING KEY (n) AS (SELECT * FROM recurring.r UNION SELECT n + 1 FROM r WHERE n < 3) "
    "SELECT * FROM r",
    "WITH RECURSIVE r USING KEY (n) AS (SELECT 1 AS n UNION SELECT n + 1 FROM r WHERE n < 3) "
    "SELECT * FROM recurring.r",
    "WITH RECURSIVE r USING KEY (n) AS (SELECT 1 AS n UNION SELECT n + 1 FROM recurring.other WHERE n < 3) "
    "SELECT * FROM r",
]


@pytest.mark.parametrize("sql", _NOT_IN_CTE_SCOPE)
def test_a_name_outside_its_ctes_scope_is_refused(sql):
    with pytest.raises(TableScopeViolation):
        check_duckdb_file_query(sql, _VIEWS)


@pytest.mark.parametrize("sql", [
    "WITH pg_settings AS (SELECT * FROM sales) SELECT * FROM pg_settings",
    "WITH duckdb_settings AS (SELECT 1 AS x) SELECT * FROM duckdb_settings",
    'WITH "SQLITE_MASTER" AS (SELECT 1 AS x) SELECT * FROM sqlite_master',
])
def test_a_cte_named_after_a_catalog_object_is_refused(sql):
    with pytest.raises(TableScopeViolation):
        check_duckdb_file_query(sql, _VIEWS)


@pytest.mark.parametrize("sql", [
    "SELECT pg_get_viewdef(i) FROM range(3) t(i)",
    "SELECT pg_catalog.pg_get_viewdef(i) FROM range(3) t(i)",
    "SELECT format_type(i, 0) FROM range(3) t(i)",
    "SELECT pg_get_constraintdef(i) FROM range(3) t(i)",
    "SELECT get_block_size('memory')",
])
def test_a_built_in_macro_that_reads_the_catalog_is_refused(sql):
    with pytest.raises(ReadOnlyViolation):
        check_duckdb_file_query(sql, _VIEWS)


def _catalog():
    con = duckdb.connect(":memory:")
    try:
        macros = [r[0] for r in con.execute(
            "SELECT DISTINCT function_name FROM duckdb_functions() "
            "WHERE function_type = 'macro' AND macro_definition ILIKE '%from%' ORDER BY 1").fetchall()]
        table_macros = [r[0] for r in con.execute(
            "SELECT DISTINCT function_name FROM duckdb_functions() WHERE function_type = 'table_macro' "
            "ORDER BY 1").fetchall()]
        views = [r[0] for r in con.execute(
            "SELECT DISTINCT view_name FROM duckdb_views() WHERE internal "
            "AND schema_name IN ('main', 'pg_catalog') ORDER BY 1").fetchall()]
    finally:
        con.close()
    return macros, table_macros, views


def test_every_catalog_reading_macro_and_catalog_view_of_this_duckdb_is_refused():
    """Enumerated from the installed DuckDB, so an upgrade that adds one
    fails here instead of passing the check."""
    macros, table_macros, views = _catalog()
    assert {"pg_get_viewdef", "format_type"} <= set(macros) and "pg_settings" in views
    for name in macros:
        with pytest.raises(ReadOnlyViolation):
            check_duckdb_file_query(f'SELECT "{name}"(i) FROM range(1) t(i)', _VIEWS)
    for name in table_macros:
        with pytest.raises(ReadOnlyViolation):
            check_duckdb_file_query(f'SELECT * FROM "{name}"(1)', _VIEWS)
    for name in views:
        with pytest.raises(TableScopeViolation):
            check_duckdb_file_query(f'SELECT * FROM "{name}"', _VIEWS)
        with pytest.raises(TableScopeViolation):
            check_duckdb_file_query(f'WITH "{name}" AS (SELECT 1 AS x) SELECT * FROM "{name}"', _VIEWS)


@pytest.mark.parametrize("sql", [
    "WITH t AS (SELECT * FROM sales) SELECT * FROM t",
    "WITH a AS (SELECT * FROM sales), b AS (SELECT * FROM a) SELECT * FROM b",
    "WITH sales AS (SELECT * FROM sales WHERE amount > 10) SELECT * FROM sales",
    "WITH t AS (SELECT * FROM sales) SELECT (SELECT count(*) FROM t) AS n FROM t",
    "WITH t AS (SELECT * FROM sales) SELECT * FROM (WITH u AS (SELECT * FROM t) SELECT * FROM u)",
    "WITH t AS (SELECT 1 AS v) SELECT * FROM (WITH t AS (SELECT * FROM t) SELECT * FROM t)",
    "WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM r WHERE n < 3) SELECT * FROM r",
    "WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM (SELECT * FROM r) q WHERE n < 3), "
    "s AS (SELECT * FROM r) SELECT * FROM s",
    "WITH RECURSIVE r USING KEY (n) AS (SELECT 1 AS n UNION SELECT n + 1 FROM r WHERE n < 3) SELECT * FROM r",
    _KEYED_RECURSIVE,
    "WITH t AS (SELECT * FROM sales) SELECT * FROM sales UNION ALL SELECT * FROM t",
])
def test_ctes_in_their_scope_pass(sql):
    check_duckdb_file_query(sql, _VIEWS)


def _chained_ctes(n: int) -> str:
    body = ", ".join(f"c{i} AS (SELECT * FROM c{i - 1})" for i in range(1, n))
    return f"WITH c0 AS (SELECT * FROM sales), {body} SELECT * FROM c{n - 1}"


def test_checking_cte_scope_grows_linearly_with_the_statement(monkeypatch):
    """Each CTE sees every one before it; the check must not copy that set
    per scope. Timed on DuckDB's own trees, with the parse done up front."""
    import time

    import app.ai.code_execution.duckdb_guard as guard

    small, large = 300, 3000
    trees = {n: guard._serialize(_chained_ctes(n)) for n in (small, large)}

    def best(n):
        monkeypatch.setattr(guard, "_serialize", lambda sql: trees[n])
        times = []
        for _ in range(3):
            t0 = time.perf_counter()
            check_duckdb_file_query("SELECT 1", _VIEWS)
            times.append(time.perf_counter() - t0)
        return min(times)

    ratio = best(large) / best(small)
    assert ratio < 30, ratio  # linear is about 10, quadratic about 100


def test_a_view_of_the_connection_that_shares_a_catalog_name_is_its_own_table():
    """The connection's own view comes first in DuckDB's lookup, so a file
    named like a catalog object is still the connection's table."""
    check_duckdb_file_query("SELECT * FROM pg_settings", ["pg_settings"])


def test_inspecting_from_many_threads_gives_the_serial_verdicts():
    texts = [
        "SELECT * FROM sales",
        "SELECT * FROM secret",
        "COPY sales TO 'x.csv'",
        "WITH t AS (SELECT 1 AS a) SELECT * FROM t",
        "SELECT * FROM read_csv('x')",
        "SELECT 1; SELECT 2",
    ]

    def verdict(sql):
        try:
            check_duckdb_file_query(sql, _VIEWS)
            return "ok"
        except TableScopeViolation:
            return "TableScopeViolation"
        except ReadOnlyViolation:
            return "ReadOnlyViolation"

    serial = [verdict(t) for t in texts]
    assert serial == ["ok", "TableScopeViolation", "ReadOnlyViolation", "ok", "ReadOnlyViolation",
                      "ReadOnlyViolation"]
    results: list = []
    unexpected: list = []
    lock = threading.Lock()

    def worker(seed):
        for i in range(50):
            k = (seed + i) % len(texts)
            try:
                got = verdict(texts[k])
            except BaseException as err:  # noqa: BLE001 - reported to the assertion
                with lock:
                    unexpected.append(repr(err))
                continue
            with lock:
                results.append((k, got))

    threads = [threading.Thread(target=worker, args=(s,)) for s in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert unexpected == []
    assert len(results) == 16 * 50
    assert all(got == serial[k] for k, got in results)


# ------------------------------------------------------------ the locked engine

def _view_over(con, path, name="a"):
    con.execute(f"CREATE VIEW {name} AS SELECT * FROM read_csv_auto('{_sq(path)}')")


def test_a_locked_connection_reads_its_own_files_only(tmp_path, outside):
    own = tmp_path / "own.csv"
    _SALES.to_csv(own, index=False)
    con = duckdb.connect(":memory:")
    try:
        _view_over(con, own)
        lock_down(con, allowed_paths=[str(own)])
        assert con.execute("SELECT sum(amount) FROM a").fetchone()[0] == 60
        for sql in (
            f"SELECT * FROM read_csv_auto('{_sq(outside)}')",
            f"ATTACH '{_sq(tmp_path / 'x.db')}'",
            "INSTALL httpfs",
            "LOAD httpfs",
            f"SELECT * FROM glob('{_sq(tmp_path)}/*')",
        ):
            with pytest.raises(duckdb.Error):
                con.execute(sql)
        before = own.read_bytes()
        with pytest.raises(duckdb.Error):
            con.execute(f"COPY (SELECT 1 AS id, 2 AS amount) TO '{_sq(own)}'")
        assert own.read_bytes() == before
        for sql in ("SET enable_external_access = true", "RESET allowed_paths", "SET allowed_paths = []"):
            with pytest.raises(duckdb.Error):
                con.execute(sql)
    finally:
        con.close()


def test_lock_down_that_cannot_lock_raises():
    con = duckdb.connect(":memory:", config={"lock_configuration": True})
    try:
        with pytest.raises(RuntimeError):
            lock_down(con, allowed_paths=["/nowhere.csv"])
    finally:
        con.close()


# ------------------------------------------------------------ through each client

def test_each_connection_answers_a_select_over_its_tables(files):
    _kind, _client, run = files
    df = run("SELECT c.name, sum(s.amount) AS total FROM sales s JOIN customers c ON s.id = c.id "
             "GROUP BY 1 ORDER BY 1")
    assert df.to_dict("records") == [{"name": "a", "total": 10}, {"name": "b", "total": 20}]
    df = run("WITH t AS (SELECT * FROM sales) SELECT count(*) AS n FROM t, range(2)")
    assert df["n"].tolist() == [6]
    assert run(_KEYED_RECURSIVE)["n"].tolist() == [1, 2, 3]


@pytest.mark.parametrize("sql", [
    "SELECT * FROM (DESCRIBE sales) d, duckdb_tables()",
    "SELECT * FROM (DESCRIBE sales) d WHERE getenv('HOME') IS NOT NULL",
    "SELECT * FROM (DESCRIBE sales) d, secret",
])
def test_each_connection_refuses_by_policy_what_sits_beside_a_describe(files, sql):
    _kind, _client, run = files
    with pytest.raises((ReadOnlyViolation, TableScopeViolation)):
        run(sql)


def test_each_connection_refuses_another_file_with_the_refusals_own_type(files, outside):
    _kind, _client, run = files
    with pytest.raises(TableScopeViolation):
        run(f"SELECT * FROM '{_sq(outside)}'")
    with pytest.raises(ReadOnlyViolation):
        run(f"SELECT * FROM read_csv_auto('{_sq(outside)}')")
    with pytest.raises(ReadOnlyViolation):
        run("SELECT current_setting('allowed_paths')")


def test_each_connection_refuses_a_copy_onto_its_own_file(files):
    kind, client, run = files
    with pytest.raises(ReadOnlyViolation):
        run("COPY (SELECT 1 AS id) TO 'copied.csv'")


@pytest.mark.parametrize("sql", [
    "WITH pg_settings AS (SELECT * FROM pg_settings) SELECT name, setting FROM pg_settings",
    "SELECT * FROM (WITH duckdb_databases AS (SELECT 1 AS x) SELECT * FROM duckdb_databases) s, duckdb_databases",
    "SELECT pg_get_viewdef(i) AS d FROM range(1) t(i)",
])
def test_each_connection_refuses_catalog_reads_that_look_like_its_own_names(files, sql):
    """On the real locked connection, not only in the check: the engine
    would answer these from its catalog, so the check is what refuses them."""
    _kind, _client, run = files
    with pytest.raises((TableScopeViolation, ReadOnlyViolation)):
        run(sql)


def test_each_connection_names_its_tables_when_refusing_an_unknown_one(files):
    _kind, _client, run = files
    with pytest.raises(TableScopeViolation) as info:
        run("SELECT * FROM salez")
    assert "salez" in str(info.value)
    assert "customers" in str(info.value) and "sales" in str(info.value)


@pytest.mark.parametrize("sql", ["DESCRIBE sales", "SHOW TABLES", "SUMMARIZE sales"])
def test_each_connection_answers_describe_with_a_fixable_error(files, sql):
    _kind, _client, run = files
    with pytest.raises(ValueError) as info:
        run(sql)
    assert _not_a_policy_refusal(info.value), type(info.value).__mro__
    assert "customers" in str(info.value) and "sales" in str(info.value)


def test_a_qvd_table_whose_cache_is_not_built_is_named_as_not_ready(tmp_path, monkeypatch):
    client = _qvd(tmp_path, monkeypatch)
    pending = tmp_path / "qvd" / "orders.qvd"
    pending.write_bytes(b"qvd")  # listed by discovery, no Parquet cache yet
    assert "orders" in [t.name for t in client.get_tables()]
    with pytest.raises(TableScopeViolation, match="not built yet") as info:
        client.execute_query("SELECT * FROM orders")
    assert "orders" in str(info.value)
    assert client.execute_query("SELECT count(*) AS n FROM sales")["n"].tolist() == [3]


def test_a_pbix_table_whose_cache_could_not_be_built_is_named_as_not_ready(tmp_path, monkeypatch):
    """A second file whose model discovery already read, but whose Parquet
    cache the query path cannot build (here: over the size limit)."""
    client = _pbix(tmp_path, monkeypatch)
    pending = tmp_path / "pending.pbix"
    pending.write_bytes(b"pbix-pending")
    pdir = client._cache_key(str(pending))[1]
    pdir.mkdir(parents=True)
    (pdir / "schema.json").write_text(json.dumps(
        {"tables": [{"name": "orders", "columns": [{"name": "id", "dtype": "int64"}]}], "relationships": []}))
    client = PBIXClient(f"{client.file_paths_raw}\n{pending}")
    monkeypatch.setattr(pbix_client_mod, "PBIX_MAX_BYTES", len(b"pbix-pending") - 1)
    assert "orders" in [t.name for t in client.get_tables()]
    with pytest.raises(TableScopeViolation, match="not built yet") as info:
        client.execute_query("SELECT * FROM orders")
    assert "orders" in str(info.value)
    assert client.execute_query("SELECT count(*) AS n FROM sales")["n"].tolist() == [3]


# ------------------------------------------------------------ the code loop

_COUNT = "SELECT count(*) AS n FROM sales"


def _code(statement: str) -> str:
    return f"def generate_df(ds_clients, excel_files):\n    return ds_clients['files'].execute_query({statement!r})\n"


class _DataHidden:
    """Organization settings with "Allow LLM to see data" off; every other
    setting is left at its default."""

    class _Off:
        value = False

    def get_config(self, key, default=None):
        return self._Off() if key == "allow_llm_see_data" else default


def _code_loop(client, statements, organization_settings=None):
    """generate_and_execute with the coder (the LLM) stubbed: attempt i
    gets the code for statements[i]. Everything below the coder is real."""
    return _program_loop(client, [_code(s) for s in statements], organization_settings)


def _program_loop(client, programs, organization_settings=None):
    """As _code_loop, with attempt i getting programs[i] as written."""
    import asyncio

    from app.ai.schemas.codegen import CodeGenContext, CodeGenRequest

    calls = []

    async def coder(**kwargs):
        calls.append(kwargs)
        return programs[min(len(calls), len(programs)) - 1]

    async def go():
        executor = StreamingCodeExecutor(organization_settings=organization_settings)
        return [ev async for ev in executor.generate_and_execute_stream_v2(
            request=CodeGenRequest(context=CodeGenContext(user_prompt="x", schemas_excerpt=""), retries=3),
            ds_clients={"files": client}, excel_files=[], code_generator_fn=coder,
        )]

    events = asyncio.run(go())
    return events[-1]["payload"], events, calls


@pytest.mark.parametrize("first, hint", [
    ("SELEC count(*) AS n FROM sales", "syntax error"),
    ("DESCRIBE sales", "sales"),
    ('WITH "my cte" AS (SELECT * FROM sales) SELECT count(*) AS n FROM "my cte"', "plain"),
])
def test_the_code_loop_retries_a_fixable_duckdb_error_and_gets_rows(mode, tmp_path, first, hint):
    done, events, calls = _code_loop(_csv(tmp_path), [first, _COUNT])
    assert len(calls) == 2
    assert done["df"]["n"].tolist() == [3]
    assert not [e for e in events if e["type"] == "security_violation"]
    feedback = calls[1]["code_and_error_messages"][-1][1]
    assert "Security violation" not in feedback
    assert hint in feedback


def test_the_code_loop_ends_on_a_refused_read_beside_a_describe(mode, tmp_path):
    """Audited as a security refusal and not retried, as without the DESCRIBE."""
    done, events, calls = _code_loop(_csv(tmp_path), ["SELECT * FROM (DESCRIBE sales) d, duckdb_tables()", _COUNT])
    assert len(calls) == 1
    assert done["df"] is None
    assert [e["payload"]["violation_type"] for e in events if e["type"] == "security_violation"] == ["unsafe_sql"]


# The last names a table whose name redaction cuts: the name the agent wrote
# is hidden, and the list still reaches the coder and the planner.
_CUT_NAME = 'SELECT * FROM "sa (les"'
_LISTS_THE_TABLES = ["DESCRIBE sales", "SELECT * FROM salez", _CUT_NAME]


@pytest.mark.parametrize("statement", _LISTS_THE_TABLES)
def test_with_data_hidden_the_retry_feedback_keeps_the_table_list(mode, tmp_path, statement):
    """Guard messages name only structure: with "Allow LLM to see data" off
    the coder still gets the connection's tables."""
    from app.ai.code_execution.code_execution import format_execution_failure

    with pytest.raises(Exception) as info:
        _sandbox_run("csv", _csv(tmp_path), statement)
    feedback = format_execution_failure(info.value, f"Execution error: {info.value}", allow_llm_see_data=False)
    assert "customers, sales" in feedback, feedback
    assert ("<value hidden>" in feedback) is (statement == _CUT_NAME), feedback


@pytest.mark.parametrize("statement", _LISTS_THE_TABLES)
def test_with_data_hidden_the_planner_and_its_replay_keep_the_table_list(mode, tmp_path, statement):
    from app.ai.context.hidden_data import hide_data_for_replay
    from app.ai.tools.implementations.create_data import CreateDataTool

    done, _events, _calls = _code_loop(_csv(tmp_path), [statement], organization_settings=_DataHidden())
    observation = CreateDataTool.build_execution_failure_observation(done["errors"], done["query_timings"])
    assert "customers, sales" in observation["error"]["db_message"], observation
    replayed = hide_data_for_replay({"observation": observation}, "create_data")["observation"]
    assert "customers, sales" in replayed["error"]["db_message"], replayed


def test_a_fixable_error_reaches_the_planner_with_its_whole_table_list(mode, tmp_path):
    from app.ai.tools.implementations.create_data import CreateDataTool

    _csv(tmp_path)
    for i in range(8):
        pd.DataFrame({"x": [1]}).to_csv(tmp_path / "csv" / f"regional_target_{i}.csv", index=False)
    client = CSVClient(file_paths=f"{tmp_path / 'csv'}/*.csv")
    done, _events, _calls = _code_loop(client, ["DESCRIBE sales"])
    observation = CreateDataTool.build_execution_failure_observation(done["errors"], done["query_timings"])
    # The connection's tables sort as customers, regional_target_0..7, sales.
    assert "regional_target_7, sales." in observation["error"]["db_message"], observation


def test_the_code_loop_still_ends_on_a_copy_without_a_retry(mode, tmp_path):
    done, events, calls = _code_loop(_csv(tmp_path), ["COPY (SELECT 1) TO 'copied.csv'", _COUNT])
    assert len(calls) == 1
    assert done["df"] is None
    assert [e["payload"]["violation_type"] for e in events if e["type"] == "security_violation"] == ["unsafe_sql"]


# Code that catches a refusal and carries on. The refusal still ends the
# attempt: no retry, no rows, and it is audited as when it escapes.
_REFUSED = {
    "table_access": "SELECT * FROM salez",
    "unsafe_sql": "COPY (SELECT 1) TO 'copied.csv'",
}
_AFTER_THE_CATCH = {
    "raises_a_fixable_error": "    return ds_clients['files'].execute_query('SELEC 1')\n",
    "returns_rows": f"    return ds_clients['files'].execute_query({_COUNT!r})\n",
    "returns_none": "    return None\n",
}


def _catching(statement: str, after: str) -> str:
    return (
        "def generate_df(ds_clients, excel_files):\n"
        "    try:\n"
        f"        ds_clients['files'].execute_query({statement!r})\n"
        "    except Exception:\n"
        "        pass\n"
        f"{after}"
    )


@pytest.mark.parametrize("after", sorted(_AFTER_THE_CATCH))
@pytest.mark.parametrize("violation", sorted(_REFUSED))
def test_a_refusal_the_code_catches_still_ends_the_attempt_as_a_security_violation(mode, tmp_path, violation, after):
    program = _catching(_REFUSED[violation], _AFTER_THE_CATCH[after])
    done, events, calls = _program_loop(_csv(tmp_path), [program, _code(_COUNT)])
    assert len(calls) == 1
    assert [e["payload"]["violation_type"] for e in events if e["type"] == "security_violation"] == [violation]
    assert done["df"] is None
    assert done["errors"] and "Security violation" in done["errors"][-1][1]


def test_a_fixable_error_the_code_catches_is_still_retried(mode, tmp_path):
    """No refusal was recorded: a caught fixable error, then another one,
    gets a new attempt and no security event."""
    program = _catching("SELEC 1", "    return ds_clients['files'].execute_query('DESCRIBE sales')\n")
    done, events, calls = _program_loop(_csv(tmp_path), [program, _code(_COUNT)])
    assert len(calls) == 2
    assert not [e for e in events if e["type"] == "security_violation"]
    assert done["df"]["n"].tolist() == [3]


def _create_data_run(client, program, monkeypatch):
    """CreateDataTool's public stream with the coder stubbed and the audit
    writer recorded; the code loop and the connection are real."""
    import asyncio

    from app.ai.tools.implementations import create_data as create_data_module

    audits = []

    class _Coder:
        def __init__(self, **_kwargs):
            self.reasoning_ms = None

        async def generate_code(self, **_kwargs):
            return program

    async def _audit(_runtime_ctx, action, *args, **kwargs):
        audits.append((action, kwargs.get("details") or (args[2] if len(args) > 2 else None)))

    class _SchemaBuilder:
        """The connection's schema, as the context hub would build it."""

        async def build(self, **_kwargs):
            ds = SimpleNamespace(info=SimpleNamespace(id="ds-files"), tables=[SimpleNamespace(name="sales")])
            return SimpleNamespace(data_sources=[ds], render_combined=lambda **_kw: "sales(id, amount)")

    monkeypatch.setattr(create_data_module, "Coder", _Coder)
    monkeypatch.setattr(create_data_module, "log_tool_audit", _audit)
    runtime_ctx = {
        "ds_clients": {"files": client}, "excel_files": [],
        "context_hub": SimpleNamespace(schema_builder=_SchemaBuilder(), db=None),
    }

    async def go():
        tool = create_data_module.CreateDataTool()
        return [e async for e in tool.run_stream({
            "title": "Sales", "user_prompt": "Count sales", "interpreted_prompt": "Count sales",
            "tables_by_source": [{"data_source_id": "ds-files", "tables": ["sales"]}],
        }, runtime_ctx)]

    return asyncio.run(go()), audits


@pytest.mark.parametrize("caught", [False, True], ids=["escaped", "caught"])
@pytest.mark.parametrize("violation", sorted(_REFUSED))
def test_create_data_audits_a_refusal_whether_or_not_the_code_caught_it(mode, tmp_path, monkeypatch, violation, caught):
    program = (_catching(_REFUSED[violation], _AFTER_THE_CATCH["returns_rows"]) if caught
               else _code(_REFUSED[violation]))
    events, audits = _create_data_run(_csv(tmp_path), program, monkeypatch)
    blocked = [details for action, details in audits if action == "security.unsafe_sql_blocked"]
    assert [d["violation_type"] for d in blocked] == [violation], audits
    assert events[-1].payload["output"]["success"] is False


def test_a_file_connections_unknown_table_gets_guidance_about_its_own_tables(mode, tmp_path):
    """The table-selection wording ("refused by the agent's table
    selection") is for SQL connections; a file connection has no table
    selection, only its own tables."""
    from app.ai.tools.implementations.create_data import CreateDataTool

    done, _events, _calls = _code_loop(_csv(tmp_path), ["SELECT * FROM salez"])
    observation = CreateDataTool.build_execution_failure_observation(done["errors"], done["query_timings"])
    assert observation["error"]["type"] == "table_access_denied"
    assert "sales" in observation["error"]["message"]
    guidance = observation["retry_guidance"]
    assert "table selection" not in guidance
    assert "this connection" in guidance


@pytest.mark.parametrize("kind", ["csv", "qvd", "pbix"])
def test_the_query_connection_is_locked_before_any_statement(kind, tmp_path, monkeypatch, outside):
    """Statements that skip the check meet a locked engine: the client's
    own connection cannot read another file, write its own, or unlock."""
    client = {"csv": lambda: _csv(tmp_path), "qvd": lambda: _qvd(tmp_path, monkeypatch),
              "pbix": lambda: _pbix(tmp_path, monkeypatch)}[kind]()
    # A file one of the connection's tables reads.
    own = next(p for p in sorted(tmp_path.rglob("*"))
               if p.suffix in (".csv", ".parquet") and "outside" not in p.parts)
    before = own.read_bytes()
    with client.connect() as con:
        assert con.execute("SELECT count(*) FROM sales").fetchone()[0] == 3
        with pytest.raises(duckdb.Error):
            con.execute(f"SELECT * FROM read_csv_auto('{_sq(outside)}')")
        with pytest.raises(duckdb.Error):
            con.execute(f"COPY (SELECT 1) TO '{_sq(own)}'")
        with pytest.raises(duckdb.Error):
            con.execute("SET enable_external_access = true")
    assert own.read_bytes() == before


@pytest.mark.parametrize("kind", ["csv", "qvd", "pbix"])
def test_a_connection_that_cannot_be_locked_is_never_handed_out(kind, tmp_path, monkeypatch):
    client = {"csv": lambda: _csv(tmp_path), "qvd": lambda: _qvd(tmp_path, monkeypatch),
              "pbix": lambda: _pbix(tmp_path, monkeypatch)}[kind]()
    module = {"csv": csv_client_mod, "qvd": qvd_client_mod, "pbix": pbix_client_mod}[kind]
    real_connect = duckdb.connect

    def connect_locked(*args, **kwargs):
        return real_connect(":memory:", config={"lock_configuration": True})

    monkeypatch.setattr(module.duckdb, "connect", connect_locked)
    with pytest.raises(RuntimeError, match="Error connecting to"):
        with client.connect():
            raise AssertionError("an unlocked connection was handed out")


def _broken(kind, tmp_path, monkeypatch):
    """A connection whose one file cannot become a table."""
    if kind == "csv":
        (tmp_path / "csv").mkdir()
        (tmp_path / "csv" / "broken.csv").mkdir()  # matches the pattern, is not a file
        return CSVClient(file_paths=f"{tmp_path}/csv/*.csv")
    if kind == "qvd":
        monkeypatch.setattr(qvd_client_mod, "_CACHE_DIR", tmp_path / "qvd_cache")
        (tmp_path / "qvd_cache").mkdir()
        src = tmp_path / "broken.qvd"
        src.write_bytes(b"qvd")
        QVDClient._cache_key(str(src))[1].write_bytes(b"not parquet")
        return QVDClient(file_paths=str(src))
    monkeypatch.setattr(pbix_client_mod, "_CACHE_DIR", tmp_path / "pbix_cache")
    f = tmp_path / "broken.pbix"
    f.write_bytes(b"pbix")
    c = PBIXClient(str(f))
    cdir = c._cache_key(str(f))[1]
    cdir.mkdir(parents=True)
    (cdir / "t.parquet").write_bytes(b"not parquet")
    (cdir / "manifest.json").write_text(json.dumps({"t": "t.parquet"}))
    return c


@pytest.mark.parametrize("kind,label", [("csv", "CSV"), ("qvd", "QVD"), ("pbix", "PBIX")])
def test_a_broken_file_still_reports_a_connection_error(kind, label, tmp_path, monkeypatch):
    client = _broken(kind, tmp_path, monkeypatch)
    with pytest.raises(RuntimeError, match=f"Error connecting to {label} files: "):
        client.execute_query("SELECT 1")


# ------------------------------------------------------------ through the sandbox

@pytest.fixture(params=[
    "inprocess",
    pytest.param("subprocess", marks=pytest.mark.skipif(not _LINUX, reason="sandbox child needs Linux")),
])
def mode(request, monkeypatch):
    monkeypatch.setenv("BOW_CODE_SANDBOX", request.param)
    if request.param == "inprocess":
        # In-process mode is for local development only; CI runs with
        # ENVIRONMENT=production, so the test names the environment itself.
        monkeypatch.setenv("ENVIRONMENT", "development")
    return request.param


def _sandbox_run(kind, client, statement: str):
    call = (f"ds_clients['files'].execute_query(query={statement!r}, table_name={_PbirsReport.TABLE!r})"
            if kind == "pbirs" else f"ds_clients['files'].execute_query({statement!r})")
    code = f"def generate_df(ds_clients, excel_files):\n    return {call}\n"
    return StreamingCodeExecutor().execute_code(code=code, ds_clients={"files": client}, excel_files=[])


def test_through_the_sandbox_a_select_runs_and_other_files_are_refused(mode, files, outside):
    kind, client, _run = files
    df, _log, _queries = _sandbox_run(kind, client, "SELECT count(*) AS n FROM sales")
    assert df["n"].tolist() == [3]
    with pytest.raises(TableAccessError):
        _sandbox_run(kind, client, f"SELECT * FROM '{_sq(outside)}'")
    with pytest.raises(UnsafeSQLError):
        _sandbox_run(kind, client, f"SELECT * FROM read_csv_auto('{_sq(outside)}')")
    with pytest.raises(UnsafeSQLError):
        _sandbox_run(kind, client, "COPY (SELECT 1) TO 'copied.csv'")
