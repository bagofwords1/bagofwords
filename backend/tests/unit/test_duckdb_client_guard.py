"""DuckDB connections (DuckDBClient) run agent SQL the way DuckDB reads it.

Contract:
- a statement is read with DuckDB's own parser and must be one SELECT, or
  one of a short list of reads: SHOW TABLES / SHOW ALL TABLES / SHOW
  DATABASES, SHOW / DESCRIBE / DESC of a plain name, a few listing PRAGMAs,
  PRAGMA table_info of a plain name, and DESCRIBE or EXPLAIN of a SELECT
  that is itself allowed; anything else is refused (`ReadOnlyViolation`);
- file readers, settings reads, `query()` and table functions other than
  a few generators are refused, with or without a table selection;
- a connection over URIs reads only the views it registers
  (`DuckDBClient.view_names()`); a connection to a database file reads
  only plain table names; anything else is refused (`ReadOnlyViolation`,
  `UnsafeSQLError` through the sandbox), with or without a table selection;
- with a table selection attached, listing commands (SHOW, the listing
  PRAGMAs) and EXPLAIN are refused, and DESCRIBE / DESC / PRAGMA table_info
  and the tables of a SELECT are checked against it (`TableScopeViolation`);
- the query connection is locked to the connection's own files: a
  statement that skipped every check still cannot read another file.
  Local URIs are resolved as DuckDB resolves them (file:// stripped, `~`
  expanded, `..` and repeated slashes resolved); a glob or directory URI
  that resolves to the root, or a relative one naming no directory under
  the working directory, is refused before the connection opens;
- table selection compares names the way DuckDB folds them (ASCII
  letters only), on both sides;
- DuckDB's own catalog views are refused on both kinds of connection, and
  a CTE never stands in for one: CTE names are plain ASCII names, compared
  the way DuckDB folds names (ASCII letters only).

Real embedded DuckDB over scratch CSV and parquet files under `tmp_path`,
and a local HTTP server on 127.0.0.1 for an http URI; nothing else touches
the network.
"""

from __future__ import annotations

import functools
import http.server
import threading
from pathlib import Path
from types import SimpleNamespace

import duckdb
import pytest

from app.ai.code_execution.code_execution import StreamingCodeExecutor, TableAccessError, UnsafeSQLError
from app.ai.code_execution.duckdb_guard import DuckdbQueryError, DuckdbSyntaxError
from app.ai.code_execution.sql_guard import ReadOnlyViolation, ensure_read_only
from app.ai.code_execution.table_scope import (
    TABLE_SCOPE_ATTR,
    TableScope,
    TableScopeViolation,
    check_table_scope,
    enforce_table_scope,
)
from app.data_sources.clients.duckdb_client import DuckDBClient

BS = chr(92)


def _sq(path) -> str:
    return str(path).replace("'", "''")


# ------------------------------------------------------------ scratch files

@pytest.fixture
def files(tmp_path) -> SimpleNamespace:
    """A CSV and a parquet file the connection names, and a readable file
    it does not."""
    data = tmp_path / "data"
    data.mkdir()
    (data / "sales.csv").write_text("id,amount\n1,10\n2,20\n3,30\n")
    writer = duckdb.connect()
    writer.execute(
        f"COPY (SELECT * FROM (VALUES (1, 'north'), (2, 'south')) AS t(id, region)) "
        f"TO '{_sq(data / 'regions.parquet')}' (FORMAT PARQUET)")
    writer.close()
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "other.csv"
    secret.write_text("token\nhidden-value\n")
    return SimpleNamespace(data=data, secret=secret)


@pytest.fixture
def client(files) -> DuckDBClient:
    return DuckDBClient(uris=f"{files.data / 'sales.csv'}\n{files.data / 'regions.parquet'}")


@pytest.fixture
def scoped(client) -> DuckDBClient:
    """The same connection with a table selection: sales on, regions off."""
    setattr(client, TABLE_SCOPE_ATTR, TableScope.from_names(active=["sales"], inactive=["regions"]))
    return client


@pytest.fixture
def db_file(tmp_path) -> Path:
    path = tmp_path / "warehouse.duckdb"
    con = duckdb.connect(str(path))
    con.execute("CREATE TABLE orders AS SELECT * FROM (VALUES (1, 5), (2, 7)) AS t(id, amount)")
    con.close()
    return path


def _run(client, sql: str):
    """What the sandbox wrapper does with a statement: the read-only guard,
    then table scope on the text it returned, then the client sends it."""
    checked = ensure_read_only(client, sql)
    enforce_table_scope(client, checked)
    return client.execute_query(checked)


@pytest.fixture
def inprocess(monkeypatch):
    monkeypatch.setenv("BOW_CODE_SANDBOX", "inprocess")
    # In-process mode is for local development only; CI runs with
    # ENVIRONMENT=production, so the test names the environment itself.
    monkeypatch.setenv("ENVIRONMENT", "development")


def _sandbox(client, sql: str):
    code = f"def generate_df(ds_clients, excel_files):\n    return ds_clients['duck'].execute_query({sql!r})\n"
    return StreamingCodeExecutor().execute_code(code=code, ds_clients={"duck": client}, excel_files=[])


# ------------------------------------------------------------ reads outside the connection

def _outside_reads(secret) -> list[str]:
    p = _sq(secret)
    return [
        f"SELECT * FROM read_csv_auto('{p}')",
        f"SELECT * FROM read_csv('{p}')",
        f"SELECT * FROM '{p}'",
        f'SELECT * FROM "{secret}"',
        f"SELECT * FROM sales JOIN read_text('{p}') ON true",
        f"SELECT * FROM query('SELECT * FROM read_csv_auto(''{p}'')')",
        f"SELECT * FROM query_table('{p}')",
        "SELECT current_setting('threads') AS v",
        "SELECT * FROM duckdb_settings()",
        "SELECT * FROM pg_settings",
        "SELECT getenv('HOME') AS v",
        "SELECT * FROM glob('*')",
        f"SELECT * FROM sniff_csv('{p}')",
        "SELECT * FROM duckdb_tables()",
    ]


def test_a_file_reader_quoted_path_settings_read_or_query_call_is_refused_without_a_table_selection(client, files):
    for sql in _outside_reads(files.secret):
        with pytest.raises(ReadOnlyViolation):
            _run(client, sql)


def test_through_the_sandbox_reads_outside_the_connection_are_refused_and_a_view_read_runs(
        inprocess, client, files):
    df, _log, _queries = _sandbox(client, "SELECT sum(amount) AS total FROM sales")
    assert df["total"].tolist() == [60]
    for sql in (f"SELECT * FROM read_csv_auto('{_sq(files.secret)}')", f"SELECT * FROM '{_sq(files.secret)}'",
                "SELECT * FROM main.not_a_view"):
        with pytest.raises(UnsafeSQLError):
            _sandbox(client, sql)


def test_a_view_read_returns_rows(client):
    assert _run(client, "SELECT sum(amount) AS total FROM sales")["total"].tolist() == [60]
    assert _run(client, "SELECT r.region FROM main.regions r JOIN SALES s USING (id) ORDER BY 1")[
        "region"].tolist() == ["north", "south"]
    assert _run(client, "WITH big AS (SELECT * FROM memory.main.sales WHERE amount > 10) "
                        "SELECT count(*) AS n FROM big")["n"].tolist() == [2]
    assert _run(client, "SELECT * FROM range(3) r JOIN sales ON sales.id = r.range")["id"].tolist() == [1, 2]


def test_the_locked_connection_cannot_read_outside_its_uris_even_without_the_checks(client, files):
    with client.connect() as con:
        assert con.execute("SELECT count(*) FROM sales").fetchone() == (3,)
        assert con.execute("SELECT count(*) FROM regions").fetchone() == (2,)
        with pytest.raises(duckdb.Error):
            con.execute(f"SELECT * FROM read_csv_auto('{_sq(files.secret)}')").fetchall()
        with pytest.raises(duckdb.Error):
            con.execute("SET enable_external_access = true")


def test_a_glob_uri_reads_its_directory_and_nothing_outside_it(tmp_path, files):
    daily = tmp_path / "daily"
    daily.mkdir()
    (daily / "part1.csv").write_text("id\n1\n")
    (daily / "part2.csv").write_text("id\n2\n")
    client = DuckDBClient(uris=f"{daily}/*.csv")
    assert _run(client, "SELECT count(*) AS n FROM daily")["n"].tolist() == [2]
    with client.connect() as con:
        with pytest.raises(duckdb.Error):
            con.execute(f"SELECT * FROM read_csv_auto('{_sq(files.secret)}')").fetchall()


# ------------------------------------------------------------ database file mode

@pytest.mark.parametrize("how", ["database", "uris"])
def test_a_database_file_connection_lists_and_reads_its_tables_and_nothing_else(how, db_file, files):
    client = DuckDBClient(**{how: str(db_file)})
    assert [t.name for t in client.get_tables()] == ["orders"]
    assert _run(client, "SELECT sum(amount) AS total FROM orders")["total"].tolist() == [12]
    assert _run(client, 'SELECT count(*) AS n FROM main."orders"')["n"].tolist() == [2]
    assert len(_run(client, "DESCRIBE orders")) == 2
    for sql in (f"SELECT * FROM '{_sq(files.secret)}'", f'SELECT * FROM "{files.secret}"',
                f"SELECT * FROM read_csv_auto('{_sq(files.secret)}')", "SELECT * FROM 'https://example.com/x.csv'"):
        with pytest.raises(ReadOnlyViolation):
            _run(client, sql)
    with client.connect() as con:
        with pytest.raises(duckdb.Error):
            con.execute(f"SELECT * FROM read_csv_auto('{_sq(files.secret)}')").fetchall()


# ------------------------------------------------------------ the read verbs, unscoped

@pytest.mark.parametrize("sql", [
    "SHOW TABLES",
    "SHOW ALL TABLES",
    "SHOW DATABASES",
    "SHOW sales",
    "DESCRIBE sales",
    "DESC sales",
    'describe main."sales";',
    "DESCRIBE SELECT * FROM sales",
    "PRAGMA table_info('sales')",
    "PRAGMA table_info(sales)",
    "PRAGMA show_tables",
    "PRAGMA show_tables_expanded",
    "PRAGMA database_list",
    "PRAGMA version",
    "PRAGMA platform",
    "EXPLAIN SELECT * FROM sales",
    "-- columns\nDESCRIBE sales -- note",
    "/* a /* nested */ note */ SHOW TABLES;",
])
def test_the_read_verbs_return_rows_on_an_unscoped_connection(client, sql):
    assert len(_run(client, sql)) > 0


@pytest.mark.parametrize("sql", [
    "PRAGMA threads = 1",
    "PRAGMA memory_limit='1GB'",
    "PRAGMA enable_profiling",
    "PRAGMA storage_info('sales')",
    "PRAGMA show('sales')",
    "PRAGMA show(sales)",
    "PRAGMA table_info('{secret}')",
    "DESCRIBE read_csv('{secret}')",
    "DESCRIBE '{secret}'",
    "SHOW '{secret}'",
    "SUMMARIZE sales",
    "DESCRIBE all",
    "EXPLAIN ANALYZE SELECT * FROM sales",
    "EXPLAIN SELECT * FROM read_csv_auto('{secret}')",
    "DESCRIBE SELECT * FROM read_csv_auto('{secret}')",
    "SELECT * FROM (DESCRIBE '{secret}')",
    "CALL pragma_version()",
    "SET threads = 1",
    "CREATE TABLE copied AS SELECT * FROM sales",
    "COPY (SELECT * FROM sales) TO '{secret}'",
    "ATTACH '{secret}' AS other",
    "INSTALL httpfs",
    "PRAGMA version; DROP VIEW sales",
    "SHOW TABLES; SELECT * FROM read_csv_auto('{secret}')",
    "EXPLAIN SELECT 1; DROP VIEW sales",
    "DESCRIBE sales /* note */ ; DROP VIEW sales",
    "PRAGMA version /* /* */ ; DROP VIEW sales",
])
def test_reads_outside_the_list_are_refused_on_an_unscoped_connection(client, files, sql):
    with pytest.raises(ReadOnlyViolation):
        ensure_read_only(client, sql.format(secret=_sq(files.secret)))
    with client.connect() as con:
        assert con.execute("SELECT count(*) FROM sales").fetchone() == (3,)


@pytest.mark.parametrize("sql", [
    "SELECT * FROM main.not_a_view",
    "SELECT * FROM other.main.sales",
    "SELECT * FROM memory.other.sales",
    "SELECT * FROM information_schema.tables",
    "DESCRIBE not_a_view",
    "DESCRIBE other.main.sales",
    "SHOW main.not_a_view",
    "PRAGMA table_info('not_a_view')",
    "DESCRIBE SELECT * FROM not_a_view",
    "EXPLAIN SELECT * FROM not_a_view",
])
def test_a_uri_connection_reads_only_its_registered_views(client, sql):
    with pytest.raises(ReadOnlyViolation):
        _run(client, sql)


# ------------------------------------------------------------ with a table selection

@pytest.mark.parametrize("sql", [
    "SHOW TABLES",
    "SHOW ALL TABLES",
    "SHOW DATABASES",
    "SHOW sales",
    "EXPLAIN SELECT * FROM sales",
])
def test_with_a_table_selection_listing_commands_and_explain_stay_refused(scoped, sql):
    with pytest.raises(TableScopeViolation):
        _run(scoped, sql)


@pytest.mark.parametrize("sql", [
    "PRAGMA show_tables",
    "PRAGMA show_tables_expanded",
    "PRAGMA database_list",
    "PRAGMA version",
    "PRAGMA platform",
])
def test_with_a_table_selection_listing_pragmas_are_refused(scoped, sql):
    with pytest.raises(TableScopeViolation):
        _run(scoped, sql)


@pytest.mark.parametrize("sql", [
    "DESCRIBE tables",
    "DESC TABLES",
    'DESCRIBE "tables"',
    'SHOW "Tables"',
    "DESCRIBE databases",
    "DESCRIBE schemas",
])
def test_a_name_duckdb_reads_as_a_listing_is_a_listing_even_when_a_table_has_it(client, sql):
    """DuckDB lists tables, databases or schemas for DESCRIBE / SHOW of a
    few single names. With a table selection that holds a table of that
    name, the statement is still a listing and is refused; without one it
    is a listing like SHOW TABLES."""
    assert len(_run(client, sql)) > 0
    setattr(client, TABLE_SCOPE_ATTR, TableScope.from_names(
        active=["sales", "tables", "databases", "schemas", "all"], inactive=["regions"]))
    with pytest.raises(TableScopeViolation):
        _run(client, sql)


def test_on_a_database_file_a_table_named_like_a_listing_does_not_open_the_listing(tmp_path):
    path = tmp_path / "named.duckdb"
    con = duckdb.connect(str(path))
    con.execute('CREATE TABLE "tables" AS SELECT 1 AS id')
    con.execute("CREATE TABLE hidden AS SELECT 2 AS id")
    con.close()
    client = DuckDBClient(database=str(path))
    setattr(client, TABLE_SCOPE_ATTR, TableScope.from_names(active=["tables"], inactive=["hidden"]))
    assert _run(client, 'SELECT id FROM "tables"')["id"].tolist() == [1]
    for sql in ("DESCRIBE tables", 'DESC "tables"', 'SHOW "tables"'):
        with pytest.raises(TableScopeViolation):
            _run(client, sql)


@pytest.mark.parametrize("template", [
    "DESCRIBE {t}",
    "DESC {t}",
    "DESCRIBE SELECT * FROM {t}",
    "PRAGMA table_info('{t}')",
    "PRAGMA table_info({t})",
    "SELECT * FROM {t}",
])
def test_with_a_table_selection_describe_and_table_info_are_checked_against_it(scoped, template):
    assert len(_run(scoped, template.format(t="sales"))) > 0
    with pytest.raises(TableScopeViolation):
        _run(scoped, template.format(t="regions"))


def test_with_a_table_selection_the_refusal_reaches_the_sandbox_as_table_access(inprocess, scoped):
    df, _log, _queries = _sandbox(scoped, "SELECT count(*) AS n FROM sales")
    assert df["n"].tolist() == [3]
    for sql in ("PRAGMA show_tables", "PRAGMA table_info('regions')"):
        with pytest.raises(TableAccessError):
            _sandbox(scoped, sql)


def test_with_a_table_selection_the_tables_read_are_the_ones_duckdb_reads():
    """Table scope reads a DuckDB statement with DuckDB's parser, so a
    table only DuckDB sees is still checked."""
    scope = TableScope.from_names(active=["sales"], inactive=["regions"])
    check_table_scope("SELECT * FROM sales", "duckdb", scope)
    for sql in ("FROM regions", "SELECT * FROM sales, regions", "SELECT * FROM sales WHERE id IN (FROM regions)",
                "SELECT * FROM (DESCRIBE regions)"):
        with pytest.raises(TableScopeViolation):
            check_table_scope(sql, "duckdb", scope)


# ------------------------------------------------------------ strings with escapes

def test_e_string_shapes_do_not_change_the_tables_read(client, scoped):
    plain = "SELECT E'it''s' AS v, amount FROM sales WHERE id = 1"
    assert _run(client, plain)["v"].tolist() == ["it's"]
    assert _run(scoped, plain)["amount"].tolist() == [10]
    for sql in ("SELECT E'a" + BS + "' AS v FROM regions -- ' FROM sales",
                "SELECT E'a" + BS + "' AS v FROM sales -- ' FROM regions"):
        with pytest.raises(ReadOnlyViolation):
            ensure_read_only(client, sql)
        with pytest.raises(TableScopeViolation):
            enforce_table_scope(scoped, sql)


# ------------------------------------------------------------ view names

def test_view_names_are_the_views_the_connection_creates(tmp_path):
    for sub in ("a", "b", "daily"):
        (tmp_path / sub).mkdir()
    (tmp_path / "a" / "sales.csv").write_text("id\n1\n")
    (tmp_path / "b" / "sales.csv").write_text("id\n2\n")
    (tmp_path / "daily" / "part1.csv").write_text("id\n3\n")
    (tmp_path / "plain").write_text("id\n4\n")
    (tmp_path / "Sales-2024.csv").write_text("id\n5\n")
    uris = [tmp_path / "a" / "sales.csv", tmp_path / "b" / "sales.csv", f"{tmp_path / 'daily'}/*.csv",
            tmp_path / "plain", tmp_path / "Sales-2024.csv"]
    client = DuckDBClient(uris="\n".join(str(u) for u in uris))
    assert client.view_names() == ["sales", "sales_2", "daily", "plain", "Sales_2024"]
    with client.connect() as con:
        created = [r[0] for r in con.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_type = 'VIEW' ORDER BY table_name"
        ).fetchall()]
    assert sorted(client.view_names()) == created
    assert _run(client, "SELECT count(*) AS n FROM SALES_2024")["n"].tolist() == [1]


# ------------------------------------------------------------ CTE names and the catalog

# Python's lower() folds this letter to "k"; DuckDB folds ASCII letters only.
KELVIN = chr(0x212A)
FAKE_TOKEN = "sig=FAKE-TOKEN"


@pytest.fixture
def http_base(tmp_path):
    """A local HTTP server over scratch files: remote.csv, which the
    connection names, and other.csv, which it does not."""
    served = tmp_path / "served"
    served.mkdir()
    (served / "remote.csv").write_text("id,amount\n1,5\n")
    (served / "other.csv").write_text("token\nhidden-value\n")

    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(served)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.fixture
def remote(files, http_base) -> DuckDBClient:
    """A connection over a local file and an http URI carrying a token."""
    return DuckDBClient(uris=f"{files.data / 'sales.csv'}\n{http_base}/remote.csv?{FAKE_TOKEN}")


def _connection(mode: str, request) -> DuckDBClient:
    if mode == "uris":
        return request.getfixturevalue("remote")
    return DuckDBClient(database=str(request.getfixturevalue("db_file")))


@pytest.mark.parametrize("mode", ["uris", "database"])
@pytest.mark.parametrize("view", ["duckdb_views", "duckdb_columns", "duckdb_databases"])
def test_a_cte_named_with_a_non_ascii_letter_does_not_stand_in_for_a_catalog_view(mode, view, request):
    client = _connection(mode, request)
    shadow = view.replace("k", KELVIN)
    sql = f"WITH {shadow} AS (SELECT 1 AS x) SELECT * FROM {view}"
    try:
        rows = _run(client, sql)
    except ReadOnlyViolation:
        pass
    else:
        text = rows.to_string()
        pytest.fail(f"the catalog view {view} was read ({len(rows)} rows; "
                    f"configured URI returned: {FAKE_TOKEN in text})")


@pytest.mark.parametrize("mode", ["uris", "database"])
@pytest.mark.parametrize("sql,refusal", [
    ("WITH duckdb_views AS (SELECT 1 AS x) SELECT * FROM duckdb_views", ReadOnlyViolation),
    ("WITH pg_settings AS (SELECT 1 AS x) SELECT * FROM pg_settings", ReadOnlyViolation),
    ("WITH Sqlite_Master AS (SELECT 1 AS x) SELECT * FROM sqlite_master", ReadOnlyViolation),
    ('WITH "a/b" AS (SELECT 1 AS x) SELECT * FROM "a/b"', DuckdbQueryError),
    ('WITH "a.b" AS (SELECT 1 AS x) SELECT * FROM "a.b"', DuckdbQueryError),
    (f'WITH "{KELVIN}" AS (SELECT 1 AS x) SELECT * FROM "{KELVIN}"', DuckdbQueryError),
    (f'WITH "mine{chr(10)}" AS (SELECT 1 AS x) SELECT * FROM "mine{chr(10)}"', DuckdbQueryError),
])
def test_a_cte_has_a_plain_name_that_is_not_a_catalog_name(mode, sql, refusal, request):
    client = _connection(mode, request)
    with pytest.raises(refusal):
        _run(client, sql)
    own = "sales" if mode == "uris" else "orders"
    assert _run(client, f"WITH mine AS (SELECT * FROM {own}) SELECT count(*) AS n FROM mine")["n"].tolist()[0] > 0


def _internal_views() -> list[tuple[str, str]]:
    con = duckdb.connect()
    try:
        return con.execute(
            "SELECT schema_name, view_name FROM duckdb_views() WHERE internal ORDER BY 1, 2").fetchall()
    finally:
        con.close()


INTERNAL_VIEWS = _internal_views()


def test_duckdb_has_the_internal_views_these_tests_name():
    names = {view for _schema, view in INTERNAL_VIEWS}
    assert {"duckdb_views", "pg_settings", "sqlite_master", "pragma_database_list", "tables"} <= names
    assert {schema for schema, _view in INTERNAL_VIEWS} == {"information_schema", "main", "pg_catalog"}


@pytest.mark.parametrize("mode", ["uris", "database"])
@pytest.mark.parametrize("schema,view", INTERNAL_VIEWS)
def test_an_internal_catalog_view_is_refused_without_a_table_selection(mode, schema, view, request):
    client = _connection(mode, request)
    reads = [f"SELECT * FROM {schema}.{view}", f"SELECT * FROM system.{schema}.{view}",
             f'SELECT * FROM "{schema.upper()}"."{view.upper()}"', f"DESCRIBE {schema}.{view}"]
    if schema != "information_schema":
        reads.append(f"SELECT * FROM {view}")
    for sql in reads:
        with pytest.raises(ReadOnlyViolation):
            ensure_read_only(client, sql)


# ------------------------------------------------------------ what the engine is locked to

def test_an_http_uri_with_a_query_string_is_allowed_exactly(remote, http_base):
    assert _run(remote, "SELECT sum(amount) AS total FROM remote")["total"].tolist() == [5]
    with remote.connect() as con:
        assert con.execute("SELECT count(*) FROM remote").fetchone() == (1,)
        for other in (f"{http_base}/other.csv", f"{http_base}/remote.csv"):
            with pytest.raises(duckdb.Error):
                con.execute(f"SELECT * FROM read_csv_auto('{other}')").fetchall()


def test_the_lockdown_targets_of_each_uri_shape(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    client = DuckDBClient(uris="\n".join([
        "https://host.example/x.csv?sig=abc",
        "https://host.example/y[1].csv",
        "s3://bucket/part?.csv",
        "s3://bucket/daily/*.parquet",
        "s3://bucket/exports/",
        "data/*.csv",
        "../shared/*.csv",
        "~/in-home/*.csv",
        f"file://{tmp_path}/a/../b/*.csv",
        f"/{tmp_path}/c/*.csv",
        f"{tmp_path}/reports/",
        str(tmp_path / "abs.csv"),
        f"file://{tmp_path}/f.csv",
    ]))
    paths, directories = client._lockdown_targets()
    assert paths == ["https://host.example/x.csv?sig=abc", "https://host.example/y[1].csv", "s3://bucket/exports/",
                     f"{tmp_path}/reports/", str(tmp_path / "abs.csv"), f"file://{tmp_path}/f.csv", f"{tmp_path}/f.csv"]
    assert directories == ["s3://bucket/", "s3://bucket/daily/", "s3://bucket/exports/", f"{work}/data/",
                           f"{tmp_path}/shared/", f"{home}/in-home/", f"{tmp_path}/b/", f"{tmp_path}/c/",
                           f"{tmp_path}/reports/"]


def test_a_relative_glob_under_a_named_directory_reads_its_files_and_nothing_outside_even_without_the_checks(
        tmp_path, monkeypatch):
    work = tmp_path / "work"
    (work / "data").mkdir(parents=True)
    (work / "data" / "x.csv").write_text("id\n1\n")
    (work / "private.csv").write_text("token\nhidden-value\n")
    secret = tmp_path / "other.csv"
    secret.write_text("token\nhidden-value\n")
    monkeypatch.chdir(work)
    client = DuckDBClient(uris="data/*.csv")
    with client.connect() as con:
        assert con.execute(f"SELECT count(*) FROM {client.view_names()[0]}").fetchone() == (1,)
        for outside in ("private.csv", "../other.csv", str(secret)):
            with pytest.raises(duckdb.Error):
                con.execute(f"SELECT * FROM read_csv_auto('{_sq(outside)}')").fetchall()


def test_a_glob_under_the_home_directory_reads_its_files_and_nothing_outside(tmp_path, monkeypatch, files):
    home = tmp_path / "home"
    (home / "daily").mkdir(parents=True)
    (home / "daily" / "part1.csv").write_text("id\n1\n")
    (home / "daily" / "part2.csv").write_text("id\n2\n")
    monkeypatch.setenv("HOME", str(home))
    client = DuckDBClient(uris="~/daily/*.csv")
    assert _run(client, "SELECT count(*) AS n FROM daily")["n"].tolist() == [2]
    with client.connect() as con:
        with pytest.raises(duckdb.Error):
            con.execute(f"SELECT * FROM read_csv_auto('{_sq(files.secret)}')").fetchall()


def test_a_directory_uri_reads_the_files_under_it_and_nothing_outside(tmp_path, files):
    reports = tmp_path / "reports"
    (reports / "2026").mkdir(parents=True)
    (reports / "a.csv").write_text("id\n1\n")
    (reports / "2026" / "b.csv").write_text("id\n2\n")
    client = DuckDBClient(uris=f"{reports}/")
    assert _run(client, "SELECT count(*) AS n FROM reports")["n"].tolist() == [2]
    with client.connect() as con:
        with pytest.raises(duckdb.Error):
            con.execute(f"SELECT * FROM read_csv_auto('{_sq(files.secret)}')").fetchall()


ROOT_REFUSAL = ("Error while connecting to DuckDB: A URI of this connection is a glob or a directory at the root "
                "of the local file system, directly after its scheme, or with a .. segment after its scheme. "
                "Name the directory its files are in.")
RELATIVE_REFUSAL = ("Error while connecting to DuckDB: A URI of this connection is a glob or a directory relative "
                    "to the working directory that names no directory under it. "
                    "Name the directory its files are in.")


def _opened_or_refused(client: DuckDBClient, outside: str) -> str:
    """The refusal `connect()` raises, or a test failure when the
    connection opens and reads `outside`, a file none of its URIs name."""
    try:
        with client.connect() as con:
            leaked = con.execute(f"SELECT * FROM read_csv_auto('{_sq(outside)}')").fetchall()
    except RuntimeError as refused:
        return str(refused)
    pytest.fail(f"the locked connection read a file outside its URIs ({len(leaked)} rows)")


@pytest.mark.parametrize("uri", [
    "/*.csv", "/*/x.csv", "file:///*.csv", "s3://*/x.csv", "s3://bucket*/x.csv", "gs://*",
    "https://*.example.com/x.csv", "s3://bucket/../*.csv", "s3://bucket/a/../../*.csv", "/", "file:///", "s3://",
    "file:/*.csv", "file://localhost/*.csv", "file:/", "file://localhost/", "/.", "file:///.",
])
def test_a_glob_at_a_root_is_refused_before_the_connection_opens_anything(uri):
    with pytest.raises(RuntimeError) as refused:
        with DuckDBClient(uris=uri).connect():
            pass
    assert str(refused.value) == ROOT_REFUSAL


def _climbing_to_the_root(prefix: str, target: Path) -> str:
    """A glob URI for `target`, written as `prefix`, its top directory and
    `..`: the directory before the glob is the root once resolved."""
    top, rest = target.parts[1], "/".join(target.parts[2:])
    return f"{prefix}{top}/../{top[0]}*/{rest}"


@pytest.mark.parametrize("prefix", ["//", "file:///", "file:////"])
def test_a_glob_whose_directory_resolves_to_the_root_is_refused(prefix, files):
    client = DuckDBClient(uris=_climbing_to_the_root(prefix, files.data / "sales.csv"))
    assert _opened_or_refused(client, str(files.secret)) == ROOT_REFUSAL


@pytest.mark.parametrize("uri", ["*.csv", "./*.csv", "data*.csv", "sub*/x.csv", ".//*.csv", "../*.csv",
                                 "file://*.csv", "./"])
def test_a_relative_glob_that_names_no_directory_is_refused(uri, tmp_path, monkeypatch):
    work = tmp_path / "work"
    (work / "sub1").mkdir(parents=True)
    (work / "data1.csv").write_text("id\n1\n")
    (work / "sub1" / "x.csv").write_text("id\n1\n")
    (tmp_path / "top.csv").write_text("id\n1\n")
    (work / "private").mkdir()
    (work / "private" / "other.csv").write_text("token\nhidden-value\n")
    monkeypatch.chdir(work)
    assert _opened_or_refused(DuckDBClient(uris=uri), str(work / "private" / "other.csv")) == RELATIVE_REFUSAL


@pytest.fixture
def scratch_cwd(tmp_path, monkeypatch) -> SimpleNamespace:
    """A working directory holding a file no URI names, under private/,
    a CSV in it and in its parent (so a glob there finds files), and a
    directory sub2/ with one CSV."""
    work = tmp_path / "work"
    (work / "private").mkdir(parents=True)
    (work / "private" / "p.csv").write_text("token\nhidden-value\n")
    (work / "top.csv").write_text("id\n1\n")
    (tmp_path / "up.csv").write_text("id\n1\n")
    for name in ("x", "data", "sub"):
        (work / name).mkdir()
    (work / "sub2").mkdir()
    (work / "sub2" / "x.csv").write_text("id\n1\n")
    monkeypatch.chdir(work)
    return SimpleNamespace(work=work, private=work / "private" / "p.csv")


@pytest.mark.parametrize("uri", ["x/../*.csv", "data/../*.csv", "./data/../*.csv", "data/../", "x/../../*.csv",
                                 "file:data/../*.csv", "data/..", "."])
def test_a_relative_uri_that_resolves_to_the_working_directory_or_above_is_refused(uri, scratch_cwd):
    assert _opened_or_refused(DuckDBClient(uris=uri), str(scratch_cwd.private)) == RELATIVE_REFUSAL


def test_a_relative_glob_that_resolves_under_the_working_directory_reads_its_files_and_nothing_outside(scratch_cwd):
    client = DuckDBClient(uris="sub/../sub2/*.csv")
    assert _run(client, "SELECT count(*) AS n FROM sub2")["n"].tolist() == [1]
    with client.connect() as con:
        with pytest.raises(duckdb.Error):
            con.execute(f"SELECT * FROM read_csv_auto('{_sq(scratch_cwd.private)}')").fetchall()


@pytest.mark.parametrize("prefix", ["file:", "file://localhost"])
def test_a_file_uri_glob_reads_its_directory_and_nothing_outside(prefix, files):
    client = DuckDBClient(uris=f"{prefix}{files.data}/*.csv")
    assert _run(client, "SELECT count(*) AS n FROM data")["n"].tolist() == [3]
    with client.connect() as con:
        with pytest.raises(duckdb.Error):
            con.execute(f"SELECT * FROM read_csv_auto('{_sq(files.secret)}')").fetchall()


def test_a_tilde_followed_by_a_name_is_not_a_home_directory(scratch_cwd, monkeypatch, tmp_path):
    # DuckDB expands only `~` and `~/`: `~root/` is a directory under the
    # working directory, not another user's home.
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    paths, directories = DuckDBClient(uris="~root/*.csv")._lockdown_targets()
    assert (paths, directories) == ([], [f"{scratch_cwd.work}/~root/"])


def test_a_directory_uri_without_its_slash_reads_the_files_under_it_and_nothing_outside(tmp_path, files):
    clean = tmp_path / "clean"
    (clean / "2026").mkdir(parents=True)
    (clean / "a.csv").write_text("id\n1\n")
    (clean / "2026" / "b.csv").write_text("id\n2\n")
    client = DuckDBClient(uris=str(clean))
    assert _run(client, "SELECT count(*) AS n FROM clean")["n"].tolist() == [2]
    with client.connect() as con:
        with pytest.raises(duckdb.Error):
            con.execute(f"SELECT * FROM read_csv_auto('{_sq(files.secret)}')").fetchall()


# ------------------------------------------------------------ table selection, names as DuckDB folds them

U_UMLAUT, u_umlaut = chr(0xDC), chr(0xFC)
OVERVIEW, overview = U_UMLAUT + "bersicht", u_umlaut + "bersicht"
CYRILLIC = chr(0x414) + chr(0x430) + chr(0x43D)


@pytest.fixture
def named_db(tmp_path) -> Path:
    """Tables whose names differ only in a non-ASCII letter's case, which
    DuckDB keeps apart, or in a Kelvin sign against a 'k'."""
    path = tmp_path / "names.duckdb"
    con = duckdb.connect(str(path))
    for name, v in [(OVERVIEW, 1), (overview, 2), (CYRILLIC, 3), (KELVIN + "x", 4), ("kx", 5)]:
        con.execute(f'CREATE TABLE "{name}" AS SELECT {v} AS v')
    con.close()
    return path


def test_with_a_table_selection_an_enabled_non_ascii_name_reads_in_every_spelling_duckdb_accepts(named_db):
    client = DuckDBClient(database=str(named_db))
    setattr(client, TABLE_SCOPE_ATTR, TableScope.from_names(
        active=[OVERVIEW, CYRILLIC, KELVIN + "x"], inactive=[overview, "kx"]))
    for name, v in [(OVERVIEW, 1), (OVERVIEW.upper(), 1), (U_UMLAUT + "BERSICHT", 1), (f'"{OVERVIEW}"', 1),
                    (f'"{OVERVIEW.upper()}"', 1), (f"main.{OVERVIEW}", 1), (CYRILLIC, 3), (f'"{CYRILLIC}"', 3),
                    (f'"{KELVIN}x"', 4), (f'"{KELVIN}X"', 4)]:
        assert _run(client, f"SELECT v FROM {name}")["v"].tolist() == [v], name
    for name in (overview, u_umlaut + "BERSICHT", f'"{overview}"', "kx", "KX", '"Kx"'):
        with pytest.raises(TableScopeViolation):
            _run(client, f"SELECT v FROM {name}")


def test_with_a_table_selection_a_kelvin_sign_never_stands_for_an_enabled_k(named_db):
    client = DuckDBClient(database=str(named_db))
    setattr(client, TABLE_SCOPE_ATTR, TableScope.from_names(active=["kx"], inactive=[KELVIN + "x"]))
    for name in ("kx", "KX", '"Kx"'):
        assert _run(client, f"SELECT v FROM {name}")["v"].tolist() == [5], name
    for name in (f'"{KELVIN}x"', f'"{KELVIN}X"'):
        with pytest.raises(TableScopeViolation):
            _run(client, f"SELECT v FROM {name}")


# ------------------------------------------------------------ SUMMARIZE

@pytest.mark.parametrize("sql", ["SUMMARIZE sales", "SELECT * FROM (SUMMARIZE sales)"])
def test_summarize_is_refused_as_not_available_with_a_select_to_use_instead(client, sql):
    with pytest.raises(ReadOnlyViolation) as refused:
        ensure_read_only(client, sql)
    assert "SUMMARIZE is not available" in str(refused.value)
    assert "SELECT with aggregates" in str(refused.value)


# ------------------------------------------------------------ the wrapper

def test_text_duckdb_cannot_parse_is_captured_before_its_error_reaches_the_code(inprocess, client):
    sql = "SELEC amount FROM sales"
    queries: list = []
    code = f"def generate_df(ds_clients, excel_files):\n    return ds_clients['duck'].execute_query({sql!r})\n"
    with pytest.raises(DuckdbSyntaxError):
        StreamingCodeExecutor().execute_code(code=code, ds_clients={"duck": client}, excel_files=[],
                                             captured_queries=queries)
    assert [str(q) for q in queries] == [sql]
