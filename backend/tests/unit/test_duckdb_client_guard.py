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

Real embedded DuckDB over scratch CSV and parquet files under `tmp_path`;
nothing touches the network.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import duckdb
import pytest

from app.ai.code_execution.code_execution import StreamingCodeExecutor, TableAccessError, UnsafeSQLError
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
    "PRAGMA show('sales')",
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
