"""Foreign-key introspection against real Postgres and SQL Server containers.

These live in `integrations/` because they need Docker, not because they need
credentials — unlike `ds_clients.py` they are self-contained and skip cleanly
when no daemon is reachable.

Postgres and SQL Server are the primary pair because they exercise *one* code
path (`fk_reflection.attach_foreign_keys`, reached through
`engine_pool.get_engine`) across two dialects and two drivers — psycopg and
pyodbc. MySQL is here for a different reason: it names tables bare rather than
`schema.table`, so it is the only coverage of the `key_fn` branch, where a
tuple-shaped lookup against a bare-keyed dict would match nothing and yield no
edges at all — silently, since an empty result is indistinguishable from a
schema with no constraints.

What each assertion is defending, since the shape of an FK test invites
tautology:

* that edges exist at all — the regression that motivated this
* that `references_name` matches the target's own `Table.name` **exactly**,
  because `schema_context_builder` resolves edges by string equality and a
  convention mismatch drops them silently rather than erroring
* that a table with no constraints stays empty, so "found FKs" can't be an
  artifact of attaching everything to everything
* that composite and cross-schema constraints survive, both being the cases a
  naive `information_schema` join gets wrong
"""

from __future__ import annotations

import pytest
import sqlalchemy

from app.ai.prompt_formatters import Table

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


# ---------------------------------------------------------------------------
# Seed schema
#
# Deliberately not the minimal two-table case: `orders -> customers` alone
# passes even when the schema qualifier is wrong, because there is only one
# schema to be wrong about. The cross-schema and composite constraints are what
# separate a working implementation from one that merely returns rows.
# ---------------------------------------------------------------------------

POSTGRES_SEED = [
    "CREATE SCHEMA sales",
    "CREATE SCHEMA ref",
    "CREATE TABLE ref.customers (id INT PRIMARY KEY, name VARCHAR(100))",
    "CREATE TABLE ref.regions (code VARCHAR(8), sub_code VARCHAR(8), label VARCHAR(64),"
    " PRIMARY KEY (code, sub_code))",
    "CREATE TABLE sales.orders ("
    "  id INT PRIMARY KEY,"
    "  customer_id INT REFERENCES ref.customers(id),"
    "  region_code VARCHAR(8),"
    "  region_sub_code VARCHAR(8),"
    "  FOREIGN KEY (region_code, region_sub_code) REFERENCES ref.regions(code, sub_code)"
    ")",
    "CREATE TABLE sales.line_items ("
    "  id INT PRIMARY KEY,"
    "  order_id INT REFERENCES sales.orders(id),"
    "  sku VARCHAR(32)"
    ")",
    # No constraints at all — the negative control.
    "CREATE TABLE sales.audit_log (id INT PRIMARY KEY, message VARCHAR(200))",
]

MSSQL_SEED = [
    "CREATE SCHEMA sales",
    "CREATE SCHEMA ref",
    "CREATE TABLE ref.customers (id INT PRIMARY KEY, name VARCHAR(100))",
    "CREATE TABLE ref.regions (code VARCHAR(8), sub_code VARCHAR(8), label VARCHAR(64),"
    " CONSTRAINT pk_regions PRIMARY KEY (code, sub_code))",
    "CREATE TABLE sales.orders ("
    "  id INT PRIMARY KEY,"
    "  customer_id INT REFERENCES ref.customers(id),"
    "  region_code VARCHAR(8),"
    "  region_sub_code VARCHAR(8),"
    "  CONSTRAINT fk_orders_region FOREIGN KEY (region_code, region_sub_code)"
    "    REFERENCES ref.regions(code, sub_code)"
    ")",
    "CREATE TABLE sales.line_items ("
    "  id INT PRIMARY KEY,"
    "  order_id INT REFERENCES sales.orders(id),"
    "  sku VARCHAR(32)"
    ")",
    "CREATE TABLE sales.audit_log (id INT PRIMARY KEY, message VARCHAR(200))",
]


MYSQL_SEED = [
    "CREATE TABLE customers (id INT PRIMARY KEY, name VARCHAR(100))",
    "CREATE TABLE regions (code VARCHAR(8), sub_code VARCHAR(8), label VARCHAR(64),"
    " PRIMARY KEY (code, sub_code))",
    "CREATE TABLE orders ("
    "  id INT PRIMARY KEY,"
    "  customer_id INT,"
    "  region_code VARCHAR(8),"
    "  region_sub_code VARCHAR(8),"
    "  FOREIGN KEY (customer_id) REFERENCES customers(id),"
    "  FOREIGN KEY (region_code, region_sub_code) REFERENCES regions(code, sub_code)"
    ")",
    "CREATE TABLE line_items ("
    "  id INT PRIMARY KEY,"
    "  order_id INT,"
    "  sku VARCHAR(32),"
    "  FOREIGN KEY (order_id) REFERENCES orders(id)"
    ")",
    "CREATE TABLE audit_log (id INT PRIMARY KEY, message VARCHAR(200))",
]


def _seed(url: str, statements: list[str]) -> None:
    engine = sqlalchemy.create_engine(url)
    try:
        with engine.connect() as conn:
            for stmt in statements:
                conn.execute(sqlalchemy.text(stmt))
            conn.commit()
    finally:
        engine.dispose()


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
            schema="sales,ref",
        )


@pytest.fixture(scope="module")
def mssql_client():
    from testcontainers.mssql import SqlServerContainer

    from app.data_sources.clients.mssql_client import MSSQLClient

    with SqlServerContainer() as container:
        url = container.get_connection_url().replace(
            "mssql+pymssql://", "mssql+pyodbc://", 1
        )
        if "driver=" not in url:
            url += "?driver=ODBC+Driver+18+for+SQL+Server&TrustServerCertificate=yes"
        _seed(url, MSSQL_SEED)
        yield MSSQLClient(
            host=container.get_container_host_ip(),
            port=container.get_exposed_port(container.port),
            database=container.dbname,
            user=container.username,
            password=container.password,
            schema="sales,ref",
            encrypt=False,
        )


@pytest.fixture(scope="module")
def mysql_client():
    from testcontainers.mysql import MySqlContainer

    from app.data_sources.clients.mysql_client import MysqlClient

    with MySqlContainer("mysql:8") as container:
        url = container.get_connection_url().replace("mysql://", "mysql+pymysql://", 1)
        _seed(url, MYSQL_SEED)
        yield MysqlClient(
            host=container.get_container_host_ip(),
            port=container.get_exposed_port(3306),
            database=container.dbname,
            user=container.username,
            password=container.password,
        )


# ---------------------------------------------------------------------------
# Shared assertions, run identically against both dialects
# ---------------------------------------------------------------------------


def _by_name(tables: list[Table]) -> dict[str, Table]:
    return {t.name: t for t in tables}


def _edges(table: Table) -> set[tuple[str, str, str]]:
    """(local column, target table, target column) for each edge."""
    return {
        (fk.column.name, fk.references_name, fk.references_column.name)
        for fk in (table.fks or [])
    }


def _assert_relationships_discovered(tables: list[Table]) -> None:
    by_name = _by_name(tables)
    orders = by_name.get("sales.orders")
    assert orders is not None, f"sales.orders missing from {sorted(by_name)}"

    edges = _edges(orders)
    assert edges, "sales.orders has no foreign keys — introspection returned none"

    # Single-column, cross-schema.
    assert ("customer_id", "ref.customers", "id") in edges, edges
    # Composite, cross-schema: both column pairs survive the flattening into
    # single-column ForeignKey rows.
    assert ("region_code", "ref.regions", "code") in edges, edges
    assert ("region_sub_code", "ref.regions", "sub_code") in edges, edges

    # Same-schema edge, to prove the qualifier isn't hardcoded to the target's.
    line_items = by_name["sales.line_items"]
    assert ("order_id", "sales.orders", "id") in _edges(line_items)


def _assert_references_resolve(tables: list[Table]) -> None:
    """Every edge must name a table that exists, spelled exactly as it is elsewhere.

    This is the assertion that would have caught a schema-qualifier mismatch:
    the edges are all present and well-formed, and still useless, because
    nothing downstream can match them to a table.
    """
    names = {t.name for t in tables}
    unresolved = [
        (t.name, fk.references_name)
        for t in tables
        for fk in (t.fks or [])
        if fk.references_name not in names
    ]
    assert not unresolved, f"edges pointing at unknown tables: {unresolved}"


def _assert_no_false_positives(tables: list[Table]) -> None:
    by_name = _by_name(tables)
    audit = by_name["sales.audit_log"]
    assert not (audit.fks or []), f"audit_log should have no FKs, got {audit.fks}"
    customers = by_name["ref.customers"]
    assert not (customers.fks or []), "customers is a target, not a source"


@requires_docker
class TestPostgresForeignKeys:
    def test_relationships_are_discovered(self, postgres_client):
        _assert_relationships_discovered(postgres_client.get_tables())

    def test_references_resolve_to_known_tables(self, postgres_client):
        _assert_references_resolve(postgres_client.get_tables())

    def test_tables_without_constraints_stay_empty(self, postgres_client):
        _assert_no_false_positives(postgres_client.get_tables())

    def test_basic_path_also_carries_relationships(self, postgres_client):
        """The no-comments fallback must not silently drop edges.

        `get_tables()` degrades to `_get_tables_basic()` whenever the enriched
        query raises — on a locked-down catalog that is the *normal* path, so an
        implementation that only wires FKs into the enriched query leaves the
        most restricted deployments exactly as blind as before.
        """
        _assert_relationships_discovered(postgres_client._get_tables_basic())


@requires_docker
class TestMssqlForeignKeys:
    def test_relationships_are_discovered(self, mssql_client):
        _assert_relationships_discovered(mssql_client.get_tables())

    def test_references_resolve_to_known_tables(self, mssql_client):
        _assert_references_resolve(mssql_client.get_tables())

    def test_tables_without_constraints_stay_empty(self, mssql_client):
        _assert_no_false_positives(mssql_client.get_tables())

    def test_basic_path_also_carries_relationships(self, mssql_client):
        _assert_relationships_discovered(mssql_client._get_tables_basic())


@requires_docker
class TestMysqlForeignKeys:
    """MySQL names tables bare, so the expected strings differ from the pair above.

    Same helper, same assertions in spirit — what is being proved here is that
    the reference string follows the client's own convention rather than a
    qualified one it never uses.
    """

    def test_relationships_are_discovered(self, mysql_client):
        by_name = _by_name(mysql_client.get_tables())
        edges = _edges(by_name["orders"])

        assert ("customer_id", "customers", "id") in edges, edges
        assert ("region_code", "regions", "code") in edges, edges
        assert ("region_sub_code", "regions", "sub_code") in edges, edges
        assert ("order_id", "orders", "id") in _edges(by_name["line_items"])

    def test_references_are_unqualified_like_the_table_names(self, mysql_client):
        """A `schema.table` reference here would resolve to nothing downstream."""
        tables = mysql_client.get_tables()
        for table in tables:
            for fk in table.fks or []:
                assert "." not in fk.references_name, fk.references_name

    def test_references_resolve_to_known_tables(self, mysql_client):
        _assert_references_resolve(mysql_client.get_tables())

    def test_tables_without_constraints_stay_empty(self, mysql_client):
        by_name = _by_name(mysql_client.get_tables())
        assert not (by_name["audit_log"].fks or [])
        assert not (by_name["customers"].fks or [])

    def test_basic_path_also_carries_relationships(self, mysql_client):
        by_name = _by_name(mysql_client._get_tables_basic())
        assert ("customer_id", "customers", "id") in _edges(by_name["orders"])
