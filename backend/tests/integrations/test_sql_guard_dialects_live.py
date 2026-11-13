"""The engines read these texts differently from sqlglot, and the guard now
refuses them before anything is sent.

Contract, on real servers (MySQL 8, MariaDB 11.4, SQL Server 2019,
Postgres 15): for each shape, sqlglot's reading finds only active tables or
no write, the engine runs more (a hidden read of another table, a file
written, a table created, a filter dropped), and `ensure_read_only` or
table scope refuses the text. The engine is driven through its own driver,
never through the client, so the guard is not in the way of the proof.
Where an engine reads a character the way sqlglot does (SQL Server's
spaces, its nested comments), the exemption the guard makes for it is
pinned here.

Needs Docker. CI does not collect tests/integrations; these run on the test
harness. Without a Docker daemon they skip, or fail when CI is set.
"""

from __future__ import annotations

import os

import pytest

from app.ai.code_execution.sql_guard import ReadOnlyViolation, ensure_read_only
from app.ai.code_execution.sql_parser import parse_guard_sql
from app.ai.code_execution.table_scope import TableScope, TableScopeViolation, check_table_scope, tables_read
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


@pytest.mark.parametrize("glued, table", [("0x1into", "t_hex"), ("$1into", "t_money")])
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
