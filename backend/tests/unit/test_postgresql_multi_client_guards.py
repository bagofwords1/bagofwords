"""Agent SQL a `postgresql_multi` connection refuses before it reaches DuckDB.

The engine attaches every tenant database under a generated alias, so its
catalog holds far more than the unified views. These refusals hold whether or
not an agent table scope is attached (a connection used outside an agent run
has none), and they need no server: nothing here is sent anywhere.
"""

import duckdb
import pytest

from app.ai.code_execution.sql_guard import dialect_for_client
from app.ai.code_execution.table_scope import TableScopeViolation
from app.data_sources.clients.postgresql_multi_client import PostgresqlMultiClient

REFUSED = [
    "SELECT * FROM orders",
    "SELECT * FROM postgres_query('__t0001', 'select 1')",
    "SELECT * FROM postgres_scan('dbname=tenant_a', 'public', 'orders')",
    "SELECT postgres_execute('__t0001', 'select 1')",
    "SELECT * FROM read_csv('/etc/passwd')",
    "SELECT * FROM public.orders o JOIN read_parquet('x.parquet') p ON true",
    "SELECT * FROM __t0001.public.orders",
    "SELECT * FROM __t0001.orders",
    "SELECT * FROM system.main.duckdb_settings",
    "SELECT * FROM public.not_indexed",
    "SELECT FROM FROM (",
    "SELECT * FROM query_table('__t0001.public.orders')",
    "SELECT * FROM query('SELECT * FROM __t0001.public.secret')",
    "SELECT * FROM duckdb_databases()",
    "SELECT * FROM duckdb_secrets()",
    "SELECT * FROM 'orders.csv'",
    "SELECT (SELECT count(*) FROM query_table('public.orders'))",
    "SELECT query('SELECT 1')",
    "SELECT json_execute_serialized_sql('{}')",
    "SELECT which_secret('s3://bucket/x', 's3')",
    "SELECT * FROM pragma_table_info('__t0001.public.orders')",
    "SELECT * FROM glob('*')",
    "PRAGMA table_info('__t0001.public.orders')",
    "PRAGMA database_list",
]

_READERS = [
    "query_table('__t0001.public.orders')",
    "query('SELECT * FROM __t0001.public.orders')",
    "duckdb_databases()",
    "duckdb_secrets()",
    "glob('*')",
    "sql_auto_complete('SELECT')",
    "unknown_reader('__t0001.public.orders')",
]
REFUSED += [
    "SELECT * FROM public.orders o, LATERAL main.histogram('__t0001.public.orders', id) x",
    "SELECT * FROM public.orders o, LATERAL system.main.histogram('__t0001.public.orders', id) x",
    'SELECT * FROM public.orders o, LATERAL "main"."histogram"(\'__t0001.public.orders\', id) x',
    "SELECT * FROM public.orders o JOIN LATERAL main.histogram_values('__t0001.public.orders', id) x ON true",
    "WITH h AS (SELECT x.* FROM public.orders o, LATERAL main.histogram('__t0001.public.orders', id) x) "
    "SELECT * FROM h",
    "SELECT * FROM main.unknown_fn('__t0001.public.orders')",
]
REFUSED += [f"SELECT * FROM public.orders o, LATERAL {fn} x" for fn in _READERS]
REFUSED += [f"SELECT * FROM public.orders o JOIN LATERAL {fn} x ON true" for fn in _READERS]
REFUSED += [f"SELECT * FROM public.orders o LEFT JOIN LATERAL {fn} x ON true" for fn in _READERS]

ALLOWED = [
    "SELECT tenant_db, count(*) FROM public.orders GROUP BY tenant_db",
    "SELECT * FROM memory.public.users",
    "WITH o AS (SELECT * FROM public.orders) SELECT * FROM o",
    "SELECT * FROM public.orders, range(3)",
    "SELECT * FROM public.orders, json_each('[1]')",
    "SELECT 1",
    "SELECT * FROM public.orders o, LATERAL unnest([1]) u",
    "SELECT * FROM public.orders o, LATERAL (SELECT * FROM public.users u WHERE u.id = o.user_id) x",
    "SELECT * FROM public.orders WHERE note GLOB 'a*'",
    "SELECT histogram(id) FROM public.orders",
    "SELECT * FROM public.orders o, LATERAL generate_series(1, 3) g",
]


def _client() -> PostgresqlMultiClient:
    client = PostgresqlMultiClient(
        host="unreachable.invalid",
        port=5432,
        databases="tenant_*",
        user="reader",
        password="pw",
    )
    client.attach_table_metadata([
        {"name": "public.orders", "metadata_json": {"schema": "public", "tenants": ["tenant_a", "tenant_b"]}},
        {"name": "public.users", "metadata_json": {"schema": "public", "tenants": ["tenant_a"]}},
    ])
    return client


@pytest.mark.parametrize("sql", REFUSED)
def test_query_outside_the_unified_views_is_refused_without_a_scope(sql):
    with pytest.raises(TableScopeViolation):
        _client().execute_query(sql)


@pytest.mark.parametrize("sql", ALLOWED)
def test_query_over_the_unified_views_passes_the_guard(sql, monkeypatch):
    import app.data_sources.clients.postgresql_multi_client as module

    reached = []
    monkeypatch.setattr(module, "get_engine", lambda *a, **k: reached.append(sql) or (_ for _ in ()).throw(LookupError))

    with pytest.raises(LookupError):
        _client().execute_query(sql)
    assert reached == [sql]


def test_client_is_checked_as_duckdb_with_exact_memory_catalog():
    client = _client()

    assert dialect_for_client(client) == "duckdb"
    assert client.exact_table_scope_catalogs == ("memory",)


def test_metadata_for_databases_outside_the_spec_is_ignored():
    client = _client()
    client.attach_table_metadata([
        {"name": "public.orders", "metadata_json": {"schema": "public", "tenants": ["other_db"]}},
    ])

    assert "other_db" not in client.description


def test_description_names_tenants_left_out_of_a_table():
    client = _client()
    client.attach_table_metadata([
        {"name": "public.orders", "metadata_json": {
            "schema": "public", "tenants": ["tenant_a"], "excluded_tenants": ["tenant_odd"],
        }},
    ])

    lines = [line for line in client.description.splitlines() if "tenant_odd" in line]

    assert lines
    assert all("public.orders" in line for line in lines)


_SERVER_TEXT = (
    'IO Error: Unable to connect to Postgres at "dbname=tenant_a sslmode=prefer '
    "options='-c statement_timeout=300000'\": connection to server at \"unreachable.invalid\" (10.9.8.7), "
    'port 6543 failed: FATAL:  password authentication failed for user "reader"'
)


class _FailingEngine:
    failed_tenants: list = []
    left_out: list = []
    missing_views: list = []

    def __init__(self, error):
        self.error = error
        self.runs = 0

    def cursor(self):
        engine = self

        class _Cursor:
            def execute(self, sql):
                engine.runs += 1
                raise engine.error

            def close(self):
                pass

        return _Cursor()


@pytest.mark.parametrize("error", [duckdb.IOException(_SERVER_TEXT), duckdb.Error(f"Failed to prepare COPY: {_SERVER_TEXT}")])
def test_query_error_reaches_the_agent_without_server_or_login(monkeypatch, error):
    import app.data_sources.clients.postgresql_multi_client as module

    monkeypatch.setattr(module, "get_engine", lambda *a, **k: _FailingEngine(error))

    with pytest.raises(duckdb.Error) as raised:
        _client().execute_query("SELECT * FROM public.orders")

    text = str(raised.value)
    assert "password authentication failed" in text
    assert "unreachable.invalid" not in text
    assert "6543" not in text
    assert '"reader"' not in text


def test_error_in_agent_sql_reaches_the_agent_unchanged(monkeypatch):
    import app.data_sources.clients.postgresql_multi_client as module

    error = duckdb.BinderException('Binder Error: Referenced column "totl" not found in FROM clause!')
    monkeypatch.setattr(module, "get_engine", lambda *a, **k: _FailingEngine(error))

    with pytest.raises(duckdb.BinderException) as raised:
        _client().execute_query("SELECT totl FROM public.orders")

    assert str(raised.value) == str(error)
