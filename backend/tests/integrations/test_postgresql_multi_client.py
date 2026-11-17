"""The client behind a `postgresql_multi` connection, against a real server.

Indexing discovers tenant databases on the server, reads each one's catalog
and returns unified `<schema>.<table>` entries; querying runs agent SQL on the
DuckDB engine over the tenants the index recorded. These promises are about
real discovery, real privileges and real DuckDB name resolution, so they run
against the shared multi-tenant Postgres container.
"""

from __future__ import annotations

import re

import pytest

from app.ai.code_execution.sql_guard import dialect_for_client
from app.ai.code_execution.table_scope import TableScopeViolation
from app.data_sources.clients.postgresql_client import PostgresqlClient
from app.data_sources.clients.postgresql_multi_client import PostgresqlMultiClient
from app.data_sources.clients.postgresql_multi_engine import invalidate_engines
from app.data_sources.clients.progress import IndexingCancelled
from tests.integrations.postgresql_multi_fixture import (
    LOCKED_TENANT,
    READABLE_TENANTS,
    TENANT_ROWS,
    _seed_tenant,
    requires_docker,
    running_multi_tenant_server,
)

pytestmark = [pytest.mark.integration, requires_docker]

ALL_TENANTS_SPEC = "tenant_*, Tenant_A, public"


@pytest.fixture(scope="module")
def multi_tenant_server():
    with running_multi_tenant_server() as server:
        yield server
    invalidate_engines()


def _client(server, databases=ALL_TENANTS_SPEC, **kwargs) -> PostgresqlMultiClient:
    return PostgresqlMultiClient(
        host=server.host,
        port=server.port,
        databases=databases,
        user=server.reader_user,
        password=server.reader_password,
        sslmode="disable",
        **kwargs,
    )


def _as_stored(tables) -> list[dict]:
    return [{"name": t.name, "metadata_json": t.metadata_json} for t in tables]


def test_index_returns_one_table_per_schema_table_with_tenant_names(multi_tenant_server):
    tables = _client(multi_tenant_server).get_schemas()

    assert sorted(t.name for t in tables) == ["public.orders", "public.users"]
    for table in tables:
        assert table.columns[0].name == "tenant_db"
        assert sorted(table.metadata_json["tenants"]) == sorted(READABLE_TENANTS)
    orders = next(t for t in tables if t.name == "public.orders")
    assert {"id", "user_id", "total", "note"} <= {c.name for c in orders.columns}


def test_spec_exclusion_and_case_sensitivity(multi_tenant_server):
    client = _client(multi_tenant_server, databases="tenant_*, !tenant_old")

    tables = client.get_schemas()

    assert tables
    for table in tables:
        assert table.metadata_json["tenants"] == ["tenant_a"]
    unread = [d["name"] for d in client.index_stats()["unreadable_datasets"]]
    assert unread == [LOCKED_TENANT]


def test_unreadable_tenant_reported_in_index_stats(multi_tenant_server):
    client = _client(multi_tenant_server)

    tables = client.get_schemas()
    stats = client.index_stats()

    assert [d["name"] for d in stats["unreadable_datasets"]] == [LOCKED_TENANT]
    assert stats["unreadable_dataset_count"] == 1
    assert all(d["reason"] for d in stats["unreadable_datasets"])
    assert all(LOCKED_TENANT not in t.metadata_json["tenants"] for t in tables)
    assert client.unindexed_table_prefixes() == []


@pytest.mark.parametrize("spec", ["nothing_like_this_*", "!tenant_a"])
def test_index_fails_when_no_database_matches(multi_tenant_server, spec):
    with pytest.raises(RuntimeError) as raised:
        _client(multi_tenant_server, databases=spec).get_schemas()

    assert spec in str(raised.value)


def test_index_fails_when_no_matching_tenant_is_readable(multi_tenant_server):
    client = _client(multi_tenant_server, databases=LOCKED_TENANT)

    with pytest.raises(RuntimeError) as raised:
        client.get_schemas()

    assert re.search(r"\b1\b", str(raised.value))
    assert str(multi_tenant_server.port) not in str(raised.value)


@pytest.mark.parametrize("error", [TypeError, NotImplementedError, RecursionError, IndexingCancelled])
def test_non_connection_error_in_a_tenant_read_fails_the_index(multi_tenant_server, monkeypatch, error):
    def broken(self, progress_callback=None):
        raise error("boom")

    monkeypatch.setattr(PostgresqlClient, "get_tables", broken)

    with pytest.raises(error):
        _client(multi_tenant_server).get_schemas()


def test_discovery_failure_fails_the_index(multi_tenant_server):
    client = _client(multi_tenant_server, discovery_database="no_such_database")

    with pytest.raises(RuntimeError):
        client.get_schemas()


def test_table_exclude_pattern_matches_schema_qualified_name(multi_tenant_server):
    tables = _client(multi_tenant_server, table_exclude_pattern=r"^public\.users$").get_schemas()

    assert [t.name for t in tables] == ["public.orders"]


def test_query_after_metadata_attach_uses_indexed_tenants(multi_tenant_server):
    indexed = _client(multi_tenant_server, databases="tenant_*, public").get_schemas()
    fresh = _client(multi_tenant_server, databases="tenant_*, public")
    fresh.attach_table_metadata(_as_stored(indexed))

    df = fresh.execute_query("SELECT DISTINCT tenant_db FROM public.orders")

    assert sorted(df["tenant_db"]) == ["public", "tenant_a", "tenant_old"]


def test_query_counts_match_each_tenant(multi_tenant_server):
    client = _client(multi_tenant_server)
    client.get_schemas()

    df = client.execute_query(
        "SELECT tenant_db, count(*) AS n FROM public.orders GROUP BY tenant_db"
    )

    counts = dict(zip(df["tenant_db"], df["n"], strict=True))
    assert counts == {t: len(TENANT_ROWS[t]["orders"]) for t in READABLE_TENANTS}


def test_description_names_join_rule_and_missing_tenants(multi_tenant_server):
    client = _client(multi_tenant_server)
    client.get_schemas()

    description = client.description

    assert "tenant_db" in description
    assert LOCKED_TENANT in description
    for tenant in READABLE_TENANTS:
        assert tenant in description


def test_fresh_client_description_names_tenants_unread_at_index(multi_tenant_server):
    indexed = _client(multi_tenant_server).get_schemas()
    fresh = _client(multi_tenant_server)
    fresh.attach_table_metadata(_as_stored(indexed))

    assert LOCKED_TENANT in fresh.description


def test_tenant_unreadable_at_query_time_is_named_in_description(multi_tenant_server):
    late = "tenant_revoked_after_index"
    multi_tenant_server.create_database(late)
    _seed_tenant(multi_tenant_server, late, TENANT_ROWS["tenant_a"])
    spec = f"{late}, tenant_a"
    indexed = _client(multi_tenant_server, databases=spec).get_schemas()
    admin = multi_tenant_server.connect("postgres")
    admin.autocommit = True
    try:
        with admin.cursor() as cur:
            cur.execute(f'REVOKE CONNECT ON DATABASE "{late}" FROM PUBLIC')
    finally:
        admin.close()
    fresh = _client(multi_tenant_server, databases=spec)
    fresh.attach_table_metadata(_as_stored(indexed))

    df = fresh.execute_query("SELECT DISTINCT tenant_db FROM public.orders")

    assert list(df["tenant_db"]) == ["tenant_a"]
    assert any(late in line and "tenant_a" not in line for line in fresh.description.splitlines())


def test_description_leaves_out_raw_connection_errors(multi_tenant_server):
    client = _client(multi_tenant_server)
    client.get_schemas()

    description = client.description

    reasons = [d["reason"] for d in client.index_stats()["unreadable_datasets"]]
    assert any(str(multi_tenant_server.port) in r for r in reasons)
    assert str(multi_tenant_server.port) not in description
    assert multi_tenant_server.reader_user not in description
    assert "permission denied" not in description.lower()


def test_bare_table_name_is_refused(multi_tenant_server):
    client = _client(multi_tenant_server)
    client.get_schemas()

    with pytest.raises(TableScopeViolation):
        client.execute_query("SELECT * FROM orders")


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM postgres_query('__t0001', 'select 1')",
        "SELECT * FROM postgres_scan('dbname=tenant_a', 'public', 'orders')",
        "SELECT * FROM read_csv('/etc/passwd')",
    ],
)
def test_denied_function_is_refused_without_a_table_scope(multi_tenant_server, sql):
    client = _client(multi_tenant_server)
    client.get_schemas()

    with pytest.raises(TableScopeViolation):
        client.execute_query(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM query_table('__t0001.public.orders')",
        "SELECT * FROM public.orders o, LATERAL query_table('__t0001.public.orders') x",
        "PRAGMA table_info('__t0001.public.orders')",
        "SELECT * FROM __t0001.public.orders",
        "SELECT * FROM __t0001.orders",
        "SELECT * FROM public.no_such_table",
    ],
)
def test_table_outside_the_unified_views_is_refused(multi_tenant_server, sql):
    client = _client(multi_tenant_server)
    client.get_schemas()

    with pytest.raises(TableScopeViolation):
        client.execute_query(sql)


def test_memory_qualified_view_is_allowed(multi_tenant_server):
    client = _client(multi_tenant_server)
    client.get_schemas()

    df = client.execute_query("SELECT count(*) AS n FROM memory.public.users")

    assert int(df["n"][0]) == sum(len(TENANT_ROWS[t]["users"]) for t in READABLE_TENANTS)


def test_client_is_checked_as_duckdb(multi_tenant_server):
    client = _client(multi_tenant_server)

    assert dialect_for_client(client) == "duckdb"
    assert client.exact_table_scope_catalogs == ("memory",)


def test_connection_fails_when_no_database_matches(multi_tenant_server):
    for spec in ("nothing_like_this_*", "!tenant_a"):
        result = _client(multi_tenant_server, databases=spec).test_connection()

        assert result["success"] is False
        assert spec in result["message"]


def test_connection_fails_when_no_matching_database_is_readable(multi_tenant_server):
    result = _client(multi_tenant_server, databases=LOCKED_TENANT).test_connection()

    assert result["success"] is False
    assert LOCKED_TENANT in result["message"]


def test_connection_succeeds_when_a_tenant_is_readable(multi_tenant_server):
    result = _client(multi_tenant_server).test_connection()

    assert result["success"] is True
    assert LOCKED_TENANT in result["message"]
