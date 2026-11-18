"""The client behind a `postgresql_multi` connection, against a real server.

Indexing discovers tenant databases on the server, reads each one's catalog
and returns unified `<schema>.<table>` entries; querying runs agent SQL on the
DuckDB engine over the tenants the index recorded. These promises are about
real discovery, real privileges and real DuckDB name resolution, so they run
against the shared multi-tenant Postgres container.
"""

from __future__ import annotations

import logging
import re
import time
from types import SimpleNamespace

import duckdb
import pytest

from app.ai.code_execution.sql_guard import dialect_for_client
from app.ai.code_execution.table_scope import TableScopeViolation
from app.data_sources.clients import postgresql_multi_engine as engine_module
from app.data_sources.clients.postgresql_client import PostgresqlClient
from app.data_sources.clients.postgresql_multi_client import PostgresqlMultiClient
from app.data_sources.clients.postgresql_multi_engine import invalidate_engines
from app.data_sources.clients.progress import IndexingCancelled
from app.data_sources.fast import extractor
from app.data_sources.fast.sources import source_for
from app.services.table_preview_service import preview_sql
from tests.integrations.postgresql_multi_fixture import (
    GRANTLESS_TENANT,
    LOCKED_TENANT,
    READABLE_TENANTS,
    TENANT_ROWS,
    UNREADABLE_TENANTS,
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


def _client(server, databases=ALL_TENANTS_SPEC, connection_id="conn-shared", **kwargs) -> PostgresqlMultiClient:
    client = PostgresqlMultiClient(
        host=server.host,
        port=server.port,
        databases=databases,
        user=server.reader_user,
        password=server.reader_password,
        sslmode="disable",
        **kwargs,
    )
    if connection_id:
        client._bow_connection_id = connection_id
    return client


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
    assert sorted(unread) == sorted(UNREADABLE_TENANTS)


def test_unreadable_tenant_reported_in_index_stats(multi_tenant_server):
    client = _client(multi_tenant_server)

    tables = client.get_schemas()
    stats = client.index_stats()

    assert sorted(d["name"] for d in stats["unreadable_datasets"]) == sorted(UNREADABLE_TENANTS)
    assert stats["unreadable_dataset_count"] == len(UNREADABLE_TENANTS)
    assert all(d["reason"] for d in stats["unreadable_datasets"])
    for tenant in UNREADABLE_TENANTS:
        assert all(tenant not in t.metadata_json["tenants"] for t in tables)
    assert client.unindexed_table_prefixes() == []


@pytest.mark.parametrize("spec", ["nothing_like_this_*", "!tenant_a"])
def test_index_fails_when_no_database_matches(multi_tenant_server, spec):
    with pytest.raises(RuntimeError) as raised:
        _client(multi_tenant_server, databases=spec).get_schemas()

    assert spec in str(raised.value)


@pytest.mark.parametrize("tenant", UNREADABLE_TENANTS)
def test_index_fails_when_no_matching_tenant_is_readable(multi_tenant_server, tenant):
    client = _client(multi_tenant_server, databases=tenant)

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
    assert GRANTLESS_TENANT in description
    for tenant in READABLE_TENANTS:
        assert tenant in description


def test_fresh_client_description_names_tenants_unread_at_index(multi_tenant_server):
    indexed = _client(multi_tenant_server).get_schemas()
    fresh = _client(multi_tenant_server)
    fresh.attach_table_metadata(_as_stored(indexed))

    assert LOCKED_TENANT in fresh.description


def _revoke_connect(server, database: str) -> None:
    admin = server.connect("postgres")
    admin.autocommit = True
    try:
        with admin.cursor() as cur:
            cur.execute(f'REVOKE CONNECT ON DATABASE "{database}" FROM PUBLIC')
    finally:
        admin.close()


def _count_builds(monkeypatch) -> list:
    built: list = []
    real_build = engine_module.build_engine

    def counting(*args, **kwargs):
        engine = real_build(*args, **kwargs)
        built.append(engine)
        return engine

    monkeypatch.setattr(engine_module, "build_engine", counting)
    return built


def test_tenant_unreadable_at_query_time_is_logged_and_named_to_every_client(
    multi_tenant_server, monkeypatch, caplog
):
    late = "revoked_after_index"
    multi_tenant_server.create_database(late)
    _seed_tenant(multi_tenant_server, late, TENANT_ROWS["tenant_a"])
    spec = f"{late}, tenant_a"
    stored = _as_stored(_client(multi_tenant_server, databases=spec).get_schemas())
    _revoke_connect(multi_tenant_server, late)
    invalidate_engines()
    builds = _count_builds(monkeypatch)

    waiting = _client(multi_tenant_server, databases=spec)
    waiting.attach_table_metadata(stored)
    assert not any(late in line and "tenant_a" not in line for line in waiting.description.splitlines())
    assert builds == []

    fresh = _client(multi_tenant_server, databases=spec)
    fresh.attach_table_metadata(stored)
    with caplog.at_level(logging.WARNING):
        df = fresh.execute_query("SELECT DISTINCT tenant_db FROM public.orders")

    assert list(df["tenant_db"]) == ["tenant_a"]
    assert any(late in r.getMessage() for r in caplog.records if r.levelno == logging.WARNING)
    assert any(late in line and "tenant_a" not in line for line in fresh.description.splitlines())
    assert any(late in line and "tenant_a" not in line for line in waiting.description.splitlines())
    assert len(builds) == 1


def test_tenant_missing_a_table_at_query_time_is_named_for_that_table(multi_tenant_server):
    shrinking = "drops_users_after_index"
    multi_tenant_server.create_database(shrinking)
    _seed_tenant(multi_tenant_server, shrinking, TENANT_ROWS["tenant_a"])
    spec = f"{shrinking}, public"
    stored = _as_stored(_client(multi_tenant_server, databases=spec).get_schemas())
    conn = multi_tenant_server.connect(shrinking)
    try:
        with conn.cursor() as cur:
            cur.execute("ALTER TABLE public.orders DROP CONSTRAINT orders_user_id_fkey")
            cur.execute("DROP TABLE public.users")
        conn.commit()
    finally:
        conn.close()
    invalidate_engines()
    client = _client(multi_tenant_server, databases=spec)
    client.attach_table_metadata(stored)

    users = client.execute_query("SELECT DISTINCT tenant_db FROM public.users")
    orders = client.execute_query("SELECT DISTINCT tenant_db FROM public.orders")

    assert list(users["tenant_db"]) == ["public"]
    assert sorted(orders["tenant_db"]) == sorted([shrinking, "public"])
    assert any(shrinking in line and "public.users" in line for line in client.description.splitlines())


def _alter_tenant(server, database: str, statement: str) -> None:
    conn = server.connect(database)
    try:
        with conn.cursor() as cur:
            cur.execute(statement)
        conn.commit()
    finally:
        conn.close()


@pytest.mark.parametrize(("vanishing", "change"), [
    ("vanishing_dropped", "DROP TABLE public.orders"),
    ("vanishing_renamed", "ALTER TABLE public.orders RENAME TO orders_archived"),
])
def test_table_gone_from_one_tenant_after_build_leaves_the_others_queryable(
    multi_tenant_server, monkeypatch, caplog, vanishing, change
):
    multi_tenant_server.create_database(vanishing)
    _seed_tenant(multi_tenant_server, vanishing, TENANT_ROWS["tenant_a"])
    spec = f"{vanishing}, public, tenant_old"
    client = _client(multi_tenant_server, databases=spec, connection_id=f"conn-{vanishing}")
    client.get_schemas()
    assert len(client.execute_query("SELECT DISTINCT tenant_db FROM public.orders")) == 3
    _alter_tenant(multi_tenant_server, vanishing, change)
    builds = _count_builds(monkeypatch)

    with caplog.at_level(logging.WARNING):
        df = client.execute_query("SELECT tenant_db, count(*) AS n FROM public.orders GROUP BY tenant_db")

    assert dict(zip(df["tenant_db"], df["n"], strict=True)) == {
        t: len(TENANT_ROWS[t]["orders"]) for t in ("public", "tenant_old")
    }
    assert any(f"conn-{vanishing}" in r.getMessage() for r in caplog.records if r.levelno == logging.WARNING)
    assert any(vanishing in line and "public.orders" in line for line in client.description.splitlines())
    assert len(builds) == 1
    assert len(client.execute_query("SELECT * FROM public.users")) > 0
    assert len(builds) == 1


def test_table_no_tenant_can_serve_at_query_time_is_named_in_the_description(multi_tenant_server):
    _create_tenant(multi_tenant_server, "sole_1", ["CREATE TABLE public.items (id int)",
                                                  "CREATE TABLE public.extras (id int)"])
    _create_tenant(multi_tenant_server, "sole_2", ["CREATE TABLE public.items (id int)"])
    stored = _as_stored(_client(multi_tenant_server, databases="sole_*").get_schemas())
    _revoke_connect(multi_tenant_server, "sole_1")
    invalidate_engines()
    client = _client(multi_tenant_server, databases="sole_*")
    client.attach_table_metadata(stored)

    client.execute_query("SELECT count(*) FROM public.items")

    assert any("public.extras" in line for line in client.description.splitlines())
    assert "public.items" not in client.description


_COPY_FAILURE = (
    'Failed to prepare COPY "COPY (SELECT "id" FROM "public"."orders" ) TO STDOUT (FORMAT "binary");": '
    'ERROR:  relation "public.orders" does not exist'
)


@pytest.mark.parametrize("sql", [
    "SELECT no_such_column FROM public.orders",
    f"SELECT * FROM public.orders WHERE error('{_COPY_FAILURE}') IS NULL",
    "SELECT * FROM public.orders WHERE error('failed to prepare copy') IS NULL",
    "SELECT * FROM public.orders WHERE error('server closed the connection unexpectedly') IS NULL",
    "SELECT * FROM public.orders WHERE error('Unable to connect to Postgres') IS NULL",
    f"SELECT CAST('{_COPY_FAILURE}' AS INTEGER) FROM public.orders",
])
def test_error_raised_by_agent_sql_does_not_rebuild_the_engine(multi_tenant_server, monkeypatch, sql):
    client = _client(multi_tenant_server, connection_id="conn-agent-errors")
    client.get_schemas()
    client.execute_query("SELECT count(*) FROM public.orders")
    builds = _count_builds(monkeypatch)

    with pytest.raises(duckdb.Error):
        client.execute_query(sql)

    assert builds == []


_FORGED_TAIL = ') TO STDOUT (FORMAT "binary");": ERROR:  relation "x" does not exist'


def _forge_tenants(server, prefix: str) -> list[str]:
    tenants = [f"{prefix}_1", f"{prefix}_2"]
    for tenant in tenants:
        server.create_database(tenant)
        _seed_tenant(server, tenant, TENANT_ROWS["tenant_a"])
    return tenants


def test_forged_vanished_table_error_neither_rebuilds_nor_blocks_a_real_recovery(multi_tenant_server, monkeypatch):
    tenants = _forge_tenants(multi_tenant_server, "forge")
    client = _client(multi_tenant_server, databases="forge_*", connection_id="conn-forge")
    client.get_schemas()
    client.execute_query("SELECT count(*) FROM public.orders")
    builds = _count_builds(monkeypatch)

    with pytest.raises(duckdb.Error):
        client.execute_query(f"SELECT * FROM public.orders WHERE note = '{_FORGED_TAIL}' || chr(0)")
    assert builds == []

    _alter_tenant(multi_tenant_server, tenants[0], "DROP TABLE public.orders")
    df = client.execute_query("SELECT tenant_db, count(*) AS n FROM public.orders GROUP BY tenant_db")

    assert dict(zip(df["tenant_db"], df["n"], strict=True)) == {tenants[1]: len(TENANT_ROWS["tenant_a"]["orders"])}
    assert len(builds) == 1


def test_tenant_left_out_after_a_drop_does_not_vouch_for_a_forged_error(multi_tenant_server, monkeypatch):
    tenants = [f"stale_{n}" for n in range(3)]
    for tenant in tenants:
        multi_tenant_server.create_database(tenant)
        _seed_tenant(multi_tenant_server, tenant, TENANT_ROWS["tenant_a"])
    offset = [0.0]
    monkeypatch.setattr(engine_module, "monotonic", lambda: time.monotonic() + offset[0])
    client = _client(multi_tenant_server, databases="stale_*", connection_id="conn-stale")
    client.get_schemas()
    client.execute_query("SELECT count(*) FROM public.orders")
    _alter_tenant(multi_tenant_server, tenants[0], "DROP TABLE public.orders")
    client.execute_query("SELECT count(*) FROM public.orders")
    offset[0] += engine_module.DEGRADED_ENGINE_TTL_SECONDS + 1
    client.execute_query("SELECT count(*) FROM public.orders")
    builds = _count_builds(monkeypatch)

    with pytest.raises(duckdb.Error):
        client.execute_query(f"SELECT * FROM public.orders WHERE note = '{_FORGED_TAIL}' || chr(0)")
    assert builds == []

    _alter_tenant(multi_tenant_server, tenants[2], "ALTER TABLE public.orders RENAME TO orders_gone")
    df = client.execute_query("SELECT tenant_db, count(*) AS n FROM public.orders GROUP BY tenant_db")

    assert dict(zip(df["tenant_db"], df["n"], strict=True)) == {tenants[1]: len(TENANT_ROWS["tenant_a"]["orders"])}
    assert len(builds) == 1


def test_vanished_table_check_reads_names_with_backslashes_on_any_string_setting(multi_tenant_server, monkeypatch):
    table = 'public."a\\b"'
    tenants = ["bs_1", "bs_2"]
    for tenant in tenants:
        _create_tenant(multi_tenant_server, tenant, [
            f"CREATE TABLE {table} (id int, note text)", f"INSERT INTO {table} VALUES (1, 'x')",
        ])
    admin = multi_tenant_server.connect("postgres")
    admin.autocommit = True
    try:
        with admin.cursor() as cur:
            cur.execute("ALTER DATABASE bs_1 SET standard_conforming_strings = off")
    finally:
        admin.close()
    client = _client(multi_tenant_server, databases="bs_*", connection_id="conn-backslash")
    client.get_schemas()
    client.execute_query(f"SELECT count(*) FROM {table}")
    builds = _count_builds(monkeypatch)

    with pytest.raises(duckdb.Error):
        client.execute_query(f"SELECT * FROM {table} WHERE note = '{_FORGED_TAIL}' || chr(0)")
    assert builds == []

    _alter_tenant(multi_tenant_server, "bs_1", f"DROP TABLE {table}")
    df = client.execute_query(f"SELECT tenant_db FROM {table}")

    assert list(df["tenant_db"]) == ["bs_2"]
    assert len(builds) == 1


@pytest.mark.parametrize("forge", [
    f"SELECT * FROM public.orders WHERE note = '{_FORGED_TAIL}'",
    f"SELECT * FROM public.orders WHERE note = '{_FORGED_TAIL}' || chr(0)",
])
def test_forged_vanished_table_error_with_a_revoked_tenant_does_not_rebuild(multi_tenant_server, monkeypatch, forge):
    prefix = "revforge_nul" if "chr(0)" in forge else "revforge"
    tenants = _forge_tenants(multi_tenant_server, prefix)
    client = _client(multi_tenant_server, databases=f"{prefix}_*", connection_id=f"conn-{prefix}")
    client.get_schemas()
    client.execute_query("SELECT count(*) FROM public.orders")
    _alter_tenant(multi_tenant_server, tenants[1], f"REVOKE SELECT ON public.orders FROM {multi_tenant_server.reader_user}")
    builds = _count_builds(monkeypatch)

    for _ in range(2):
        with pytest.raises(duckdb.Error):
            client.execute_query(forge)

    assert builds == []


def test_tenant_read_error_does_not_reveal_whether_a_word_is_the_login_or_host(multi_tenant_server):
    client = _client(multi_tenant_server, connection_id="conn-oracle")
    client.get_schemas()
    messages = {}
    for word in ("alice", multi_tenant_server.reader_user, multi_tenant_server.host, "bob"):
        with pytest.raises(duckdb.Error) as raised:
            client.execute_query(f"SELECT * FROM public.orders WHERE note = 'x {word} y' || chr(0)")
        messages[word] = str(raised.value).replace(word, "<word>")

    assert len(set(messages.values())) == 1, messages
    assert "<redacted>" not in next(iter(messages.values()))


def test_revoked_select_on_one_tenant_does_not_rebuild_the_engine(multi_tenant_server, monkeypatch):
    tenant = "select_revoked"
    multi_tenant_server.create_database(tenant)
    _seed_tenant(multi_tenant_server, tenant, TENANT_ROWS["tenant_a"])
    client = _client(multi_tenant_server, databases=f"{tenant}, public", connection_id="conn-revoked-select")
    client.get_schemas()
    client.execute_query("SELECT count(*) FROM public.orders")
    _alter_tenant(multi_tenant_server, tenant, f"REVOKE SELECT ON public.orders FROM {multi_tenant_server.reader_user}")
    builds = _count_builds(monkeypatch)

    for _ in range(2):
        with pytest.raises(duckdb.Error, match="permission denied"):
            client.execute_query("SELECT count(*) FROM public.orders")

    assert builds == []


def _reader_connections(server, databases: list[str]) -> int:
    rows = server.query("postgres", f"SELECT datname FROM pg_stat_activity WHERE usename = '{server.reader_user}'")
    return sum(1 for (datname,) in rows if datname in databases)


def _wait_for(predicate, timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return False


def _set_reader_connection_limit(server, limit: int) -> None:
    admin = server.connect("postgres")
    admin.autocommit = True
    try:
        with admin.cursor() as cur:
            cur.execute(f"ALTER ROLE {server.reader_user} CONNECTION LIMIT {int(limit)}")
    finally:
        admin.close()


def test_query_during_slot_shortage_fails_without_poisoning_the_engine(multi_tenant_server, monkeypatch):
    tenants = ["slots_1", "slots_2"]
    for tenant in tenants:
        multi_tenant_server.create_database(tenant)
        _seed_tenant(multi_tenant_server, tenant, TENANT_ROWS["public"])
    monkeypatch.setattr(engine_module, "DEFAULT_PG_IDLE_TIMEOUT_MS", 1_000)
    client = _client(multi_tenant_server, databases="slots_*", connection_id="conn-slots")
    client.get_schemas()
    client.execute_query("SELECT count(*) FROM public.orders")
    assert _wait_for(lambda: _reader_connections(multi_tenant_server, tenants) == 0)
    builds = _count_builds(monkeypatch)

    _set_reader_connection_limit(multi_tenant_server, 0)
    try:
        with pytest.raises(duckdb.Error) as raised:
            client.execute_query("SELECT count(*) FROM public.orders")
    finally:
        _set_reader_connection_limit(multi_tenant_server, -1)
    df = client.execute_query("SELECT tenant_db, count(*) AS n FROM public.orders GROUP BY tenant_db")

    assert multi_tenant_server.host not in str(raised.value)
    assert builds == []
    assert dict(zip(df["tenant_db"], df["n"], strict=True)) == {t: len(TENANT_ROWS["public"]["orders"]) for t in tenants}


def test_client_without_a_connection_keeps_its_engine_to_itself(multi_tenant_server, monkeypatch):
    import gc

    tenants = ["private_1", "private_2"]
    for tenant in tenants:
        multi_tenant_server.create_database(tenant)
        _seed_tenant(multi_tenant_server, tenant, TENANT_ROWS["public"])
    stored = _as_stored(_client(multi_tenant_server, databases="private_*").get_schemas())
    invalidate_engines()
    builds = _count_builds(monkeypatch)
    client = _client(multi_tenant_server, databases="private_*", connection_id=None)
    client.attach_table_metadata(stored)

    client.execute_query("SELECT count(*) FROM public.users")
    client.execute_query("SELECT count(*) FROM public.orders")

    assert len(builds) == 1
    assert engine_module.peek_engine(client._pg_conn(), tenants=tenants) is None
    assert _reader_connections(multi_tenant_server, tenants) > 0
    del client, builds[:]
    gc.collect()
    assert _wait_for(lambda: _reader_connections(multi_tenant_server, tenants) == 0, timeout=5)


def test_tenant_whose_catalog_read_is_incomplete_gets_a_readable_reason(multi_tenant_server, monkeypatch):
    real = PostgresqlClient.get_tables

    def partial(self, progress_callback=None):
        if self.database == "tenant_old":
            raise RuntimeError("catalog listing stopped part-way")
        return real(self, progress_callback=progress_callback)

    monkeypatch.setattr(PostgresqlClient, "get_tables", partial)
    client = _client(multi_tenant_server)
    client.get_schemas()

    reasons = {d["name"]: d["reason"] for d in client.index_stats()["unreadable_datasets"]}
    assert "incomplete" in reasons["tenant_old"]


def test_agents_with_different_tables_share_one_engine_and_keep_their_own_scope(multi_tenant_server, monkeypatch):
    stored = _as_stored(_client(multi_tenant_server).get_schemas())
    invalidate_engines()
    builds = _count_builds(monkeypatch)
    orders_agent = _client(multi_tenant_server)
    orders_agent.attach_table_metadata([t for t in stored if t["name"] == "public.orders"])
    users_agent = _client(multi_tenant_server)
    users_agent.attach_table_metadata([t for t in stored if t["name"] == "public.users"])

    orders = orders_agent.execute_query("SELECT count(*) AS n FROM public.orders")
    users = users_agent.execute_query("SELECT count(*) AS n FROM public.users")

    assert int(orders["n"][0]) == sum(len(TENANT_ROWS[t]["orders"]) for t in READABLE_TENANTS)
    assert int(users["n"][0]) == sum(len(TENANT_ROWS[t]["users"]) for t in READABLE_TENANTS)
    assert len(builds) == 1
    with pytest.raises(TableScopeViolation):
        orders_agent.execute_query("SELECT count(*) FROM public.users")
    with pytest.raises(TableScopeViolation):
        users_agent.execute_query("SELECT count(*) FROM public.orders")


def _create_tenant(server, name: str, statements: list[str]) -> None:
    server.create_database(name)
    conn = server.connect(name)
    try:
        with conn.cursor() as cur:
            for statement in statements:
                cur.execute(statement)
            cur.execute(f"GRANT SELECT ON ALL TABLES IN SCHEMA public TO {server.reader_user}")
        conn.commit()
    finally:
        conn.close()


def test_agents_whose_tables_cover_different_tenants_share_one_engine(multi_tenant_server, monkeypatch):
    _create_tenant(multi_tenant_server, "split_1", ["CREATE TABLE public.items (id int)",
                                                   "CREATE TABLE public.extras (id int)"])
    _create_tenant(multi_tenant_server, "split_2", ["CREATE TABLE public.items (id int)"])
    stored = _as_stored(_client(multi_tenant_server, databases="split_*").get_schemas())
    invalidate_engines()
    builds = _count_builds(monkeypatch)

    for table in ("public.items", "public.extras"):
        agent = _client(multi_tenant_server, databases="split_*")
        agent.attach_table_metadata([t for t in stored if t["name"] == table])
        agent.execute_query(f"SELECT count(*) FROM {table}")

    assert len(builds) == 1


def test_two_connections_with_the_same_login_get_separate_engines(multi_tenant_server, monkeypatch):
    stored = _as_stored(_client(multi_tenant_server).get_schemas())
    invalidate_engines()
    builds = _count_builds(monkeypatch)
    for connection_id in ("conn-org-1", "conn-org-2", "conn-org-1"):
        client = _client(multi_tenant_server, connection_id=connection_id)
        client.attach_table_metadata(stored)
        client.execute_query("SELECT count(*) FROM public.users")

    assert len(builds) == 2


def test_clients_without_a_connection_never_share_an_engine(multi_tenant_server, monkeypatch):
    stored = _as_stored(_client(multi_tenant_server).get_schemas())
    invalidate_engines()
    builds = _count_builds(monkeypatch)
    for _ in range(2):
        client = _client(multi_tenant_server, connection_id=None)
        client.attach_table_metadata(stored)
        client.execute_query("SELECT count(*) FROM public.users")
        client.execute_query("SELECT count(*) FROM public.orders")

    assert len(builds) == 2


def test_reindexing_one_connection_keeps_the_other_connections_engine(multi_tenant_server, monkeypatch):
    stored = _as_stored(_client(multi_tenant_server).get_schemas())
    invalidate_engines()
    other = _client(multi_tenant_server, connection_id="conn-other")
    other.attach_table_metadata(stored)
    other.execute_query("SELECT count(*) FROM public.users")
    builds = _count_builds(monkeypatch)

    _client(multi_tenant_server, connection_id="conn-reindexed").get_schemas()
    other.execute_query("SELECT count(*) FROM public.users")

    assert builds == []


def test_left_out_tables_and_tenants_are_reported_in_index_stats(multi_tenant_server):
    common = ["CREATE TABLE public.items (id int)"]
    _create_tenant(multi_tenant_server, "drift_1", [*common, "CREATE TABLE public.tags (id int, labels int[])"])
    _create_tenant(multi_tenant_server, "drift_2", [*common, "CREATE TABLE public.tags (id int, labels int[])"])
    _create_tenant(multi_tenant_server, "drift_3", [
        *common,
        "CREATE TABLE public.tags (id int, labels date[])",
        "CREATE TABLE public.audit (id int, tenant_db text)",
        "CREATE TABLE public.people (id int)",
        'CREATE TABLE public."People" (id int)',
    ])
    client = _client(multi_tenant_server, databases="drift_*")

    tables = {t.name: t for t in client.get_schemas()}
    entries = client.index_stats()["unreadable_datasets"]

    assert tables["public.tags"].metadata_json["tenants"] == ["drift_1", "drift_2"]
    assert not {"public.audit", "public.people", "public.People"} & set(tables)

    def reported(*needles):
        return [e for e in entries if all(n in f"{e['name']} {e['reason']}" for n in needles)]

    assert reported("public.tags", "drift_3", "labels")
    assert reported("public.audit")
    assert reported("public.people")
    assert reported("public.People")
    assert all(e["reason"] for e in entries)
    assert client.index_stats()["unreadable_dataset_count"] == len(entries)


def test_unreadable_reasons_name_the_cause_without_server_or_login(multi_tenant_server):
    client = _client(multi_tenant_server)
    client.get_schemas()

    reasons = {d["name"]: d["reason"] for d in client.index_stats()["unreadable_datasets"]}
    probe = _client(multi_tenant_server).test_connection()["message"]
    unreachable = _client(multi_tenant_server, discovery_database="no_such_database").test_connection()
    texts = [*reasons.values(), probe, unreachable["message"], client.description]

    assert "permission denied" in reasons[LOCKED_TENANT]
    assert unreachable["success"] is False
    assert "does not exist" in unreachable["message"]
    for text_ in texts:
        assert multi_tenant_server.host not in text_
        assert str(multi_tenant_server.port) not in text_
        assert multi_tenant_server.reader_user not in text_


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
        "SELECT x.* FROM public.orders o, LATERAL main.histogram('__t0001.public.orders', id) x",
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


@pytest.mark.parametrize("tenant", UNREADABLE_TENANTS)
def test_connection_fails_when_no_matching_database_is_readable(multi_tenant_server, tenant):
    result = _client(multi_tenant_server, databases=tenant).test_connection()

    assert result["success"] is False
    assert tenant in result["message"]


def test_connection_succeeds_when_a_tenant_is_readable(multi_tenant_server):
    result = _client(multi_tenant_server).test_connection()

    assert result["success"] is True
    for tenant in UNREADABLE_TENANTS:
        assert tenant in result["message"]
    for tenant in READABLE_TENANTS:
        assert f"{tenant}," not in result["message"].split("Not readable")[-1]


def _stored_table(table):
    return SimpleNamespace(
        name=table.name,
        metadata_json=table.metadata_json,
        columns=[{"name": c.name, "dtype": c.dtype} for c in table.columns],
    )


def test_table_preview_samples_the_unified_table(multi_tenant_server):
    client = _client(multi_tenant_server)
    orders = next(t for t in client.get_schemas() if t.name == "public.orders")
    limit = 5

    columns, rows = extractor.preview(client, preview_sql("postgresql_multi", _stored_table(orders), limit), limit)

    assert columns[0]["name"] == "tenant_db"
    assert len(rows) == limit
    assert {row[0] for row in rows} <= set(READABLE_TENANTS)


def test_custom_query_runs_over_the_unified_tables(multi_tenant_server):
    client = _client(multi_tenant_server)
    client.get_schemas()
    sql = "SELECT tenant_db, count(*) AS n FROM public.orders GROUP BY tenant_db"

    _, rows = extractor.preview(client, sql, 100)
    batches = list(source_for(client).stream_batches(sql, 1_000))

    expected = {t: len(TENANT_ROWS[t]["orders"]) for t in READABLE_TENANTS}
    assert {tenant: int(n) for tenant, n in rows} == expected
    assert sum(b.num_rows for b in batches) == len(expected)
