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
    "SELECT pg_get_viewdef('__t0001.public.orders')",
    "SELECT pg_get_constraintdef(1)",
    "SELECT has_table_privilege('__t0001.public.orders', 'select')",
    "SELECT has_schema_privilege('__t0001.public', 'usage')",
    "SELECT tenant_db FROM public.orders WHERE has_database_privilege('__t0001', 'connect')",
    "SELECT pg_table_is_visible(1)",
    "SELECT pg_type_is_visible(1)",
    "SELECT current_setting('pg_pool_max_connections')",
    "SELECT getvariable('anything')",
    "SELECT format_type(16, NULL)",
    "SELECT enum_range(NULL::__t0004.public.secret_plan)",
    "SELECT CAST(1 AS __t0001.public.secret_plan)",
    "SELECT TRY_CAST(note AS __t0002.public.mood) FROM public.orders",
    "SELECT NULL::__t0001.public.secret_plan[]",
    "SELECT NULL::STRUCT(a __t0001.public.secret_plan)",
    "SELECT NULL::public.secret_plan",
    "SELECT enum_range(json_transform('null', '\"__t0004.public.secret_plan\"'))",
    "SELECT json_transform_strict('null', '\"__t0004.public.secret_plan\"')",
    "SELECT from_json('null', '\"__t0004.public.secret_plan\"')",
    "SELECT FROM_JSON_STRICT('null', '\"__t0004.public.secret_plan\"')",
    "SELECT json_transform('{}', '{\"a\": \"__t0004.public.x\"}')",
    "SELECT json_transform('{}', '{\"a\": [{\"b\": \"__t0001.public.x\"}]}')",
    "SELECT json_transform(note, note) FROM public.orders",
    "SELECT json_transform('{}', '{not json')",
    "SELECT json_transform('{}', '\"INTEGER) FROM __t0001.public.orders --\"')",
    "SELECT ('null').json_transform('\"__t0001.public.secret_plan\"')",
    "SELECT 'null'.json_transform('\"__t0001.public.secret_plan\"')",
    "SELECT ('null'::VARCHAR).from_json('\"__t0001.public.secret_plan\"')",
    "SELECT note.from_json('\"__t0001.public.secret_plan\"') FROM public.orders",
    "SELECT o.note.json_transform_strict('{\"a\": \"__t0002.public.x\"}') FROM public.orders o",
    "SELECT note.from_json_strict(note) FROM public.orders",
    "SELECT json_transform('null', '{\"a\": \"__t0004.public.x\", \"a\": \"INTEGER\"}')",
    "SELECT main.json_transform('null', '\"__t0001.public.secret_plan\"')",
    "SELECT system.main.from_json('null', '\"__t0001.public.secret_plan\"')",
    "SELECT main.json_transform_strict('null', '{\"a\": \"__t0004.public.x\", \"a\": \"INTEGER\"}')",
    "SET search_path = '__t0004.public'",
    "SET search_path = '__t0004.public'; SELECT * FROM public.orders",
    "SELECT * FROM public.orders; SELECT * FROM public.users",
    "DESCRIBE public.orders",
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
    "SELECT CAST(total AS DECIMAL(10, 2)) FROM public.orders",
    "SELECT 1::INTEGER[]",
    "SELECT NULL::memory.main.mood",
    "SELECT json_transform(note, '{\"a\": \"INTEGER\", \"b\": [\"VARCHAR\"]}') FROM public.orders",
    "SELECT from_json(note, '\"memory.main.mood\"') FROM public.orders",
    "SELECT * FROM public.orders UNION ALL SELECT * FROM public.orders",
    "(SELECT 1)",
    "SELECT ('null').json_transform('\"INTEGER\"')",
    "SELECT note.from_json('{\"a\": \"VARCHAR\"}') FROM public.orders",
]


def _client() -> PostgresqlMultiClient:
    client = PostgresqlMultiClient(
        host="unreachable.invalid",
        port=5432,
        databases="tenant_*",
        user="reader",
        password="pw",
    )
    client._bow_connection_id = "conn-guards"
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
    attached_tenants = frozenset({"tenant_a", "tenant_b"})

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
    assert "authentication failed" in text
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


_PG_HBA_TEXT = (
    'IO Error: Unable to connect to Postgres at "dbname=tenant_a sslmode=prefer": connection to server at '
    '"unreachable.invalid" (10.9.8.7), port 6543 failed: FATAL:  no pg_hba.conf entry for host "10.1.1.1", '
    'user "reader", database "tenant_a", no encryption'
)
_TLS_TEXT = (
    'IO Error: Unable to connect to Postgres at "dbname=tenant_a sslmode=verify-full": '
    'server certificate for "db-proxy.internal" (and 2 other names) does not match host name "unreachable.invalid"'
)


@pytest.mark.parametrize("text", [_PG_HBA_TEXT, _TLS_TEXT])
def test_rejected_connection_reaches_the_agent_without_server_or_login(monkeypatch, text):
    import app.data_sources.clients.postgresql_multi_client as module

    monkeypatch.setattr(module, "get_engine", lambda *a, **k: _FailingEngine(duckdb.IOException(text)))

    with pytest.raises(duckdb.Error) as raised:
        _client().execute_query("SELECT * FROM public.orders")

    message = str(raised.value)
    for detail in ("unreachable.invalid", "10.1.1.1", "db-proxy.internal", '"reader"', "6543"):
        assert detail not in message


@pytest.mark.parametrize("error", [
    duckdb.InvalidInputException("Invalid Input Error: unreachable.invalid reader pw"),
    duckdb.BinderException('Binder Error: Referenced column "reader" not found in FROM clause!'),
    duckdb.ConversionException("Conversion Error: Could not convert string 'pw' to INT32"),
])
def test_error_from_agent_sql_is_never_rewritten_with_connection_secrets(monkeypatch, error):
    import app.data_sources.clients.postgresql_multi_client as module

    monkeypatch.setattr(module, "get_engine", lambda *a, **k: _FailingEngine(error))

    with pytest.raises(type(error)) as raised:
        _client().execute_query("SELECT * FROM public.orders")

    assert str(raised.value) == str(error)


class _TwoArgumentIOError(duckdb.IOException):
    def __init__(self, message, code):
        super().__init__(message)
        self.code = code


def test_connection_error_type_that_cannot_be_rebuilt_is_still_redacted(monkeypatch):
    import app.data_sources.clients.postgresql_multi_client as module

    error = _TwoArgumentIOError(_SERVER_TEXT, 7)
    monkeypatch.setattr(module, "get_engine", lambda *a, **k: _FailingEngine(error))

    with pytest.raises(duckdb.Error) as raised:
        _client().execute_query("SELECT * FROM public.orders")

    assert "unreachable.invalid" not in str(raised.value)


def test_unknown_type_error_names_no_tenant_type(monkeypatch):
    import app.data_sources.clients.postgresql_multi_client as module

    error = duckdb.CatalogException(
        'Catalog Error: Type with name secret_pla does not exist!\nDid you mean "__t0004.public.secret_plan"?'
    )
    monkeypatch.setattr(module, "get_engine", lambda *a, **k: _FailingEngine(error))

    with pytest.raises(duckdb.CatalogException) as raised:
        _client().execute_query("SELECT NULL::secret_pla")

    assert "__t0004" not in str(raised.value)
    assert "secret_plan" not in str(raised.value)


def test_unsaved_client_retry_onto_a_newer_engine_keeps_its_rebuild_budget():
    client = _client()
    client._bow_connection_id = ""
    failed, current, rebuilt = object(), object(), object()
    client._own_engine = current

    retried = [client._discard(failed), client._discard(failed)]

    assert retried == [True, True]
    assert client._own_engine is current
    assert client._discard(current) is True
    client._own_engine = rebuilt
    assert client._discard(rebuilt) is False


def test_test_connection_probes_tenants_in_parallel_within_a_bound(monkeypatch):
    import threading
    import time as _time

    from app.data_sources.clients import postgresql_multi_client as client_module

    tenants = [f"tenant_{i:02d}" for i in range(24)]
    client = _client()
    monkeypatch.setattr(client, "_discover", lambda: tenants)
    lock, running, peak = threading.Lock(), [0], [0]

    def probe(database):
        with lock:
            running[0] += 1
            peak[0] = max(peak[0], running[0])
        _time.sleep(0.1)
        with lock:
            running[0] -= 1
        return "permission denied" if database == "tenant_05" else None

    monkeypatch.setattr(client, "_probe_tenant", probe)
    started = _time.monotonic()
    result = client.test_connection()
    elapsed = _time.monotonic() - started

    assert result["success"], result
    assert "tenant_05" in result["message"]
    assert peak[0] == client_module.TENANT_WORKERS  # parallel, never more than the bound
    assert elapsed < 24 * 0.1 / 2  # 2.4 s serially


def test_engine_build_failure_reaches_the_agent_as_a_duckdb_error(monkeypatch):
    import duckdb

    client = _client()
    monkeypatch.setattr(client, "_check_table_refs", lambda sql: ["public.orders"])

    from app.data_sources.clients.postgresql_multi_engine import ExtensionUnavailable

    def no_extension(used):
        raise ExtensionUnavailable("The DuckDB postgres extension is not available on this server")

    monkeypatch.setattr(client, "_engine", no_extension)
    with pytest.raises(duckdb.IOException, match="extension is not available"):
        client.execute_query("SELECT * FROM public.orders")


def test_postgres_extension_load_failure_names_the_fix(monkeypatch):
    import duckdb

    from app.data_sources.clients import postgresql_multi_engine as engine_module

    class _Offline:
        def execute(self, sql):
            if sql.startswith(("LOAD", "INSTALL")):
                raise duckdb.IOException("Failed to download extension from extensions.duckdb.org")

    monkeypatch.delenv(engine_module.EXTENSION_DIR_ENV, raising=False)
    with pytest.raises(duckdb.IOException, match=engine_module.EXTENSION_DIR_ENV):
        engine_module._load_postgres_extension(_Offline())


@pytest.mark.parametrize(("postgres", "duckdb_type"), [
    ("integer", "BIGINT"), ("numeric(12,2)", "DECIMAL(12,2)"), ("character varying(50)", "VARCHAR"),
    ("timestamp without time zone", "TIMESTAMP"), ("timestamp with time zone", "TIMESTAMP WITH TIME ZONE"),
    ("boolean", "BOOLEAN"), ("date", "DATE"), ("json", "VARCHAR"), ("jsonb", "VARCHAR"),
])
def test_coarse_type_matches_postgres_and_duckdb_names(postgres, duckdb_type):
    from app.data_sources.clients.postgresql_multi_engine import coarse_type

    assert coarse_type(postgres) == coarse_type(duckdb_type) is not None


def test_coarse_type_tells_numbers_from_text_and_skips_arrays():
    from app.data_sources.clients.postgresql_multi_engine import coarse_type

    assert coarse_type("numeric") != coarse_type("VARCHAR")
    assert coarse_type("integer[]") is None and coarse_type("my_enum") is None


@pytest.mark.parametrize(("expected", "served", "fits"), [
    ("bigint", "DOUBLE", False),            # rounds other tenants' integers past 2^53
    ("numeric(12,2)", "DOUBLE", False),
    ("integer", "BIGINT", True),
    ("bigint", "DECIMAL(20,2)", True),      # exact numbers merge without loss
    ("numeric", "DOUBLE", True),            # bare numeric is served as DOUBLE anyway
    ("numeric(50,10)", "DOUBLE", True),     # wider than DuckDB's DECIMAL: DOUBLE anyway
    ("numeric", "DECIMAL(12,2)", True),     # an approximate column stays approximate
    ("numeric(12,2)", "DECIMAL(14,4)", True),
    ("double precision", "FLOAT", True),
    ("double precision", "BIGINT", True),
    ("numeric", "VARCHAR", False),
])
def test_runtime_type_check_keeps_exact_and_approximate_numbers_apart(expected, served, fits):
    from app.data_sources.clients.postgresql_multi_engine import coarse_type, type_still_fits

    assert type_still_fits(coarse_type(expected), coarse_type(served)) is fits


def test_python_frame_keeps_nullable_integers_exact():
    import datetime
    from decimal import Decimal

    from app.data_sources.clients.postgresql_multi_client import _python_frame

    description = [("n", "BIGINT"), ("t", "TIME WITH TIME ZONE"), ("d", "DECIMAL(5,2)")]
    tz = datetime.timezone(datetime.timedelta(hours=2))
    frame = _python_frame(description, [
        (9007199254740993, datetime.time(11, tzinfo=tz), Decimal("1.25")),
        (None, None, None),
    ])

    assert str(frame["n"].dtype) == "Int64"
    assert frame["n"].iloc[0] == 9007199254740993
    assert frame["n"].isna().iloc[1]
    assert frame["t"].iloc[0].utcoffset() == datetime.timedelta(hours=2)
    assert frame["d"].iloc[0] == Decimal("1.25")
