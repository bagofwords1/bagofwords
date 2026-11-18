"""The DuckDB engine that unifies tenant databases into `tenant_db` views.

Runs against a real Postgres container (see `postgresql_multi_fixture.py`)
because every promise here is about what DuckDB's postgres extension actually
does: which tenant a row came from, how a missing column reads, whether a
tenant filter avoids scanning the other databases, and whether agent SQL can
leave the sandbox once the engine is built.
"""

from __future__ import annotations

import logging
import threading
import time
from decimal import Decimal

import duckdb
import pytest

from app.data_sources.clients import postgresql_multi_engine as engine_module
from app.data_sources.clients.postgresql_multi_engine import (
    PgConn,
    build_engine,
    get_engine,
    invalidate_engines,
    peek_engine,
)
from tests.integrations.postgresql_multi_fixture import (
    LOCKED_TENANT,
    READABLE_TENANTS,
    TENANT_ROWS,
    _seed_tenant,
    requires_docker,
    running_multi_tenant_server,
)

pytestmark = [pytest.mark.integration, requires_docker]

ALL_TABLES = ("public.orders", "public.users")


@pytest.fixture(scope="module")
def multi_tenant_server():
    with running_multi_tenant_server() as server:
        yield server


def _reader(server, *, password: str | None = None) -> PgConn:
    return PgConn(
        host=server.host,
        port=server.port,
        user=server.reader_user,
        password=server.reader_password if password is None else password,
        sslmode="disable",
    )


def _views(tenants: list[str]) -> dict[str, list[str]]:
    return {name: list(tenants) for name in ALL_TABLES}


def _build(server, tenants=READABLE_TENANTS, *, conn=None, views=None, statement_timeout_ms=60_000):
    return build_engine(
        conn or _reader(server),
        views or _views(tenants),
        statement_timeout_ms=statement_timeout_ms,
        memory_limit="512MB",
        threads=2,
        pg_connection_limit=2,
        pg_idle_timeout_ms=60_000,
    )


@pytest.fixture
def engine(multi_tenant_server):
    built = _build(multi_tenant_server)
    yield built
    built.close()


@pytest.fixture(autouse=True)
def _clean_engine_cache():
    invalidate_engines()
    yield
    invalidate_engines()


def test_unified_view_returns_rows_from_every_tenant(engine, multi_tenant_server):
    rows = engine.cursor().execute("SELECT tenant_db, count(*) FROM public.orders GROUP BY 1").fetchall()

    expected = {
        tenant: multi_tenant_server.query(tenant, "SELECT count(*) FROM public.orders")[0][0]
        for tenant in READABLE_TENANTS
    }
    assert dict(rows) == expected


def test_cross_tenant_aggregates_match_postgres(engine, multi_tenant_server):
    avg_total, distinct_users = engine.cursor().execute(
        "SELECT avg(total), count(DISTINCT user_id) FROM public.orders"
    ).fetchone()

    totals: list[Decimal] = []
    user_ids: set[int] = set()
    for tenant in READABLE_TENANTS:
        for total, user_id in multi_tenant_server.query(tenant, "SELECT total, user_id FROM public.orders"):
            totals.append(total)
            user_ids.add(user_id)
    assert Decimal(str(avg_total)).quantize(Decimal("0.0001")) == (sum(totals) / len(totals)).quantize(
        Decimal("0.0001")
    )
    assert distinct_users == len(user_ids)


def test_missing_column_reads_null_for_old_tenant(engine):
    notes = engine.cursor().execute(
        "SELECT note FROM public.orders WHERE tenant_db = 'tenant_old'"
    ).fetchall()

    assert len(notes) == len(TENANT_ROWS["tenant_old"]["orders"])
    assert all(note is None for (note,) in notes)
    other = engine.cursor().execute(
        "SELECT count(note) FROM public.orders WHERE tenant_db = 'tenant_a'"
    ).fetchone()[0]
    assert other > 0


def test_database_named_like_schema_or_differing_by_case_is_queryable(engine):
    cur = engine.cursor()
    by_tenant = {
        tenant: sorted(cur.execute(
            "SELECT name FROM public.users WHERE tenant_db = ?", [tenant]
        ).fetchall())
        for tenant in ("public", "tenant_a", "Tenant_A")
    }

    for tenant, names in by_tenant.items():
        assert names == sorted((name,) for _, name in TENANT_ROWS[tenant]["users"])
    assert len({tuple(v) for v in by_tenant.values()}) == 3


def test_unreachable_tenant_is_left_out_and_reported(multi_tenant_server):
    built = _build(multi_tenant_server, tenants=READABLE_TENANTS + [LOCKED_TENANT])
    try:
        tenants = {t for (t,) in built.cursor().execute("SELECT DISTINCT tenant_db FROM public.orders").fetchall()}

        assert tenants == set(READABLE_TENANTS)
        assert [f["name"] for f in built.failed_tenants] == [LOCKED_TENANT]
        assert built.failed_tenants[0]["reason"]
    finally:
        built.close()


def test_failure_reasons_name_the_cause_without_server_or_login(multi_tenant_server):
    wrong = _reader(multi_tenant_server, password="not-the-password")
    locked = _build(multi_tenant_server, tenants=["tenant_a", LOCKED_TENANT])
    refused = _build(multi_tenant_server, conn=wrong, views={"public.users": ["tenant_a"]})
    try:
        reasons = {
            "locked": locked.failed_tenants[0]["reason"],
            "wrong password": refused.failed_tenants[0]["reason"],
        }
    finally:
        locked.close()
        refused.close()

    assert "permission denied" in reasons["locked"]
    assert "authentication" in reasons["wrong password"]
    for reason in reasons.values():
        assert multi_tenant_server.host not in reason
        assert str(multi_tenant_server.port) not in reason
        assert multi_tenant_server.reader_user not in reason


def test_tenant_missing_one_table_is_left_out_of_that_view_only(multi_tenant_server):
    views = {"public.orders": ["tenant_a", "public"], "public.refunds": ["tenant_a"]}
    built = _build(multi_tenant_server, views=views)
    try:
        tenants = {t for (t,) in built.cursor().execute("SELECT DISTINCT tenant_db FROM public.orders").fetchall()}

        assert tenants == {"tenant_a", "public"}
        assert built.failed_tenants == []
        assert built.missing_views == ["public.refunds"]
        assert [(e["view"], e["tenant"]) for e in built.left_out] == [("public.refunds", "tenant_a")]
        assert built.left_out[0]["reason"]
        with pytest.raises(duckdb.CatalogException):
            built.cursor().execute("SELECT * FROM public.refunds")
    finally:
        built.close()


def test_view_that_cannot_be_created_leaves_the_other_views_queryable(multi_tenant_server):
    clash = "case_clash_tenant"
    multi_tenant_server.create_database(clash)
    conn = multi_tenant_server.connect(clash)
    try:
        with conn.cursor() as cur:
            cur.execute('CREATE TABLE public.users (id int)')
            cur.execute('CREATE TABLE public."Users" (id int, name text)')
            cur.execute(f"GRANT USAGE ON SCHEMA public TO {multi_tenant_server.reader_user}")
            cur.execute(f"GRANT SELECT ON ALL TABLES IN SCHEMA public TO {multi_tenant_server.reader_user}")
        conn.commit()
    finally:
        conn.close()
    views = {"public.users": [clash], "public.Users": [clash], "public.orders": ["tenant_a"]}

    built = _build(multi_tenant_server, views=views)
    try:
        assert built.cursor().execute("SELECT count(*) FROM public.orders").fetchone()[0] == len(
            TENANT_ROWS["tenant_a"]["orders"]
        )
        assert {"public.users", "public.Users"} & set(built.missing_views)
    finally:
        built.close()


def test_database_name_with_quote_and_backslash_is_attached(multi_tenant_server):
    odd = "o'brien\\co"
    multi_tenant_server.create_database(odd)
    conn = multi_tenant_server.connect(odd)
    try:
        with conn.cursor() as cur:
            cur.execute("CREATE TABLE public.users (id int, name text)")
            cur.execute("INSERT INTO public.users VALUES (1, 'quoted')")
            cur.execute(f"GRANT SELECT ON public.users TO {multi_tenant_server.reader_user}")
        conn.commit()
    finally:
        conn.close()

    built = _build(multi_tenant_server, views={"public.users": [odd]})
    try:
        assert built.failed_tenants == []
        assert built.cursor().execute("SELECT tenant_db, name FROM public.users").fetchall() == [(odd, "quoted")]
    finally:
        built.close()


def test_engine_refuses_attach_and_file_reads_after_build(engine, multi_tenant_server):
    cur = engine.cursor()
    s = multi_tenant_server
    attach = (
        f"ATTACH 'dbname=tenant_a host={s.host} port={s.port} user={s.admin_user} password={s.admin_password}'"
        " AS sneaky (TYPE postgres, READ_ONLY)"
    )

    with pytest.raises(duckdb.PermissionException, match="disabled"):
        cur.execute(attach)
    with pytest.raises(duckdb.PermissionException, match="disabled"):
        cur.execute("SELECT * FROM read_csv('/etc/hosts')")
    with pytest.raises(duckdb.InvalidInputException, match="locked"):
        cur.execute("SET enable_external_access = true")


def test_connection_repr_hides_password(multi_tenant_server):
    conn = _reader(multi_tenant_server, password="Repr-Hidden-77")

    assert "Repr-Hidden-77" not in repr(conn)
    assert multi_tenant_server.reader_user in repr(conn)


def test_concurrent_queries_stay_within_connection_cap_per_tenant(multi_tenant_server):
    cap = 2
    slow_view = "slow_orders_cap"
    for tenant in READABLE_TENANTS:
        conn = multi_tenant_server.connect(tenant)
        try:
            with conn.cursor() as cur:
                cur.execute(f"CREATE VIEW public.{slow_view} AS SELECT o.* FROM public.orders o, pg_sleep(0.4)")
                cur.execute(f"GRANT SELECT ON public.{slow_view} TO {multi_tenant_server.reader_user}")
            conn.commit()
        finally:
            conn.close()

    built = build_engine(
        _reader(multi_tenant_server),
        {f"public.{slow_view}": READABLE_TENANTS},
        statement_timeout_ms=60_000,
        memory_limit="512MB",
        threads=4,
        pg_connection_limit=cap,
        pg_idle_timeout_ms=60_000,
    )
    settings = dict(built.cursor().execute(
        "SELECT name, value FROM duckdb_settings() WHERE name IN ('pg_pool_max_connections', 'pg_pool_acquire_mode')"
    ).fetchall())
    assert settings == {"pg_pool_max_connections": str(cap), "pg_pool_acquire_mode": "wait"}

    errors: list[BaseException] = []

    def run_query():
        try:
            built.cursor().execute(f"SELECT tenant_db, count(*) FROM public.{slow_view} GROUP BY 1").fetchall()
        except BaseException as exc:
            errors.append(exc)

    workers = [threading.Thread(target=run_query) for _ in range(3 * cap)]
    peak: dict[str, int] = {}
    monitor = multi_tenant_server.connect("postgres")
    monitor.autocommit = True
    try:
        for w in workers:
            w.start()
        with monitor.cursor() as cur:
            while any(w.is_alive() for w in workers):
                cur.execute(
                    "SELECT datname, count(*) FROM pg_stat_activity WHERE usename = %s GROUP BY 1",
                    [multi_tenant_server.reader_user],
                )
                for datname, count in cur.fetchall():
                    peak[datname] = max(peak.get(datname, 0), count)
        for w in workers:
            w.join()
    finally:
        monitor.close()
        built.close()

    assert errors == []
    assert set(peak) == set(READABLE_TENANTS)
    assert max(peak.values()) <= cap, peak


def _reader_connections(server, databases: list[str]) -> int:
    rows = server.query(
        "postgres",
        f"SELECT datname FROM pg_stat_activity WHERE usename = '{server.reader_user}'",
    )
    return sum(1 for (datname,) in rows if datname in databases)


def _wait_for(predicate, timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return False


def test_idle_tenant_connections_are_released_and_reopened_on_demand(multi_tenant_server):
    tenants = [f"idle_release_{n}" for n in range(3)]
    for tenant in tenants:
        multi_tenant_server.create_database(tenant)
        _seed_tenant(multi_tenant_server, tenant, TENANT_ROWS["tenant_old"])
    cap = 2
    built = build_engine(
        _reader(multi_tenant_server),
        {"public.orders": tenants},
        statement_timeout_ms=60_000,
        memory_limit="512MB",
        threads=2,
        pg_connection_limit=cap,
        pg_idle_timeout_ms=1_000,
    )
    try:
        built.cursor().execute("SELECT count(*) FROM public.orders").fetchall()
        assert 0 < _reader_connections(multi_tenant_server, tenants) <= cap * len(tenants)

        assert _wait_for(lambda: _reader_connections(multi_tenant_server, tenants) == 0)

        count = built.cursor().execute("SELECT count(*) FROM public.orders").fetchone()[0]
        assert count == len(tenants) * len(TENANT_ROWS["tenant_old"]["orders"])
    finally:
        built.close()


def test_cached_engine_releases_idle_connections_on_the_default_timeout(multi_tenant_server, monkeypatch):
    tenants = [f"idle_cached_{n}" for n in range(2)]
    for tenant in tenants:
        multi_tenant_server.create_database(tenant)
        _seed_tenant(multi_tenant_server, tenant, TENANT_ROWS["public"])
    monkeypatch.setattr(engine_module, "DEFAULT_PG_IDLE_TIMEOUT_MS", 1_000)

    cached = get_engine(_reader(multi_tenant_server), {"public.users": tenants}, connection_id="idle-cached")
    cached.cursor().execute("SELECT count(*) FROM public.users").fetchall()

    assert _reader_connections(multi_tenant_server, tenants) > 0
    assert _wait_for(lambda: _reader_connections(multi_tenant_server, tenants) == 0)


def test_password_not_in_error_text(multi_tenant_server):
    wrong = "Wr0ngPassw0rd-zz9"
    built = _build(multi_tenant_server, conn=_reader(multi_tenant_server, password=wrong))
    try:
        assert {f["name"] for f in built.failed_tenants} == set(READABLE_TENANTS)
        for failure in built.failed_tenants:
            assert wrong not in failure["reason"]
        with pytest.raises(duckdb.Error) as raised:
            built.cursor().execute("SELECT * FROM public.orders")
        assert wrong not in str(raised.value)
    finally:
        built.close()


def test_tenant_filter_does_not_scan_other_tenants(engine):
    plan = "\n".join(
        row[1] for row in engine.cursor().execute(
            "EXPLAIN SELECT * FROM public.orders WHERE tenant_db = 'tenant_a'"
        ).fetchall()
    )

    assert plan.count("POSTGRES_SCAN") == 1, plan


def test_statement_timeout_reaches_postgres(multi_tenant_server):
    slow = "slow_tenant"
    multi_tenant_server.create_database(slow)
    conn = multi_tenant_server.connect(slow)
    try:
        with conn.cursor() as cur:
            cur.execute("CREATE VIEW public.slow AS SELECT 1 AS id FROM pg_sleep(5)")
            cur.execute(f"GRANT SELECT ON public.slow TO {multi_tenant_server.reader_user}")
        conn.commit()
    finally:
        conn.close()

    built = _build(multi_tenant_server, views={"public.slow": [slow]}, statement_timeout_ms=300)
    try:
        assert built.failed_tenants == []
        with pytest.raises(duckdb.Error, match="(?i)statement timeout|cancel"):
            built.cursor().execute("SELECT * FROM public.slow").fetchall()
    finally:
        built.close()


def test_get_engine_shares_one_build_per_key(multi_tenant_server):
    conn = _reader(multi_tenant_server)
    views = {"public.users": ["tenant_a"]}
    results = []
    barrier = threading.Barrier(4)

    def worker():
        barrier.wait()
        results.append(get_engine(conn, views))

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len({id(e) for e in results}) == 1
    assert get_engine(conn, {"public.users": ["tenant_a"]}) is results[0]
    assert get_engine(conn, {"public.users": ["public"]}) is not results[0]


def test_get_engine_key_ignores_view_and_tenant_order(multi_tenant_server):
    conn = _reader(multi_tenant_server)

    first = get_engine(conn, {"public.users": ["tenant_a", "public"], "public.orders": ["public"]})
    second = get_engine(conn, {"public.orders": ["public"], "public.users": ["public", "tenant_a"]})

    assert second is first


def test_get_engine_rebuilds_after_invalidate_and_ttl(multi_tenant_server, monkeypatch):
    conn = _reader(multi_tenant_server)
    views = {"public.users": ["tenant_a"]}
    clock = [1000.0]
    monkeypatch.setattr(engine_module, "monotonic", lambda: clock[0])

    first = get_engine(conn, views, ttl_seconds=60)
    held = first.cursor()
    invalidate_engines()
    assert held.execute("SELECT count(*) FROM public.users").fetchone()[0] == len(TENANT_ROWS["tenant_a"]["users"])
    assert first.cursor().execute("SELECT count(*) FROM public.users").fetchone()[0] > 0
    second = get_engine(conn, views, ttl_seconds=60)
    assert second is not first

    clock[0] += 59
    assert get_engine(conn, views, ttl_seconds=60) is second
    clock[0] += 2
    third = get_engine(conn, views, ttl_seconds=60)
    assert third is not second
    assert third.cursor().execute("SELECT count(*) FROM public.users").fetchone()[0] == len(
        TENANT_ROWS["tenant_a"]["users"]
    )


def test_build_interrupted_by_invalidate_is_not_cached(multi_tenant_server, monkeypatch):
    conn = _reader(multi_tenant_server)
    views = {"public.users": ["tenant_a"]}
    real_build = engine_module.build_engine

    def build_then_reindex(*args, **kwargs):
        built = real_build(*args, **kwargs)
        invalidate_engines()
        return built

    monkeypatch.setattr(engine_module, "build_engine", build_then_reindex)
    stale = get_engine(conn, views)
    monkeypatch.setattr(engine_module, "build_engine", real_build)

    fresh = get_engine(conn, views)
    assert fresh is not stale
    assert get_engine(conn, views) is fresh


def test_extension_directory_without_postgres_extension_fails_offline(multi_tenant_server, monkeypatch, tmp_path):
    monkeypatch.setenv("BOW_DUCKDB_EXTENSION_DIR", str(tmp_path))

    with pytest.raises(duckdb.Error):
        _build(multi_tenant_server, views={"public.users": ["tenant_a"]})
    assert not any(tmp_path.rglob("*.duckdb_extension"))


def _count_builds(monkeypatch) -> list:
    built: list = []
    real_build = engine_module.build_engine

    def counting(*args, **kwargs):
        engine = real_build(*args, **kwargs)
        built.append(engine)
        return engine

    monkeypatch.setattr(engine_module, "build_engine", counting)
    return built


def test_selections_on_one_connection_share_one_engine(multi_tenant_server, monkeypatch):
    builds = _count_builds(monkeypatch)
    conn = _reader(multi_tenant_server)

    orders_only = get_engine(conn, {"public.orders": ["tenant_a"]}, tenants=READABLE_TENANTS, connection_id="c1")
    users_only = get_engine(conn, {"public.users": ["public"]}, tenants=READABLE_TENANTS, connection_id="c1")

    assert users_only is orders_only
    assert len(builds) == 1
    cur = users_only.cursor()
    assert cur.execute("SELECT count(*) FROM public.orders").fetchone()[0] == len(TENANT_ROWS["tenant_a"]["orders"])
    assert cur.execute("SELECT count(*) FROM public.users").fetchone()[0] == len(TENANT_ROWS["public"]["users"])


def test_identical_logins_on_two_connections_never_share_an_engine(multi_tenant_server):
    conn = _reader(multi_tenant_server)
    views = {"public.users": ["tenant_a"]}

    first = get_engine(conn, views, connection_id="org-1-conn")
    second = get_engine(conn, views, connection_id="org-2-conn")

    assert first is not second


def test_peek_returns_the_cached_engine_without_building(multi_tenant_server, monkeypatch):
    builds = _count_builds(monkeypatch)
    conn = _reader(multi_tenant_server)
    tenants = ["tenant_a", "public"]

    assert peek_engine(conn, tenants=tenants, connection_id="c1") is None
    assert builds == []
    engine = get_engine(conn, {"public.users": tenants}, tenants=tenants, connection_id="c1")

    assert peek_engine(conn, tenants=tenants, connection_id="c1") is engine
    assert peek_engine(conn, tenants=tenants, connection_id="other") is None
    assert len(builds) == 1


def test_engine_with_failed_tenant_expires_sooner(multi_tenant_server, monkeypatch):
    conn = _reader(multi_tenant_server)
    clock = [5000.0]
    monkeypatch.setattr(engine_module, "monotonic", lambda: clock[0])
    healthy_views = {"public.users": ["tenant_a"]}
    failing_views = {"public.users": ["tenant_a", LOCKED_TENANT]}

    healthy = get_engine(conn, healthy_views, ttl_seconds=600)
    failing = get_engine(conn, failing_views, ttl_seconds=600)
    assert [f["name"] for f in failing.failed_tenants] == [LOCKED_TENANT]

    clock[0] += 120
    assert get_engine(conn, healthy_views, ttl_seconds=600) is healthy
    assert get_engine(conn, failing_views, ttl_seconds=600) is not failing


def test_failed_tenant_is_logged_without_the_password(multi_tenant_server, caplog):
    secret = "Logged-Never-42"
    with caplog.at_level(logging.WARNING, logger=engine_module.__name__):
        built = _build(
            multi_tenant_server,
            conn=_reader(multi_tenant_server, password=secret),
            views={"public.users": ["tenant_a"]},
        )
    built.close()

    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("tenant_a" in w for w in warnings)
    assert not any(secret in w for w in warnings)
