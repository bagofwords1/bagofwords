"""The engines read these texts differently from sqlglot, and the guard now
refuses them before anything is sent.

Contract, on real servers (MySQL 8 and MariaDB 11.4, in their default
mode and in the server modes that move where literals end; SQL Server 2019;
Postgres 15; ClickHouse 25.8, run in clickhouse-local): for each shape,
sqlglot's reading finds only active tables or no write, the engine runs
more (a hidden read of another table, a file written, a table created, a
filter dropped), and `ensure_read_only` or table scope refuses the text.
The engine is driven through its own driver or command line, never through
the client, so the guard is not in the way of the proof. Where an engine
reads text the way sqlglot does (SQL Server's spaces, its nested comments;
the digit-led names MySQL, MariaDB and ClickHouse read as one name), the
exemption the guard makes for it is pinned here.

Needs Docker. CI does not collect tests/integrations; these run on the test
harness. Without a Docker daemon they skip, or fail when CI is set.
"""

from __future__ import annotations

import os

import pytest
from sqlglot import exp

from app.ai.code_execution.sql_guard import ReadOnlyViolation, ensure_read_only
from app.ai.code_execution.sql_parser import parse_guard_sql
from app.ai.code_execution.table_scope import TableScope, TableScopeViolation, check_table_scope, tables_read
from app.data_sources.clients.clickhouse_client import ClickhouseClient
from app.data_sources.clients.mariadb_client import MariadbClient
from app.data_sources.clients.mssql_client import MSSQLClient
from app.data_sources.clients.mysql_client import MysqlClient
from app.data_sources.clients.postgresql_client import PostgresqlClient

pytestmark = pytest.mark.integration

NL = chr(10)
NUL = chr(0)
NBSP = chr(0xA0)
K_SIGN = chr(0x212A)
NON_ASCII_SPACES = [chr(c) for c in range(0x80, 0x110000) if chr(c).isspace()]
LINE_BREAKS = {chr(0x85), chr(0x2028), chr(0x2029)}
SECRET = 42


def _docker_available() -> bool:
    try:
        import docker

        docker.from_env().ping()
        return True
    except Exception:
        return False


def _require_docker() -> None:
    if not _docker_available():
        if os.environ.get("CI"):
            pytest.fail("Docker daemon not reachable in CI: the live dialect tests must run")
        pytest.skip("Docker daemon not reachable")


def _sqlglot_tables(sql: str, dialect: str) -> set:
    return {ref for s in parse_guard_sql(sql, dialect) if s is not None for ref in tables_read(s)}


class _Engine:
    """One server and a connection to it as its owner."""

    def __init__(self, conn):
        self.conn = conn

    def rows(self, sql: str) -> list:
        cur = self.conn.cursor()
        cur.execute(sql)
        return [tuple(r) for r in cur.fetchall()] if cur.description else []

    def rows_in_mode(self, mode: str, sql: str) -> list:
        """`rows(sql)` with the session's sql_mode set to `mode` (MySQL,
        MariaDB), then set back."""
        cur = self.conn.cursor()
        cur.execute("SELECT @@SESSION.sql_mode")
        previous = cur.fetchone()[0]
        cur.execute("SET SESSION sql_mode = %s", (mode,))
        try:
            return self.rows(sql)
        finally:
            cur.execute("SET SESSION sql_mode = %s", (previous,))


def _mysql_like(image: str):
    _require_docker()
    import pymysql
    from testcontainers.mysql import MySqlContainer

    with MySqlContainer(image) as container:
        conn = pymysql.connect(host=container.get_container_host_ip(), port=int(container.get_exposed_port(3306)),
                               user="root", password=container.root_password, database=container.dbname,
                               charset="utf8mb4", autocommit=True)
        engine = _Engine(conn)
        for sql in ("CREATE TABLE orders (id INT)", "INSERT INTO orders VALUES (1)",
                    "CREATE TABLE secret (x INT)", f"INSERT INTO secret VALUES ({SECRET})"):
            engine.rows(sql)
        yield engine
        conn.close()


@pytest.fixture(scope="module")
def mysql():
    yield from _mysql_like("mysql:8")


@pytest.fixture(scope="module")
def mariadb():
    yield from _mysql_like("mariadb:11.4")


SCOPE = TableScope.from_names(active=["orders"], inactive=["secret"])


# --------------------------------------------------------------- MySQL / MariaDB

def test_mysql_runs_an_executable_comment_that_sqlglot_drops(mysql):
    sql = "SELECT * FROM orders /*! UNION SELECT * FROM secret */"
    assert _sqlglot_tables(sql, "mysql") == {("orders",)}
    assert (SECRET,) in mysql.rows(sql)
    with pytest.raises(ReadOnlyViolation):
        ensure_read_only(object.__new__(MysqlClient), sql)
    with pytest.raises(TableScopeViolation):
        check_table_scope(sql, "mysql", SCOPE)


def test_mariadb_runs_its_own_executable_comment_that_sqlglot_drops(mariadb):
    sql = "SELECT * FROM orders /*M! UNION SELECT * FROM secret */"
    assert _sqlglot_tables(sql, "mysql") == {("orders",)}
    assert (SECRET,) in mariadb.rows(sql)
    with pytest.raises(ReadOnlyViolation):
        ensure_read_only(object.__new__(MariadbClient), sql)


def test_mysql_reads_two_dashes_before_a_word_as_minus_signs_and_writes_a_file(mysql):
    path = "/var/lib/mysql-files/p5a-dashes.csv"
    sql = f"SELECT * FROM orders WHERE 1 = 1 --1 INTO OUTFILE '{path}'"
    from sqlglot import exp

    # sqlglot reads a comment from the dashes on: a plain read.
    assert not list(parse_guard_sql(sql, "mysql")[0].find_all(exp.Into))
    mysql.rows(sql)
    assert mysql.rows(f"SELECT LOAD_FILE('{path}') IS NOT NULL") == [(1,)]
    with pytest.raises(ReadOnlyViolation):
        ensure_read_only(object.__new__(MysqlClient), sql)


DIGIT_GLUED = [
    "SELECT x, 1.0from secret",
    "SELECT id FROM orders UNION SELECT x + 0.0from secret",
]


@pytest.mark.parametrize("engine_name, client_class", [("mysql", MysqlClient), ("mariadb", MariadbClient)])
@pytest.mark.parametrize("sql", DIGIT_GLUED)
def test_mysql_and_mariadb_read_a_number_glued_to_from_and_read_the_table_after_it(
        request, engine_name, client_class, sql):
    engine = request.getfixturevalue(engine_name)
    # sqlglot reads one name `1.0from` (and `secret` as its alias): no
    # table but the enabled one. The server reads the number, then FROM
    # secret.
    assert _sqlglot_tables(sql, "mysql") <= {("orders",)}
    assert any(row[0] == SECRET for row in engine.rows(sql))
    with pytest.raises(ReadOnlyViolation, match="starts with a digit"):
        ensure_read_only(object.__new__(client_class), sql)
    with pytest.raises(TableScopeViolation):
        check_table_scope(sql, "mysql", SCOPE)


NUMBER_THEN_FROM = [
    "SELECT x, .5from secret",
    "SELECT x, -.5from secret",
    "SELECT x, 1e5from secret",
    "SELECT x, 1e+5from secret",
    "SELECT x, 1.e5from secret",
]


@pytest.mark.parametrize("engine_name, client_class", [("mysql", MysqlClient), ("mariadb", MariadbClient)])
@pytest.mark.parametrize("sql", NUMBER_THEN_FROM)
def test_mysql_and_mariadb_read_these_digit_led_words_as_a_number_and_then_from(request, engine_name, client_class,
                                                                                sql):
    engine = request.getfixturevalue(engine_name)
    assert engine.rows(sql)[0][0] == SECRET
    with pytest.raises(ReadOnlyViolation, match="starts with a digit"):
        ensure_read_only(object.__new__(client_class), sql)


@pytest.mark.parametrize("engine_name, client_class", [("mysql", MysqlClient), ("mariadb", MariadbClient)])
def test_mysql_and_mariadb_read_other_digit_led_words_as_one_name_as_sqlglot_does(request, engine_name,
                                                                                 client_class):
    engine = request.getfixturevalue(engine_name)
    engine.rows("CREATE TABLE IF NOT EXISTS digits (`2023_sales` INT, `1st_quarter` INT, `12abc` INT, `1_000` INT, "
                "`1e` INT, `1efrom` INT)")
    engine.rows("DELETE FROM digits")
    engine.rows("INSERT INTO digits VALUES (1, 2, 3, 4, 5, 6)")
    sql = "SELECT 2023_sales, d.1st_quarter, 12abc, 1_000, 1e, 1efrom FROM digits d"
    assert engine.rows(sql) == [(1, 2, 3, 4, 5, 6)]
    assert _sqlglot_tables(sql, "mysql") == {("digits",)}
    ensure_read_only(object.__new__(client_class), sql)


# Each literal ends at the backslash in the server mode named, while
# sqlglot reads the default mode, in which the backslash escapes the quote.
SERVER_MODE_HIDDEN = [
    ("NO_BACKSLASH_ESCAPES", "SELECT 'a" + chr(92) + "' AS v, x FROM secret -- ' FROM orders"),
    ("ANSI_QUOTES", "SELECT 1 AS \"a" + chr(92) + "\", x FROM secret -- \" FROM orders"),
]


@pytest.mark.parametrize("engine_name, client_class", [("mysql", MysqlClient), ("mariadb", MariadbClient)])
@pytest.mark.parametrize("mode, sql", SERVER_MODE_HIDDEN, ids=[mode for mode, _ in SERVER_MODE_HIDDEN])
def test_a_server_mode_ends_a_literal_at_a_backslash_and_reads_the_table_after_it(request, engine_name,
                                                                                 client_class, mode, sql):
    engine = request.getfixturevalue(engine_name)
    assert _sqlglot_tables(sql, "mysql") == {("orders",)}
    assert [row[1] for row in engine.rows_in_mode(mode, sql)] == [SECRET]
    # In the default mode the server reads what sqlglot reads.
    assert SECRET not in [v for row in engine.rows(sql) for v in row]
    with pytest.raises(ReadOnlyViolation, match="backslash"):
        ensure_read_only(object.__new__(client_class), sql)
    with pytest.raises(TableScopeViolation):
        check_table_scope(sql, "mysql", SCOPE)


def test_into_outfile_hidden_by_a_server_mode_is_refused(mysql):
    path = "/var/lib/mysql-files/p5a-mode.csv"
    sql = "SELECT 'a" + chr(92) + "' AS v INTO OUTFILE '" + path + "' -- ' FROM orders"
    mysql.rows_in_mode("NO_BACKSLASH_ESCAPES", sql)
    assert mysql.rows(f"SELECT LOAD_FILE('{path}') IS NOT NULL") == [(1,)]
    with pytest.raises(ReadOnlyViolation):
        ensure_read_only(object.__new__(MysqlClient), sql)


# MariaDB in MSSQL mode reads [...] as a quoted name; sqlglot reads a
# bracket and then a string.
MSSQL_MODE_BRACKET_HIDDEN = [
    ("SELECT 1 AS [a'], x FROM secret -- '] FROM orders", [(1, SECRET)]),
    ("SELECT [a'] AS v FROM (SELECT 1 AS [a']) t UNION ALL SELECT x FROM secret -- ']", [(1,), (SECRET,)]),
]


@pytest.mark.parametrize("sql, rows", MSSQL_MODE_BRACKET_HIDDEN)
def test_mariadb_in_mssql_mode_reads_brackets_as_a_quoted_name_and_reads_the_table_after_it(mariadb, sql, rows):
    import pymysql

    with pytest.raises(pymysql.err.ProgrammingError):
        mariadb.rows(sql)
    assert mariadb.rows_in_mode("MSSQL", sql) == rows
    with pytest.raises(ReadOnlyViolation, match="bracket"):
        ensure_read_only(object.__new__(MariadbClient), sql)
    with pytest.raises(TableScopeViolation):
        check_table_scope(sql, "mysql", SCOPE)


def test_into_outfile_hidden_by_brackets_in_mssql_mode_is_refused(mariadb):
    path = "/var/lib/mysql/p5a-bracket.csv"
    sql = "SELECT 1 AS [a'] INTO OUTFILE '" + path + "' -- '"
    mariadb.rows_in_mode("MSSQL", sql)
    assert mariadb.rows(f"SELECT LOAD_FILE('{path}') IS NOT NULL") == [(1,)]
    with pytest.raises(ReadOnlyViolation, match="bracket"):
        ensure_read_only(object.__new__(MariadbClient), sql)


@pytest.mark.parametrize("space", NON_ASCII_SPACES, ids=lambda c: f"U+{ord(c):04X}")
def test_mysql_reads_a_space_outside_ascii_as_part_of_a_name(mysql, space):
    import pymysql

    sql = "SELECT id" + space + "FROM orders"
    # sqlglot reads `id FROM orders`; MySQL reads one column name.
    assert _sqlglot_tables(sql, "mysql") == {("orders",)}
    with pytest.raises(pymysql.err.OperationalError, match="Unknown column"):
        mysql.rows(sql)
    with pytest.raises(ReadOnlyViolation):
        ensure_read_only(object.__new__(MysqlClient), sql)


# --------------------------------------------------------------- SQL Server

@pytest.fixture(scope="module")
def mssql():
    _require_docker()
    import pymssql
    from testcontainers.mssql import SqlServerContainer

    with SqlServerContainer("mcr.microsoft.com/mssql/server:2019-latest") as container:
        conn = pymssql.connect(server=container.get_container_host_ip(), port=int(container.get_exposed_port(1433)),
                               user="sa", password=container.password, autocommit=True)
        engine = _Engine(conn)
        engine.rows("CREATE TABLE orders (id INT)")
        engine.rows("INSERT INTO orders VALUES (1)")
        yield engine
        conn.close()


@pytest.mark.parametrize("glued, table", [("0x1into", "t_hex"), ("$1into", "t_money"), ("$1.into", "t_money_dot")])
def test_sql_server_reads_a_constant_glued_to_into_as_a_select_into_that_creates_a_table(mssql, glued, table):
    sql = f"SELECT x = {glued} {table} FROM orders"
    from sqlglot import exp

    assert not list(parse_guard_sql(sql, "tsql")[0].find_all(exp.Into)), "sqlglot reads no INTO"
    mssql.rows(sql)
    assert mssql.rows(f"SELECT COUNT(*) FROM sys.tables WHERE name = '{table}'") == [(1,)]
    with pytest.raises(ReadOnlyViolation):
        ensure_read_only(object.__new__(MSSQLClient), sql)


@pytest.mark.parametrize("space", NON_ASCII_SPACES, ids=lambda c: f"U+{ord(c):04X}")
def test_sql_server_reads_every_space_outside_ascii_as_whitespace(mssql, space):
    sql = "SELECT id" + space + "FROM orders"
    assert mssql.rows(sql) == [(1,)]
    if space in LINE_BREAKS:
        # Also line breaks: refused everywhere outside a string.
        with pytest.raises(ReadOnlyViolation, match="line break"):
            ensure_read_only(object.__new__(MSSQLClient), sql)
    else:
        ensure_read_only(object.__new__(MSSQLClient), sql)


def test_sql_server_nests_block_comments(mssql):
    sql = "SELECT id /* a /* b */ c */ FROM orders"
    assert mssql.rows(sql) == [(1,)]
    ensure_read_only(object.__new__(MSSQLClient), sql)


# --------------------------------------------------------------- ClickHouse

_CLICKHOUSE_TABLES = (
    "CREATE TABLE orders (id Int32) ENGINE = Memory; INSERT INTO orders VALUES (1); "
    f"CREATE TABLE secret (x Int32) ENGINE = Memory; INSERT INTO secret VALUES ({SECRET}); "
    # A Join-engine table and a dictionary over another table, both disabled.
    "CREATE TABLE secret_join (k UInt64, x Int32) ENGINE = Join(ANY, LEFT, k); "
    f"INSERT INTO secret_join VALUES (1, {SECRET}); "
    f"CREATE TABLE secret_src (id UInt64, x Int32) ENGINE = Memory; INSERT INTO secret_src VALUES (1, {SECRET}); "
    "CREATE DICTIONARY secret_dict (id UInt64, x Int32) PRIMARY KEY id "
    "SOURCE(CLICKHOUSE(TABLE 'secret_src')) LAYOUT(FLAT()) LIFETIME(0); "
    # Disabled tables named after words sqlglot reads as keywords after IN.
    + "".join(f"CREATE TABLE {name} (x Int32) ENGINE = Memory; INSERT INTO {name} VALUES ({SECRET}); "
              for name in ("sample", "tablesample", "fetch", "notnull", "current_date"))
)
CLICKHOUSE_KEYWORD_TABLES = ["sample", "tablesample", "fetch", "notnull", "current_date"]
CLICKHOUSE_SCOPE = TableScope.from_names(active=["orders"],
                                         inactive=["secret", "secret_join", "secret_src", "secret_dict",
                                                   *CLICKHOUSE_KEYWORD_TABLES])


@pytest.fixture(scope="module")
def clickhouse_local():
    """A function that runs a query with clickhouse-local, in one container
    kept asleep for the module, after creating `orders` and `secret`; it
    returns (exit code, output)."""
    _require_docker()
    import docker

    container = docker.from_env().containers.run(
        "clickhouse/clickhouse-server:25.8", entrypoint="sleep", command=["infinity"], detach=True)

    def run(sql: str):
        code, out = container.exec_run(["clickhouse", "local", "-q", _CLICKHOUSE_TABLES + sql])
        return code, out.decode().strip()

    try:
        yield run
    finally:
        container.remove(force=True)


@pytest.mark.parametrize("sql", ["SELECT 1.5from system.one", "SELECT dummy FROM system.one WHERE 1 = 1.0from system.one"])
def test_clickhouse_refuses_a_decimal_glued_to_a_word_instead_of_reading_from(clickhouse_local, sql):
    # sqlglot's ClickHouse tokenizer reads `1.5from` as one name, like the
    # MySQL one; ClickHouse refuses a number written right before a word
    # ("Wrong number"), so it never reads this number and then FROM.
    code, out = clickhouse_local(sql)
    assert code != 0 and "Wrong number" in out


@pytest.mark.parametrize("sql", ["SELECT x, -.5from secret", "SELECT 1-.5from secret"])
def test_clickhouse_reads_a_number_after_a_lone_dot_and_then_from(clickhouse_local, sql):
    # After an operator, ClickHouse reads `.5` as a number and FROM after
    # it; sqlglot reads a dot and the name `5from`.
    code, out = clickhouse_local(sql)
    assert code == 0 and out.endswith("0.5")
    with pytest.raises(ReadOnlyViolation, match="starts with a digit"):
        ensure_read_only(object.__new__(ClickhouseClient), sql)


def test_clickhouse_reads_a_digit_led_word_as_one_name_as_sqlglot_does(clickhouse_local):
    code, out = clickhouse_local("SELECT 1from FROM (SELECT 7 AS `1from`)")
    assert code == 0 and out == "7"


LDQ, RDQ, LSQ, RSQ = chr(0x201C), chr(0x201D), chr(0x2018), chr(0x2019)
CURLY_QUOTE_HIDDEN = [
    ("SELECT " + LSQ + "--" + RSQ + " AS a, x FROM secret", "--\t42"),
    ("SELECT 1 AS " + LDQ + "--" + RDQ + ", x FROM secret", "1\t42"),
    ("SELECT id FROM orders WHERE " + LSQ + "--" + RSQ + " = '--' UNION ALL SELECT x FROM secret", "42"),
]


@pytest.mark.parametrize("sql, shown", CURLY_QUOTE_HIDDEN)
def test_clickhouse_reads_curly_quotes_as_quotes_that_sqlglot_reads_as_name_characters(clickhouse_local, sql, shown):
    # sqlglot reads the curly quote as a name character and a comment from
    # the dashes on: no table but the enabled one.
    assert _sqlglot_tables(sql, "clickhouse") <= {("orders",)}
    code, out = clickhouse_local(sql)
    assert code == 0 and shown in out.splitlines()
    with pytest.raises(ReadOnlyViolation, match="curly quote"):
        ensure_read_only(object.__new__(ClickhouseClient), sql)
    with pytest.raises(TableScopeViolation):
        check_table_scope(sql, "clickhouse", SCOPE)


def test_clickhouse_reads_curly_quotes_inside_a_string_as_characters(clickhouse_local):
    sql = "SELECT 'it" + RSQ + "s " + LDQ + "x" + RDQ + "' AS a"
    code, out = clickhouse_local(sql)
    assert code == 0 and out == "it" + RSQ + "s " + LDQ + "x" + RDQ
    ensure_read_only(object.__new__(ClickhouseClient), sql)


IN_TABLE = [
    ("SELECT count() FROM orders WHERE 42 IN secret", "1"),
    ("SELECT count() FROM orders WHERE 42 IN (secret)", "1"),
    ("SELECT count() FROM orders WHERE 42 GLOBAL IN secret", "1"),
    ("SELECT count() FROM orders WHERE 42 NOT IN secret", "0"),
    ("SELECT count() FROM orders WHERE 42 IN default.secret", "1"),
    ("SELECT count() FROM orders WHERE 42 IN (default.secret)", "1"),
]


@pytest.mark.parametrize("sql, count", IN_TABLE)
def test_clickhouse_reads_a_name_on_the_right_of_in_as_a_table(clickhouse_local, sql, count):
    # sqlglot reads a column there; ClickHouse reads the table's rows as the
    # set, so whether 42 is in the secret table decides the count.
    assert _sqlglot_tables(sql, "clickhouse") == {("orders",)}
    code, out = clickhouse_local(sql)
    assert code == 0 and out == count
    with pytest.raises(TableScopeViolation, match="SECRET"):
        check_table_scope(ensure_read_only(object.__new__(ClickhouseClient), sql), "clickhouse", SCOPE)


def test_clickhouse_takes_only_a_table_or_constants_on_the_right_of_in(clickhouse_local):
    # A column there is an error, so reading the name as a table refuses
    # no query that runs.
    code, out = clickhouse_local("SELECT count() FROM orders WHERE 1 IN (id)")
    assert code != 0 and "constant or table expression" in out


# Each IN operator as a function, with its set (42 is in the secret table:
# 1 for an IN, 0 for a NOT IN).
IN_FUNCTION_TABLE = [
    ("SELECT count() FROM orders WHERE globalIn(42, secret)", "1"),
    ("SELECT count() FROM orders WHERE notIn(42, default.secret)", "0"),
    ("SELECT count() FROM orders WHERE nullIn(42, (secret))", "1"),
    ("SELECT count() FROM orders WHERE `globalNotIn`(42, `secret`)", "0"),
    ("SELECT count() FROM orders WHERE notNullIn(42, secret)", "0"),
    ("SELECT count() FROM orders WHERE globalNullIn(42, secret)", "1"),
    ("SELECT count() FROM orders WHERE globalNotNullIn(42, secret)", "0"),
    ("SELECT arrayJoin([globalIn(42, secret), globalIn(7, secret)]) AS hit", "1" + NL + "0"),
]


@pytest.mark.parametrize("sql, shown", IN_FUNCTION_TABLE)
def test_clickhouse_reads_the_set_of_an_in_function_from_a_table(clickhouse_local, sql, shown):
    assert _sqlglot_tables(sql, "clickhouse") <= {("orders",)}
    code, out = clickhouse_local(sql)
    assert code == 0 and out == shown
    with pytest.raises(TableScopeViolation, match="SECRET"):
        check_table_scope(ensure_read_only(object.__new__(ClickhouseClient), sql), "clickhouse", CLICKHOUSE_SCOPE)


# A WITH entry whose body is a name is an alias of that name: IN over the
# alias reads the table. sqlglot reads a CTE named `s`.
WITH_ALIAS_TABLE = [
    ("WITH secret AS s SELECT count() FROM orders WHERE 42 IN s", "1"),
    ("WITH secret AS s SELECT count() FROM orders WHERE 42 GLOBAL IN s", "1"),
    ("WITH (secret) AS s SELECT count() FROM orders WHERE 42 IN (s)", "1"),
    ("WITH secret AS s, s AS t SELECT count() FROM orders WHERE 42 NOT IN t", "0"),
    ("WITH secret AS s SELECT count() FROM orders WHERE (42) IN s", "1"),
    ("SELECT * FROM (WITH secret AS s SELECT count() FROM orders WHERE 42 IN s)", "1"),
    ("WITH secret AS s SELECT count() FROM orders WHERE globalIn(42, s)", "1"),
]


@pytest.mark.parametrize("sql, shown", WITH_ALIAS_TABLE)
def test_clickhouse_reads_in_over_a_with_alias_of_a_table_name_as_that_table(clickhouse_local, sql, shown):
    assert _sqlglot_tables(sql, "clickhouse") <= {("orders",)}
    code, out = clickhouse_local(sql)
    assert code == 0 and out == shown
    with pytest.raises(TableScopeViolation, match="SECRET"):
        check_table_scope(ensure_read_only(object.__new__(ClickhouseClient), sql), "clickhouse", CLICKHOUSE_SCOPE)


@pytest.mark.parametrize("sql, shown", [
    ("WITH 1 AS n SELECT count() FROM orders WHERE id IN (n)", "1"),
    ("WITH (1, 2, 3) AS ids SELECT count() FROM orders WHERE id IN ids", "1"),
    ("SELECT count() FROM orders WHERE id IN (1, 2)", "1"),
    ("SELECT count() FROM orders WHERE id GLOBAL IN (SELECT id FROM orders)", "1"),
    ("WITH o AS (SELECT id FROM orders) SELECT count() FROM orders WHERE id IN o", "1"),
    ("SELECT count() FROM orders WHERE globalIn(id, (1, 2))", "1"),
])
def test_clickhouse_scalar_with_aliases_and_in_over_lists_or_queries_still_read(clickhouse_local, sql, shown):
    code, out = clickhouse_local(sql)
    assert code == 0 and out == shown
    check_table_scope(ensure_read_only(object.__new__(ClickhouseClient), sql), "clickhouse", CLICKHOUSE_SCOPE)


@pytest.mark.parametrize("sql", [
    "SELECT joinGet('secret_join', 'x', toUInt64(1))",
    "SELECT joinGetOrNull('default.secret_join', 'x', toUInt64(1))",
    "SELECT dictGet('secret_dict', 'x', toUInt64(1))",
    "SELECT dictGetInt32('secret_dict', 'x', toUInt64(1))",
])
def test_clickhouse_reads_a_join_table_or_a_dictionary_named_in_a_string(clickhouse_local, sql):
    assert _sqlglot_tables(sql, "clickhouse") == set()
    code, out = clickhouse_local(sql)
    assert code == 0 and out == str(SECRET)
    with pytest.raises(TableScopeViolation, match="not allowed"):
        check_table_scope(ensure_read_only(object.__new__(ClickhouseClient), sql), "clickhouse", CLICKHOUSE_SCOPE)


def _sqlglot_cte_names(sql: str, dialect: str) -> set:
    """The WITH entry names sqlglot reads in `sql`, lowered as sqlglot
    compares them."""
    return {cte.alias_or_name.lower() for s in parse_guard_sql(sql, dialect) if s is not None
            for cte in s.find_all(exp.CTE)}


# A WITH entry that is not a query, or whose name differs in case, does not
# stand for a table of that name: the engine reads the table. sqlglot reads
# a CTE named `secret`.
CLICKHOUSE_WITH_NAMES_A_TABLE = [
    ("WITH SECRET AS (SELECT 7 AS x) SELECT x FROM secret", str(SECRET)),
    ("WITH 1 AS secret SELECT x FROM secret", str(SECRET)),
    ("WITH (SELECT 7) AS secret SELECT x FROM secret", str(SECRET)),
    ("WITH 1 AS secret SELECT x FROM orders JOIN secret ON 1 = 1", str(SECRET)),
    ("WITH 1 AS secret SELECT * FROM (SELECT x FROM secret)", str(SECRET)),
    ("WITH 1 AS secret SELECT count() FROM orders WHERE 42 IN (SELECT x FROM secret)", "1"),
]


@pytest.mark.parametrize("sql, shown", CLICKHOUSE_WITH_NAMES_A_TABLE)
def test_clickhouse_reads_the_table_a_scalar_or_differently_cased_with_entry_is_named_after(clickhouse_local, sql,
                                                                                              shown):
    assert "secret" in _sqlglot_cte_names(sql, "clickhouse")
    code, out = clickhouse_local(sql)
    assert code == 0 and out == shown
    with pytest.raises(TableScopeViolation, match="SECRET"):
        check_table_scope(ensure_read_only(object.__new__(ClickhouseClient), sql), "clickhouse", CLICKHOUSE_SCOPE)


def test_mysql_reads_the_table_a_differently_cased_cte_is_named_after(mysql):
    sql = "WITH SECRET AS (SELECT 7 AS x) SELECT x FROM secret"
    assert "secret" in _sqlglot_cte_names(sql, "mysql")
    assert mysql.rows(sql) == [(SECRET,)]
    ensure_read_only(object.__new__(MysqlClient), sql)
    with pytest.raises(TableScopeViolation, match="SECRET"):
        check_table_scope(sql, "mysql", SCOPE)


def test_mysql_and_clickhouse_still_read_a_query_cte_named_as_written(mysql, clickhouse_local):
    sql = "WITH o AS (SELECT id FROM orders) SELECT count(*) FROM o"
    assert mysql.rows(sql) == [(1,)]
    check_table_scope(ensure_read_only(object.__new__(MysqlClient), sql), "mysql", SCOPE)
    code, out = clickhouse_local(sql)
    assert code == 0 and out == "1"
    check_table_scope(ensure_read_only(object.__new__(ClickhouseClient), sql), "clickhouse", CLICKHOUSE_SCOPE)


# The set of an IN: an alias written around a name, a WITH entry that names
# another (followed through aliases, every entry of the WITH, exact names),
# and words sqlglot reads as keywords after IN. Each reads the disabled
# table (1 for an IN of 42, 0 for a NOT IN).
CLICKHOUSE_IN_SET_TABLE = [
    ("SELECT count() FROM orders WHERE globalIn(42, secret AS s)", "1"),
    ("SELECT count() FROM orders WHERE globalIn(42, (secret AS s))", "1"),
    ("SELECT count() FROM orders WHERE notIn(42, secret AS s)", "0"),
    ("SELECT arrayMap(v -> globalIn(v, secret AS s), [42])", "[1]"),
    ("WITH (secret AS x) AS s SELECT count() FROM orders WHERE 42 IN s", "1"),
    ("WITH secret AS s, (s AS q) AS t SELECT count() FROM orders WHERE 42 IN t", "1"),
    ("WITH orders AS t, secret AS orders SELECT 42 IN t", "1"),
    ("WITH 1 AS SECRET SELECT count() FROM orders WHERE 42 IN secret", "1"),
    ("WITH 1 AS SECRET, secret AS s SELECT count() FROM orders WHERE 42 IN s", "1"),
    *[(f"SELECT count() FROM orders WHERE 42 IN {name}", "1") for name in CLICKHOUSE_KEYWORD_TABLES],
    ("SELECT count() FROM orders WHERE globalIn(42, current_date)", "1"),
]


@pytest.mark.parametrize("sql, shown", CLICKHOUSE_IN_SET_TABLE)
def test_clickhouse_reads_these_in_sets_from_a_disabled_table(clickhouse_local, sql, shown):
    code, out = clickhouse_local(sql)
    assert code == 0 and out == shown
    with pytest.raises(TableScopeViolation):
        check_table_scope(ensure_read_only(object.__new__(ClickhouseClient), sql), "clickhouse", CLICKHOUSE_SCOPE)


def test_clickhouse_refuses_two_with_entries_of_one_name(clickhouse_local):
    code, out = clickhouse_local("WITH 1 AS s, secret AS s SELECT 42 IN s")
    assert code != 0 and "MULTIPLE_EXPRESSIONS_FOR_ALIAS" in out


# A setting that holds SQL runs that SQL against any table; sqlglot reads a
# string.
CLICKHOUSE_SQL_SETTINGS = [
    ("SELECT count() FROM orders SETTINGS additional_table_filters = {'orders': '42 IN secret'}", "1"),
    ("SELECT count() FROM orders SETTINGS additional_table_filters = {'orders': '7 IN secret'}", "0"),
    ("SELECT * FROM (SELECT count() FROM orders SETTINGS additional_table_filters = {'orders': '7 IN secret'})",
     "0"),
]


@pytest.mark.parametrize("sql, shown", CLICKHOUSE_SQL_SETTINGS)
def test_clickhouse_runs_the_sql_a_query_setting_holds(clickhouse_local, sql, shown):
    assert _sqlglot_tables(sql, "clickhouse") == {("orders",)}
    code, out = clickhouse_local(sql)
    assert code == 0 and out == shown
    with pytest.raises(TableScopeViolation, match="setting"):
        check_table_scope(ensure_read_only(object.__new__(ClickhouseClient), sql), "clickhouse", CLICKHOUSE_SCOPE)


@pytest.mark.parametrize("sql", [
    "SELECT count() FROM orders SETTINGS max_execution_time = 5",
    "SELECT count() FROM orders SETTINGS max_threads = 2, max_result_rows = 100, result_overflow_mode = 'break'",
])
def test_clickhouse_common_query_settings_still_read(clickhouse_local, sql):
    code, out = clickhouse_local(sql)
    assert code == 0 and out == "1"
    check_table_scope(ensure_read_only(object.__new__(ClickhouseClient), sql), "clickhouse", CLICKHOUSE_SCOPE)


def test_clickhouse_readonly_mode_refuses_even_the_common_settings_so_the_client_does_not_set_it(clickhouse_local):
    # readonly=1 would stop a query from changing any setting, the ones
    # that hold SQL included, but also max_execution_time: a floor the
    # client cannot set without refusing reads that run today. Table scope
    # refuses the settings outside its list instead.
    code, out = clickhouse_local("SET readonly = 1; SELECT count() FROM orders SETTINGS max_execution_time = 5")
    assert code != 0 and "READONLY" in out
    code, out = clickhouse_local("SET readonly = 1; SELECT count() FROM orders")
    assert code == 0 and out == "1"


def test_clickhouse_tells_whether_a_disabled_table_has_a_column(clickhouse_local):
    sql = "SELECT hasColumnInTable('default', 'secret', 'x')"
    assert _sqlglot_tables(sql, "clickhouse") == set()
    code, out = clickhouse_local(sql)
    assert code == 0 and out == "1"
    with pytest.raises(TableScopeViolation, match="not allowed"):
        check_table_scope(ensure_read_only(object.__new__(ClickhouseClient), sql), "clickhouse", CLICKHOUSE_SCOPE)


@pytest.mark.parametrize("sql, shown", [
    ("WITH 5 AS n SELECT count() FROM orders WHERE id IN (n, 1)", "1"),
    ("WITH [1, 2] AS ids SELECT count() FROM orders WHERE id IN ids", "1"),
    ("SELECT count() FROM orders WHERE (id, 'a') IN ((1, 'a'), (2, 'b'))", "1"),
    ("SELECT count() FROM orders WHERE id IN [1, -2]", "1"),
    ("WITH o AS (SELECT id FROM orders) SELECT count() FROM orders WHERE globalIn(id, o)", "1"),
])
def test_clickhouse_in_over_values_names_of_values_and_query_ctes_still_reads(clickhouse_local, sql, shown):
    code, out = clickhouse_local(sql)
    assert code == 0 and out == shown
    check_table_scope(ensure_read_only(object.__new__(ClickhouseClient), sql), "clickhouse", CLICKHOUSE_SCOPE)


# --------------------------------------------------------------- Postgres

@pytest.fixture(scope="module")
def postgres():
    _require_docker()
    import psycopg2
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("postgres:15") as container:
        conn = psycopg2.connect(host=container.get_container_host_ip(), port=int(container.get_exposed_port(5432)),
                                user=container.username, password=container.password, dbname=container.dbname)
        conn.autocommit = True
        engine = _Engine(conn)
        for sql in (
            "CREATE TABLE orders (id int, owner text)", "INSERT INTO orders VALUES (1, 'a'), (2, 'b')",
            'CREATE TABLE "Orders" (v text)', "INSERT INTO \"Orders\" VALUES ('enabled')",
            "CREATE TABLE kx (v text)", "INSERT INTO kx VALUES ('enabled')",
            'CREATE TABLE "' + K_SIGN + 'x" (v text)', "INSERT INTO \"" + K_SIGN + "x\" VALUES ('hidden')",
        ):
            engine.rows(sql)
        yield engine
        conn.close()


def test_postgres_stops_reading_at_a_nul_that_sqlglot_reads_past(postgres):
    sql = "SELECT * FROM orders -- x" + NUL + NL + "WHERE owner = 'a'"
    # sqlglot keeps the filter; the server receives the text up to the NUL.
    assert "WHERE" in parse_guard_sql(sql, "postgres")[0].sql("postgres")
    assert len(postgres.rows(sql)) == 2
    with pytest.raises(ReadOnlyViolation, match="NUL"):
        ensure_read_only(object.__new__(PostgresqlClient), sql)


@pytest.mark.parametrize("sql, read", [
    # Unquoted: Postgres folds to `orders`, which is not enabled.
    ("SELECT * FROM public.Orders", "orders"),
    # Postgres folds ASCII letters only: the Kelvin-sign table, not `kx`.
    ("SELECT v FROM public." + K_SIGN + "x", "hidden"),
])
def test_postgres_folds_names_unlike_python_and_table_scope_follows_it(postgres, sql, read):
    rows = postgres.rows(sql)
    assert (read == "orders" and len(rows) == 2) or rows == [(read,)]
    scope = TableScope.from_names(active=["public.Orders", "public.kx"],
                                  inactive=["public.orders", "public." + K_SIGN + "x"])
    with pytest.raises(TableScopeViolation):
        check_table_scope(ensure_read_only(object.__new__(PostgresqlClient), sql), "postgres", scope)
    check_table_scope(ensure_read_only(object.__new__(PostgresqlClient), 'SELECT * FROM public."Orders"'),
                      "postgres", scope)
