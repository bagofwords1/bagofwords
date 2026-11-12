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
    stmt = inspect_duckdb_sql(
        "WITH recent AS (SELECT * FROM main.sales WHERE amount > 1) "
        "SELECT upper(c.name), count(*) FROM recent JOIN memory.main.customers c USING (id) "
        "JOIN range(3) r ON true GROUP BY 1"
    )
    assert stmt.cte_names == frozenset({"recent"})
    assert stmt.tables == frozenset({("", "main", "sales"), ("", "", "recent"), ("memory", "main", "customers")})
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
    "DESCRIBE sales",
    "SHOW TABLES",
    "SUMMARIZE sales",
    "EXPLAIN SELECT 1",
    "SELEC 1",
    "",
])
def test_anything_but_one_select_is_refused(sql):
    with pytest.raises(ReadOnlyViolation):
        inspect_duckdb_sql(sql)


def test_a_query_that_does_not_parse_is_refused_with_the_parsers_reason():
    with pytest.raises(ReadOnlyViolation, match="syntax error"):
        inspect_duckdb_sql("SELEC amount FROM sales")


# ------------------------------------------------------------ the check

_VIEWS = ["sales", "customers"]


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
    'WITH "x.csv" AS (SELECT 1) SELECT * FROM "x.csv"',
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
        except (ReadOnlyViolation, TableScopeViolation) as err:
            return type(err).__name__

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
