"""Stored procedures / functions discovered through `get_schemas()` against
real Postgres, SQL Server and MySQL containers.

Routines ride the normal schema catalog as entries marked
``metadata_json["type"] == "routine"`` (see app.data_sources.routines). The
catalog SQL is per engine and is the part a unit test cannot cover, so it runs
here against the real thing. Like test_fk_reflection, these need Docker, not
credentials, and skip cleanly without a daemon.

What the assertions defend:

* tables come through unchanged — routine discovery is additive
* every routine appears exactly once, marked, with its kind and signature
* the source text is captured (the agent reads business logic from it)
* names that collide never merge: overloads (Postgres), a procedure and a
  function sharing a name (MySQL), a function named like a table (Postgres)
* identifiers that need quoting resolve (SQL Server, a space in the name)
* an account that cannot see a routine's source gets the routine flagged,
  not dropped and not shown as empty
* the connection's schema filter and extension-owned functions are honored
"""

from __future__ import annotations

import pytest
import sqlalchemy

from app.ai.prompt_formatters import Table
from app.data_sources.routines import is_routine, routine_kind, routine_meta

pytestmark = pytest.mark.integration


def _docker_available() -> bool:
    try:
        import docker  # noqa: F401

        client = docker.from_env()
        client.ping()
        return True
    except Exception:
        return False


requires_docker = pytest.mark.skipif(
    not _docker_available(), reason="Docker daemon not reachable"
)


def _seed(url: str, statements: list[str]) -> None:
    engine = sqlalchemy.create_engine(url)
    try:
        with engine.connect() as conn:
            for stmt in statements:
                conn.execute(sqlalchemy.text(stmt))
            conn.commit()
    finally:
        engine.dispose()


def _routines(entries: list[Table]) -> dict[str, Table]:
    return {t.name: t for t in entries if is_routine(t)}


def _tables(entries: list[Table]) -> set[str]:
    return {t.name for t in entries if not is_routine(t)}


def _assert_catalog_is_consistent(client, entries: list[Table]) -> None:
    names = [t.name for t in entries]
    assert len(names) == len(set(names)), "a schema entry name repeats; the catalog is keyed by name"
    # Additive: exactly the tables get_tables() reports, routines on top.
    assert _tables(entries) == {t.name for t in client.get_tables()}


POSTGRES_SEED = [
    "CREATE SCHEMA sales",
    "CREATE SCHEMA hidden",
    "CREATE EXTENSION IF NOT EXISTS pgcrypto SCHEMA sales",
    "CREATE TABLE sales.orders (id int PRIMARY KEY, status text, amount numeric(10,2))",
    # Overloads: one name, two signatures.
    "CREATE FUNCTION sales.fiscal_quarter(d date) RETURNS text LANGUAGE sql IMMUTABLE "
    "AS $$ SELECT 'Q' || extract(quarter FROM d - interval '1 month') $$",
    "CREATE FUNCTION sales.fiscal_quarter(ts timestamp) RETURNS text LANGUAGE sql IMMUTABLE "
    "AS $$ SELECT sales.fiscal_quarter(ts::date) $$",
    # Named like a table.
    "CREATE FUNCTION sales.orders(p_status text) RETURNS SETOF sales.orders LANGUAGE sql STABLE "
    "AS $$ SELECT * FROM sales.orders WHERE status = p_status $$",
    "CREATE PROCEDURE sales.close_month(p_month date) LANGUAGE plpgsql "
    "AS $$ BEGIN PERFORM 1 FROM sales.orders WHERE status = 'complete'; END $$",
    "COMMENT ON PROCEDURE sales.close_month(date) IS 'Month-end close'",
    # Outside the connection's schema filter.
    "CREATE FUNCTION hidden.secret() RETURNS int LANGUAGE sql AS $$ SELECT 1 $$",
]


@pytest.fixture(scope="module")
def postgres_client():
    from testcontainers.postgres import PostgresContainer

    from app.data_sources.clients.postgresql_client import PostgresqlClient

    with PostgresContainer("postgres:15") as container:
        _seed(container.get_connection_url(), POSTGRES_SEED)
        yield PostgresqlClient(
            host=container.get_container_host_ip(),
            port=container.get_exposed_port(5432),
            database=container.dbname,
            user=container.username,
            password=container.password,
            schema="sales",
            index_routines=True,
        )


@requires_docker
class TestPostgresRoutines:
    def test_routines_are_indexed_next_to_unchanged_tables(self, postgres_client):
        entries = postgres_client.get_schemas()
        _assert_catalog_is_consistent(postgres_client, entries)
        routines = _routines(entries)
        assert "sales.close_month" in routines
        assert routine_kind(routines["sales.close_month"]) == "procedure"
        assert routines["sales.close_month"].description == "Month-end close"
        assert [c.name for c in routines["sales.close_month"].columns] == ["p_month"]
        assert all((c.metadata or {}).get("role") == "parameter"
                   for c in routines["sales.close_month"].columns)
        assert "status = 'complete'" in routine_meta(routines["sales.close_month"])["definition"]

    def test_overloads_collapse_into_one_entry_with_every_signature(self, postgres_client):
        fq = _routines(postgres_client.get_schemas())["sales.fiscal_quarter"]
        sigs = routine_meta(fq)["signatures"]
        assert len(sigs) == 2
        assert any("date" in s for s in sigs) and any("timestamp" in s for s in sigs)
        assert routine_kind(fq) == "function"

    def test_a_function_named_like_a_table_never_merges_with_it(self, postgres_client):
        entries = postgres_client.get_schemas()
        table = next(t for t in entries if t.name == "sales.orders")
        assert not is_routine(table)
        renamed = [r for name, r in _routines(entries).items() if name.startswith("sales.orders")]
        assert len(renamed) == 1 and renamed[0].name != "sales.orders"
        # The signature keeps the engine's own name, so the agent can call it.
        assert "sales.orders(" in routine_meta(renamed[0])["signatures"][0]

    def test_schema_filter_and_extension_functions_are_excluded(self, postgres_client):
        routines = _routines(postgres_client.get_schemas())
        assert not any(name.startswith("hidden.") for name in routines)
        # pgcrypto installs dozens of functions into `sales`; none is business logic.
        assert not any("digest" in name or "crypt" in name for name in routines)


MSSQL_SEED = [
    "CREATE TABLE dbo.orders (id INT PRIMARY KEY, status NVARCHAR(20), amount DECIMAL(10,2))",
    "CREATE FUNCTION dbo.net_amount(@amount DECIMAL(10,2), @discount DECIMAL(10,2)) "
    "RETURNS DECIMAL(10,2) AS BEGIN RETURN @amount - @discount END",
    "CREATE PROCEDURE dbo.[load daily sales] @day DATE, @rows INT OUTPUT AS "
    "BEGIN SELECT @rows = COUNT(*) FROM dbo.orders WHERE status = 'complete' END",
    "EXEC sp_addextendedproperty 'MS_Description', 'Nightly load', 'SCHEMA', 'dbo', "
    "'PROCEDURE', 'load daily sales'",
    "IF NOT EXISTS (SELECT 1 FROM sys.server_principals WHERE name = 'limited') "
    "CREATE LOGIN limited WITH PASSWORD = 'Limited!Pass123', CHECK_POLICY = OFF",
    "CREATE USER limited FOR LOGIN limited",
    "ALTER ROLE db_datareader ADD MEMBER limited",
    # Can see (and run) the function, but not read its source.
    "GRANT EXECUTE ON dbo.net_amount TO limited",
]


@pytest.fixture(scope="module")
def mssql_container():
    from testcontainers.mssql import SqlServerContainer

    with SqlServerContainer() as container:
        url = container.get_connection_url().replace("mssql+pymssql://", "mssql+pyodbc://", 1)
        if "driver=" not in url:
            url += "?driver=ODBC+Driver+18+for+SQL+Server&TrustServerCertificate=yes"
        _seed(url, MSSQL_SEED)
        yield container


def _mssql_client(container, user=None, password=None):
    from app.data_sources.clients.mssql_client import MSSQLClient

    return MSSQLClient(
        host=container.get_container_host_ip(),
        port=container.get_exposed_port(container.port),
        database=container.dbname,
        user=user or container.username,
        password=password or container.password,
        encrypt=False,
        index_routines=True,
    )


@requires_docker
class TestMssqlRoutines:
    def test_routines_are_indexed_next_to_unchanged_tables(self, mssql_container):
        client = _mssql_client(mssql_container)
        entries = client.get_schemas()
        _assert_catalog_is_consistent(client, entries)
        fn = _routines(entries)["dbo.net_amount"]
        assert routine_kind(fn) == "function"
        assert routine_meta(fn)["returns"] == "decimal(10,2)"
        assert [c.name for c in fn.columns] == ["@amount", "@discount"]
        assert "@amount - @discount" in routine_meta(fn)["definition"]

    def test_names_that_need_quoting_resolve(self, mssql_container):
        proc = _routines(_mssql_client(mssql_container).get_schemas())["dbo.load daily sales"]
        assert routine_kind(proc) == "procedure"
        assert proc.description == "Nightly load"
        meta = routine_meta(proc)
        assert meta["definition_available"] is True
        assert "status = 'complete'" in meta["definition"]
        modes = {c.name: (c.metadata or {}).get("mode") for c in proc.columns}
        assert modes == {"@day": "IN", "@rows": "OUT"}

    def test_unreadable_source_is_flagged_not_dropped(self, mssql_container):
        client = _mssql_client(mssql_container, "limited", "Limited!Pass123")
        routines = _routines(client.get_schemas())
        fn = routines["dbo.net_amount"]
        assert routine_meta(fn)["definition_available"] is False
        assert "definition" not in routine_meta(fn)
        # The procedure was never granted: invisible to this login, not listed.
        assert "dbo.load daily sales" not in routines


MYSQL_SEED = [
    "CREATE TABLE orders (id INT PRIMARY KEY, status VARCHAR(20), amount DECIMAL(10,2))",
    # A procedure and a function may share a name in MySQL.
    "CREATE PROCEDURE net(IN p_status VARCHAR(20)) "
    "BEGIN SELECT SUM(amount) FROM orders WHERE status = p_status; END",
    "CREATE FUNCTION net(p_amount DECIMAL(10,2)) RETURNS DECIMAL(10,2) DETERMINISTIC "
    "RETURN p_amount * 0.9",
]


@pytest.fixture(scope="module")
def mysql_client():
    from testcontainers.mysql import MySqlContainer

    from app.data_sources.clients.mysql_client import MysqlClient

    # Binary logging is on by default; without this a non-SUPER user cannot
    # create a function at all.
    with MySqlContainer("mysql:8").with_command("--log-bin-trust-function-creators=1") as container:
        url = container.get_connection_url().replace("mysql://", "mysql+pymysql://", 1)
        _seed(url, MYSQL_SEED)
        yield MysqlClient(
            host=container.get_container_host_ip(),
            port=container.get_exposed_port(3306),
            database=container.dbname,
            user=container.username,
            password=container.password,
            index_routines=True,
        )


@requires_docker
class TestMysqlRoutines:
    def test_same_named_procedure_and_function_stay_distinct_variants(self, mysql_client):
        entries = mysql_client.get_schemas()
        _assert_catalog_is_consistent(mysql_client, entries)
        net = _routines(entries)["net"]
        sigs = routine_meta(net)["signatures"]
        assert len(sigs) == 2
        proc_sig = next(s for s in sigs if s.startswith("procedure"))
        func_sig = next(s for s in sigs if s.startswith("function"))
        # Parameters are not cross-merged between the two variants.
        assert "p_status" in proc_sig and "p_amount" not in proc_sig
        assert "p_amount" in func_sig and "p_status" not in func_sig
        definition = routine_meta(net)["definition"]
        assert "status = p_status" in definition and "p_amount * 0.9" in definition
