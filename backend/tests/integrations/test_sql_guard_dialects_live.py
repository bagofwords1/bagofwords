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
    # Disabled tables named like metrics, and disabled tables whose names
    # the catalog splits differently from a plain name, each with its own
    # value.
    + "CREATE TABLE events (x Int32) ENGINE = Memory; INSERT INTO events VALUES (7); "
    + "CREATE TABLE revenue (x Int32) ENGINE = Memory; INSERT INTO revenue VALUES (8); "
    + "".join(f"CREATE TABLE `{name.replace(chr(96), chr(96) * 2)}` (x Int32) ENGINE = Memory; "
              f"INSERT INTO `{name.replace(chr(96), chr(96) * 2)}` VALUES ({value}); "
              for name, value in (("my.table", 43), ("[hidden]", 45), ("it's", 46), ("tick" + chr(96) + "name", 47),
                                  (" padded", 48), ("padded ", 49)))
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


# Inside a WITH entry's own body its name is the table, also in the first
# operand of a recursive entry's UNION and as the set of an IN. sqlglot
# reads a CTE named `secret`.
CLICKHOUSE_OWN_NAME_IN_BODY = [
    ("WITH RECURSIVE secret AS (SELECT x FROM secret) SELECT x FROM secret", str(SECRET)),
    ("WITH RECURSIVE secret AS (SELECT x FROM secret) SELECT sum(x) FROM secret", str(SECRET)),
    ("WITH RECURSIVE secret AS (SELECT x FROM secret UNION ALL SELECT x FROM secret WHERE 0) SELECT x FROM secret",
     str(SECRET)),
    ("WITH secret AS (SELECT 42 IN secret AS r) SELECT r FROM secret", "1"),
    ("WITH secret AS (SELECT arrayFilter(v -> v IN secret, [7, 42]) AS r) SELECT r FROM secret", "[42]"),
    ("WITH secret AS (SELECT arrayJoin([globalIn(42, secret), globalIn(7, secret)]) AS hit) SELECT hit FROM secret",
     "1" + NL + "0"),
    ("WITH (SELECT groupArray(n) FROM (SELECT arrayJoin(range(50)) AS n) WHERE n IN secret) AS secret SELECT secret",
     "[42]"),
    ("WITH secret AS (SELECT * FROM (SELECT 42 IN secret AS r)) SELECT r FROM secret", "1"),
    ("WITH secret AS (SELECT globalIn(42, secret) AS r) SELECT r FROM secret", "1"),
    ("WITH (SELECT 42 IN secret) AS secret SELECT secret", "1"),
]


@pytest.mark.parametrize("sql, shown", CLICKHOUSE_OWN_NAME_IN_BODY)
def test_clickhouse_reads_the_table_a_with_entry_names_inside_its_own_body(clickhouse_local, sql, shown):
    assert "secret" in _sqlglot_cte_names(sql, "clickhouse")
    code, out = clickhouse_local(sql)
    assert code == 0 and out == shown
    with pytest.raises(TableScopeViolation, match="SECRET"):
        check_table_scope(ensure_read_only(object.__new__(ClickhouseClient), sql), "clickhouse", CLICKHOUSE_SCOPE)


# The recursive part of a recursive entry (the UNION's operands after the
# first) reads the entry, by FROM, through a subquery and as an IN set; an
# IN set in one entry reads a later entry of the same WITH. None of them
# reads a table of that name (there is none, so reading one would fail).
RECURSIVE_AND_LATER_ENTRIES = [
    ("WITH RECURSIVE r AS (SELECT 1 AS x UNION ALL SELECT x+1 FROM r WHERE x<3) SELECT sum(x) FROM r", "6"),
    ("WITH RECURSIVE walk AS (SELECT 1 AS x UNION ALL SELECT x+1 FROM walk WHERE x<3) SELECT sum(x) FROM walk", "6"),
    ("WITH RECURSIVE walk AS (SELECT 1 AS x UNION ALL SELECT x+1 FROM walk WHERE x < 3 AND 42 IN walk) "
     "SELECT sum(x) FROM walk", "1"),
    ("WITH RECURSIVE walk AS (SELECT 1 AS x UNION ALL SELECT y FROM (SELECT x AS y FROM walk WHERE x = 42)) "
     "SELECT groupArray(x) FROM walk", "[1]"),
    ("WITH a AS (SELECT 42 IN walk AS r), walk AS (SELECT 1 AS x) SELECT r FROM a", "0"),
]


@pytest.mark.parametrize("sql, shown", RECURSIVE_AND_LATER_ENTRIES)
def test_clickhouse_reads_the_entry_in_a_recursive_part_and_a_later_entry_in_an_in_set(clickhouse_local, sql, shown):
    code, out = clickhouse_local(sql)
    assert code == 0 and out == shown
    check_table_scope(ensure_read_only(object.__new__(ClickhouseClient), sql), "clickhouse", CLICKHOUSE_SCOPE)


@pytest.mark.parametrize("sql, shown", RECURSIVE_AND_LATER_ENTRIES[1:])
def test_clickhouse_the_same_entries_named_after_a_disabled_table_are_refused(clickhouse_local, sql, shown):
    # They read the entry, not the table, but an entry may not take a
    # disabled table's name: the over-refusal the name rule accepts.
    sql = sql.replace("walk", "secret")
    code, out = clickhouse_local(sql)
    assert code == 0 and out == shown
    with pytest.raises(TableScopeViolation, match=NAMED_AFTER_SECRET):
        check_table_scope(ensure_read_only(object.__new__(ClickhouseClient), sql), "clickhouse", CLICKHOUSE_SCOPE)


# WITH entries that name each other in a loop: while ClickHouse reads an
# entry it reads that entry's name as the table of that name, so the name
# that closes the loop reads the disabled table. On a table reference, as
# the set of an IN and as a scalar alias; wrapped in a UNION, a subquery, an
# inner WITH, nested subqueries, a WITH RECURSIVE; next to a recursive
# entry; over three entries. sqlglot reads a CTE named `secret`.
CLICKHOUSE_WITH_CYCLES = [
    ("WITH secret AS (SELECT x FROM orders), orders AS (SELECT x FROM secret) SELECT x FROM secret", "42"),
    ("WITH secret AS (SELECT x FROM orders), orders AS (SELECT x FROM secret) "
     "SELECT x FROM secret UNION ALL SELECT x FROM secret", "42" + NL + "42"),
    ("WITH secret AS (SELECT x FROM orders), orders AS (SELECT 0 AS x WHERE 0 UNION ALL SELECT x FROM secret) "
     "SELECT x FROM secret", "42"),
    ("SELECT * FROM (WITH secret AS (SELECT x FROM orders), orders AS (SELECT x FROM secret) SELECT x FROM secret)",
     "42"),
    ("WITH secret AS (SELECT x FROM orders), orders AS (WITH q AS (SELECT x FROM secret) SELECT x FROM q) "
     "SELECT x FROM secret", "42"),
    ("WITH secret AS (SELECT x FROM (SELECT x FROM orders)), orders AS (SELECT x FROM (SELECT x FROM secret)) "
     "SELECT x FROM secret", "42"),
    ("WITH RECURSIVE secret AS (SELECT x FROM orders), orders AS (SELECT x FROM secret) SELECT x FROM secret", "42"),
    ("WITH a AS (SELECT arrayFilter(v -> v IN secret, range(100)) AS r), secret AS (SELECT r FROM a) "
     "SELECT r FROM secret", "[42]"),
    ("WITH a AS (SELECT 42 IN secret AS r), secret AS (SELECT r FROM a) SELECT r FROM secret", "1"),
    ("WITH (SELECT arrayFilter(v -> v IN secret, range(100))) AS r, secret AS (SELECT r AS x) SELECT x FROM secret",
     "[42]"),
    ("WITH (SELECT 42 IN secret) AS r, secret AS (SELECT r + 100 AS v) SELECT 101 IN secret", "1"),
    ("WITH RECURSIVE r AS (SELECT 1 AS x UNION ALL SELECT x+1 FROM r WHERE x<3), "
     "b AS (SELECT arrayFilter(v -> v IN secret, range(100)) AS a), secret AS (SELECT a FROM b) SELECT a FROM secret",
     "[42]"),
    ("WITH secret AS (SELECT x FROM orders), c AS (SELECT x FROM secret), orders AS (SELECT x FROM c) "
     "SELECT x FROM secret", "42"),
    ("WITH a AS (SELECT x FROM b), b AS (SELECT x FROM secret), secret AS (SELECT x FROM a) SELECT x FROM secret",
     "42"),
]


@pytest.mark.parametrize("sql, shown", CLICKHOUSE_WITH_CYCLES)
def test_clickhouse_reads_the_table_named_by_with_entries_that_name_each_other_in_a_loop(clickhouse_local, sql,
                                                                                          shown):
    assert "secret" in _sqlglot_cte_names(sql, "clickhouse")
    code, out = clickhouse_local(sql)
    assert code == 0 and out == shown
    # Refused first because an entry takes the disabled table's name; the
    # loop check refuses them too.
    with pytest.raises(TableScopeViolation, match=NAMED_AFTER_SECRET):
        check_table_scope(ensure_read_only(object.__new__(ClickhouseClient), sql), "clickhouse", CLICKHOUSE_SCOPE)
    with pytest.raises(TableScopeViolation, match="refer to each other"):
        tables_read(parse_guard_sql(sql, "clickhouse")[0], "clickhouse")


# A WITH entry named after a disabled table. ClickHouse and MySQL 8.4
# resolve an entry's body where the entry is used, and read the name of an
# entry being resolved as the table of that name; ClickHouse calls a WITH
# lambda by its name, which may be a built-in's, and decodes escapes in a
# quoted name. So the name sqlglot reads as the entry reads the disabled
# table: across levels (FROM, a scalar subquery, a UNION, a LATERAL), through
# a lambda (named F, named after `length`, called in an arrayMap, written
# without parentheses), and under an escaped name. The rule that refuses
# each is in its comment.
NAMED_AFTER = "is not enabled for this agent, and a WITH entry may not take its name"
NAMED_AFTER_SECRET = "SECRET " + NAMED_AFTER
CROSS_LEVEL = [
    # name rule: the entry `secret`
    "WITH secret AS (SELECT x FROM orders) SELECT x FROM (WITH orders AS (SELECT x FROM secret) SELECT x FROM secret) d",
    # name rule: the entry `secret`
    "WITH secret AS (SELECT x FROM orders) SELECT (WITH orders AS (SELECT x FROM secret) SELECT x FROM secret) AS v",
    # name rule: the entry `secret`
    "WITH secret AS (SELECT x FROM orders) SELECT x FROM (WITH orders AS (SELECT x FROM secret) SELECT x FROM secret "
    "UNION ALL SELECT x FROM secret) d",
    # name rule: the entry `secret`
    "WITH secret AS (SELECT x FROM orders) SELECT 1 AS x FROM orders WHERE 0 UNION ALL "
    "SELECT x FROM (WITH orders AS (SELECT x FROM secret) SELECT x FROM secret) d",
]
CLICKHOUSE_LAMBDAS = [
    # name rule: the entry `secret`; lambda rule: the entry F
    ("WITH secret AS (SELECT F(1) AS x), (y -> (SELECT x FROM secret)) AS F SELECT x FROM secret", str(SECRET)),
    # name rule: the entry `secret`; lambda rule: the entry `length`
    ("WITH secret AS (SELECT length(1) AS x), (y -> (SELECT x FROM secret)) AS length SELECT x FROM secret",
     str(SECRET)),
    # name rule: the entry `secret`; lambda rule: the entry F
    ("WITH secret AS (SELECT arrayMap(v -> F(v), [1]) AS x), (y -> (SELECT x FROM secret)) AS F SELECT x FROM secret",
     f"[{SECRET}]"),
    # name rule: the entry `secret`; lambda rule: the entry F
    ("WITH secret AS (SELECT F(1) AS x), y -> (SELECT x FROM secret) AS F SELECT x FROM secret", str(SECRET)),
]
BS = chr(92)
# backslash rule: the quoted names (sqlglot reads `secre\x74`, not `secret`)
CLICKHOUSE_ESCAPED_NAMES = [
    "WITH `secre" + BS + "x74` AS (SELECT x FROM orders) SELECT x FROM "
    "(WITH orders AS (SELECT x FROM `secre" + BS + "x74`) SELECT x FROM `secre" + BS + "x74`)",
    'WITH "secre' + BS + 'x74" AS (SELECT x FROM orders) SELECT x FROM '
    '(WITH orders AS (SELECT x FROM "secre' + BS + 'x74") SELECT x FROM "secre' + BS + 'x74")',
]


@pytest.mark.parametrize("sql, shown", [(sql, str(SECRET)) for sql in CROSS_LEVEL[:2]]
                         + [(CROSS_LEVEL[2], str(SECRET) + NL + str(SECRET)), (CROSS_LEVEL[3], str(SECRET))]
                         + CLICKHOUSE_LAMBDAS)
def test_clickhouse_reads_the_disabled_table_an_entry_is_named_after(clickhouse_local, sql, shown):
    code, out = clickhouse_local(sql)
    assert code == 0 and out == shown
    with pytest.raises(TableScopeViolation, match=NAMED_AFTER_SECRET):
        check_table_scope(ensure_read_only(object.__new__(ClickhouseClient), sql), "clickhouse", CLICKHOUSE_SCOPE)


@pytest.mark.parametrize("sql, shown", CLICKHOUSE_LAMBDAS)
def test_clickhouse_with_lambdas_are_refused_by_their_own_rule_too(sql, shown):
    from app.ai.code_execution.table_scope import _check_clickhouse_with_lambdas

    with pytest.raises(TableScopeViolation, match="lambda"):
        _check_clickhouse_with_lambdas(parse_guard_sql(sql, "clickhouse")[0])


@pytest.mark.parametrize("sql", CLICKHOUSE_ESCAPED_NAMES)
def test_clickhouse_decodes_escapes_in_a_quoted_name_that_sqlglot_keeps(clickhouse_local, sql):
    code, out = clickhouse_local(sql)
    assert code == 0 and out == str(SECRET)
    with pytest.raises(ReadOnlyViolation, match="backslash"):
        ensure_read_only(object.__new__(ClickhouseClient), sql)
    with pytest.raises(TableScopeViolation, match="backslash"):
        check_table_scope(sql, "clickhouse", CLICKHOUSE_SCOPE)


# Aliases named after a disabled table (ClickHouse): in the SELECT list, in
# WHERE, in a subquery, and metrics named like a table. The engine reads
# the enabled table only (the disabled ones hold 42, 7 and 8); table scope
# lets them through.
@pytest.mark.parametrize("sql, shown", [
    ("SELECT id AS secret FROM orders", "1"),
    ("SELECT count() FROM orders WHERE (id + 1 AS secret) > 0", "1"),
    ("SELECT * FROM (SELECT (SELECT 1) AS secret FROM orders)", "1"),
    ("SELECT count() AS events FROM orders", "1"),
    ("SELECT sum(id) AS revenue FROM orders", "1"),
])
def test_clickhouse_aliases_named_after_a_disabled_table_read_the_enabled_table_and_still_read(clickhouse_local,
                                                                                                sql, shown):
    code, out = clickhouse_local(sql)
    assert code == 0 and out == shown
    scope = TableScope.from_names(active=["default.orders"],
                                  inactive=["default.secret", "default.events", "default.revenue"])
    check_table_scope(ensure_read_only(object.__new__(ClickhouseClient), sql), "clickhouse", scope)


# A WITH entry named after a disabled table whose name the catalog splits
# differently from a plain name (a dot, brackets, a quote or a backtick
# inside, a leading or trailing space), reached through the function form
# of a lambda: ClickHouse reads the table (each holds its own value). Name
# rule: the entry takes the table's name, compared as the catalog splits it.
ODD_TABLE_NAMES = [("my.table", "43"), ("[hidden]", "45"), ("it's", "46"), ("tick" + chr(96) + "name", "47"),
                   (" padded", "48"), ("padded ", "49")]


@pytest.mark.parametrize("name, shown", ODD_TABLE_NAMES)
def test_clickhouse_reads_an_odd_named_table_an_entry_is_named_after_through_a_function_lambda(clickhouse_local,
                                                                                                name, shown):
    quoted = chr(96) + name.replace(chr(96), chr(96) * 2) + chr(96)
    sql = (f"WITH {quoted} AS (SELECT F(1) AS x), lambda(tuple(y), (SELECT x FROM {quoted})) AS F "
           f"SELECT x FROM {quoted}")
    code, out = clickhouse_local(sql)
    assert code == 0 and out == shown
    scope = TableScope.from_names(active=["default.orders"], inactive=["default." + name])
    with pytest.raises(TableScopeViolation, match=NAMED_AFTER):
        check_table_scope(ensure_read_only(object.__new__(ClickhouseClient), sql), "clickhouse", scope)


# ClickHouse's function form of a lambda, as a WITH value (also named after
# a built-in) or under an alias, in a function's arguments or in a tuple:
# called by that name, it reads the disabled table. Lambda rule.
CLICKHOUSE_FUNCTION_LAMBDAS = [
    ("WITH lambda(tuple(y), (SELECT x FROM secret)) AS F SELECT F(1)", str(SECRET)),
    ("WITH lambda(tuple(y), (SELECT x FROM secret)) AS length SELECT length(1)", str(SECRET)),
    ("SELECT arrayMap(lambda(tuple(y), (SELECT x FROM secret)) AS f, [1]), f(5)", f"[{SECRET}]\t{SECRET}"),
    ("WITH tuple(lambda(tuple(y), (SELECT x FROM secret)) AS F) AS u SELECT F(1)", str(SECRET)),
]


@pytest.mark.parametrize("sql, shown", CLICKHOUSE_FUNCTION_LAMBDAS)
def test_clickhouse_calls_a_function_form_lambda_by_its_entry_or_alias_name(clickhouse_local, sql, shown):
    code, out = clickhouse_local(sql)
    assert code == 0 and out == shown
    with pytest.raises(TableScopeViolation, match="lambda"):
        check_table_scope(ensure_read_only(object.__new__(ClickhouseClient), sql), "clickhouse", CLICKHOUSE_SCOPE)


def test_clickhouse_function_form_lambda_passed_to_a_function_still_reads(clickhouse_local):
    sql = "SELECT arrayMap(lambda(tuple(v), v + 1), [id]) FROM orders"
    code, out = clickhouse_local(sql)
    assert code == 0 and out == "[2]"
    check_table_scope(ensure_read_only(object.__new__(ClickhouseClient), sql), "clickhouse", CLICKHOUSE_SCOPE)


# The set of an IN named like an alias defined elsewhere in the statement.
# Where ClickHouse reads the alias's value, the disabled table (holding
# 42), the IN is 1 (or the filter keeps 42); where it reads the enabled
# table (holding 1), 0 (or the filter keeps 1). The guard reads the enabled
# table in both. Refused either way.
CLICKHOUSE_IN_SET_NAMED_LIKE_AN_ALIAS = [
    # reads the disabled table
    ("WITH tuple(secret AS orders) AS unused SELECT 42 IN orders", "1"),
    ("WITH tuple(secret AS orders) AS unused SELECT globalIn(42, orders)", "1"),
    ("WITH [secret AS orders] AS unused SELECT 42 IN orders", "1"),
    ("SELECT * FROM (WITH tuple(secret AS orders) AS unused SELECT 42 IN orders)", "1"),
    ("WITH orders AS s, tuple(secret AS orders) AS u SELECT 42 IN s", "1"),
    # reads the enabled table
    ("WITH [secret AS orders] AS unused SELECT arrayFilter(v -> v IN orders, range(100))", "[1]"),
    ("WITH (secret AS orders) AS unused SELECT 42 IN orders", "0"),
    ("WITH tuple(secret AS ORDERS) AS unused SELECT 42 IN orders", "0"),
    ("WITH tuple(secret AS orders) AS unused SELECT 42 IN default.orders", "0"),
]


@pytest.mark.parametrize("sql, shown", CLICKHOUSE_IN_SET_NAMED_LIKE_AN_ALIAS)
def test_clickhouse_may_read_an_in_set_named_like_an_alias_as_the_alias(clickhouse_local, sql, shown):
    code, out = clickhouse_local(sql)
    assert code == 0 and out == shown
    with pytest.raises(TableScopeViolation, match="named like an alias"):
        check_table_scope(ensure_read_only(object.__new__(ClickhouseClient), sql), "clickhouse", CLICKHOUSE_SCOPE)


@pytest.mark.parametrize("sql", [
    "SELECT 42 IN orders, (secret AS orders) AS z",
    "SELECT 1 FROM orders WHERE (secret AS orders) IS NOT NULL OR 42 IN orders",
])
def test_clickhouse_does_not_read_a_table_as_an_alias_value_in_the_select_list_or_where(clickhouse_local, sql):
    # The engine refuses the name `secret` there; table scope refuses the
    # IN set named like an alias anyway.
    code, out = clickhouse_local(sql)
    assert code != 0 and "UNKNOWN_IDENTIFIER" in out
    with pytest.raises(TableScopeViolation, match="named like an alias"):
        check_table_scope(ensure_read_only(object.__new__(ClickhouseClient), sql), "clickhouse", CLICKHOUSE_SCOPE)


def test_clickhouse_from_does_not_follow_a_with_entry_whose_value_is_a_name(clickhouse_local):
    # FROM reads the table `orders`, not the entry `orders` (whose value
    # names the disabled table): its columns are `id`, and `x` is unknown.
    code, out = clickhouse_local("WITH secret AS orders SELECT * FROM orders")
    assert code == 0 and out == "1"
    code, out = clickhouse_local("WITH secret AS orders SELECT x FROM orders")
    assert code != 0 and "UNKNOWN_IDENTIFIER" in out
    check_table_scope(ensure_read_only(object.__new__(ClickhouseClient), "WITH secret AS orders SELECT * FROM orders"),
                      "clickhouse", CLICKHOUSE_SCOPE)


# The identity check asks about a UNION branch or a subquery whose WITH
# stands around it; the engine reads the disabled table (1 for 101 IN).
CLICKHOUSE_IDENTITY_FORMS = [
    ("WITH (SELECT 42 IN secret) AS r, secret AS (SELECT r + 100 AS v) "
     "SELECT 101 IN secret UNION ALL SELECT 101 IN secret", "1" + NL + "1"),
    ("WITH (SELECT 42 IN secret) AS r, secret AS (SELECT r + 100 AS v) SELECT * FROM (SELECT 101 IN secret)", "1"),
]


@pytest.mark.parametrize("sql, shown", CLICKHOUSE_IDENTITY_FORMS)
def test_clickhouse_identity_check_counts_a_with_around_the_part_it_checks(clickhouse_local, sql, shown):
    from app.ai.code_execution.identity_bind import _sql_binds

    code, out = clickhouse_local(sql)
    assert code == 0 and out == shown
    # reads_rows: any WITH on ClickHouse reads rows, so the identity filter is required.
    assert _sql_binds(sql, "clickhouse", ("u1",)) is not True


MYSQL_CROSS_LEVEL = [
    *[(sql, [(SECRET,)]) for sql in CROSS_LEVEL[:2]],
    (CROSS_LEVEL[2], [(SECRET,), (SECRET,)]),
    (CROSS_LEVEL[3], [(SECRET,)]),
    # name rule: the entry `secret`
    ("WITH secret AS (SELECT x FROM orders) SELECT * FROM (SELECT 1 AS k) z, "
     "LATERAL (WITH orders AS (SELECT x FROM secret) SELECT x FROM secret) d", [(1, SECRET)]),
]


@pytest.mark.parametrize("sql, rows", MYSQL_CROSS_LEVEL)
def test_mysql_reads_the_disabled_table_an_entry_is_named_after_across_levels(mysql, sql, rows):
    from app.ai.code_execution.identity_bind import _sql_binds

    assert mysql.rows(sql) == rows
    with pytest.raises(TableScopeViolation, match=NAMED_AFTER_SECRET):
        check_table_scope(ensure_read_only(object.__new__(MysqlClient), sql), "mysql", SCOPE)
    # reads_rows: any WITH on MySQL reads rows, so the identity filter is required.
    assert _sql_binds(sql, "mysql", ("u1",)) is not True


def test_clickhouse_reads_the_table_an_entry_named_after_it_reads_and_scope_checks_that_table(clickhouse_local):
    # An entry that names itself is not a loop: inside its body the name is
    # the table, checked against the scope.
    sql = "WITH secret AS (SELECT x FROM secret) SELECT x FROM secret"
    code, out = clickhouse_local(sql)
    assert code == 0 and out == str(SECRET)
    with pytest.raises(TableScopeViolation, match="SECRET"):
        check_table_scope(ensure_read_only(object.__new__(ClickhouseClient), sql), "clickhouse", CLICKHOUSE_SCOPE)
    sql = "WITH orders AS (SELECT id FROM orders WHERE id > 0) SELECT id FROM orders"
    code, out = clickhouse_local(sql)
    assert code == 0 and out == "1"
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
