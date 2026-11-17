"""Agent SQL a `postgresql_multi` connection refuses before it reaches DuckDB.

The engine attaches every tenant database under a generated alias, so its
catalog holds far more than the unified views. These refusals hold whether or
not an agent table scope is attached (a connection used outside an agent run
has none), and they need no server: nothing here is sent anywhere.
"""

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
