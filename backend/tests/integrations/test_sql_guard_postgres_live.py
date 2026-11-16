"""The Postgres server is the floor under the read-only guard, and reads
literals the way the guard did.

Contract, on a real Postgres 15:

* every agent query and every custom-table (FAST) statement on a Postgres
  connection runs in a read-only transaction that ends in a rollback, so a
  write the guard lets through (a user function, a locking read) or a call
  that never met the guard fails on the server or leaves nothing behind;
* the pooled session keeps no read-only default: after a read it goes back
  to the server's default, so discovery on the same backend still works, a
  read replica keeps serving plain connections, and a database whose
  default is read-only stays read-only for them;
* on a server with `standard_conforming_strings=off` the client switches it
  on for the query (also behind PgBouncer), so a backslash literal holds the
  value the guard read;
* a query that leaves the setting off does not hand its connection back;
* a read on a reused pooled connection costs three statements (four with
  pinned schemas): no health-check ping unless the connection looks dead or
  sat idle past the threshold (`BOW_PG_PING_LISTENER=off` gives back the
  pre-ping's `SELECT 1`); a backend the server ended is replaced without
  failing the next read.

Needs Docker. CI does not collect tests/integrations; these run on the test
harness. Without a Docker daemon they skip, or fail when CI is set.
"""

from __future__ import annotations

import logging
import os
import sys
import time
import uuid
from contextlib import contextmanager
from unittest.mock import patch

import pytest

from app.ai.code_execution.code_execution import StreamingCodeExecutor, UnsafeSQLError
from app.ai.code_execution.sql_guard import ensure_read_only
from app.data_sources import engine_pool
from app.data_sources.clients.postgresql_client import PostgresqlClient
from app.data_sources.fast import artifacts, extractor
from app.data_sources.fast.sources import source_for

pytestmark = pytest.mark.integration

BS = chr(92)
# A literal holding one backslash, and the value it holds.
BACKSLASH_SQL = "SELECT 'a" + BS + "b' AS v, current_setting('standard_conforming_strings') AS scs"
BACKSLASH_VALUE = "a" + BS + "b"
PGBOUNCER_IMAGE = "edoburu/pgbouncer:v1.25.1-p0"
READ_ONLY_ERROR = "read-only transaction"

SEED = [
    "CREATE TABLE orders (id int)",
    "INSERT INTO orders VALUES (1), (2)",
    "CREATE SEQUENCE seq",
    "CREATE FUNCTION add_order() RETURNS int LANGUAGE sql AS 'INSERT INTO orders VALUES (99) RETURNING id'",
]


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
            pytest.fail("Docker daemon not reachable in CI: the live Postgres tests must run")
        pytest.skip("Docker daemon not reachable")


@contextmanager
def _postgres(*options: str):
    """A seeded Postgres 15 started with `options`; yields its settings."""
    _require_docker()
    from testcontainers.postgres import PostgresContainer

    command = " ".join(["postgres", *options])
    with PostgresContainer("postgres:15").with_command(command) as container:
        settings = {"host": container.get_container_host_ip(), "port": int(container.get_exposed_port(5432)),
                    "user": container.username, "password": container.password, "dbname": container.dbname,
                    "container": container}
        for sql in SEED:
            _admin(settings, sql)
        yield settings


def _admin(settings: dict, sql: str):
    """Run `sql` as the server's owner on its own connection; its rows."""
    import psycopg2

    con = psycopg2.connect(host=settings["host"], port=settings["port"], user=settings["user"],
                           password=settings["password"], dbname=settings["dbname"])
    con.autocommit = True
    try:
        with con.cursor() as cur:
            cur.execute(sql)
            return cur.fetchall() if cur.description else None
    finally:
        con.close()


@pytest.fixture(scope="module")
def server():
    with _postgres() as settings:
        yield settings


@pytest.fixture(scope="module")
def scs_off_server():
    """Reads backslashes in plain literals as escapes; logs every statement
    it receives."""
    with _postgres("-c standard_conforming_strings=off", "-c log_statement=all") as settings:
        yield settings


def _address(container) -> str:
    """The container's address on the Docker network."""
    inner = container.get_wrapped_container()
    inner.reload()
    networks = inner.attrs["NetworkSettings"]["Networks"]
    return next(n["IPAddress"] for n in networks.values() if n.get("IPAddress"))


def _wait_for(settings: dict, sql: str, expected) -> None:
    deadline = time.monotonic() + 60
    while True:
        try:
            if _admin(settings, sql) == expected:
                return
        except Exception:
            if time.monotonic() > deadline:
                raise
        if time.monotonic() > deadline:
            raise AssertionError(f"{sql} never returned {expected}")
        time.sleep(0.5)


@pytest.fixture(scope="module")
def logged_server():
    """Logs every statement it receives."""
    with _postgres("-c log_statement=all") as settings:
        _admin(settings, "CREATE TABLE scratch (v text)")
        yield settings


@pytest.fixture(scope="module")
def read_only_database(server):
    """A database on `server` whose sessions default to read-only."""
    _admin(server, "CREATE DATABASE read_only_default")
    _admin(server, "ALTER DATABASE read_only_default SET default_transaction_read_only = on")
    yield {**server, "dbname": "read_only_default"}


@pytest.fixture(scope="module")
def standby():
    """A hot standby streaming from its own seeded primary."""
    from testcontainers.core.container import DockerContainer

    with _postgres() as primary:
        primary["container"].exec(
            ["bash", "-c", "echo 'host replication all all scram-sha-256' >> \"$PGDATA/pg_hba.conf\""])
        _admin(primary, "SELECT pg_reload_conf()")
        script = (
            'mkdir -p "$PGDATA" && chown postgres "$PGDATA" && chmod 700 "$PGDATA" && '
            f'gosu postgres pg_basebackup -h {_address(primary["container"])} -p 5432 -U {primary["user"]} '
            '-D "$PGDATA" -R -X stream && exec docker-entrypoint.sh postgres'
        )
        container = (
            DockerContainer("postgres:15")
            .with_env("PGPASSWORD", primary["password"])
            .with_exposed_ports(5432)
            .with_command(["bash", "-c", script])
        )
        with container:
            settings = {**primary, "host": container.get_container_host_ip(),
                        "port": int(container.get_exposed_port(5432)), "container": container}
            _wait_for(settings, "SELECT pg_is_in_recovery(), (SELECT count(*) FROM orders)", [(True, 2)])
            yield settings


@pytest.fixture(scope="module")
def bouncer(scs_off_server):
    """PgBouncer in transaction mode in front of the scs=off server."""
    from testcontainers.core.container import DockerContainer

    address = _address(scs_off_server["container"])
    container = (
        DockerContainer(PGBOUNCER_IMAGE)
        .with_env("DB_HOST", address).with_env("DB_PORT", "5432")
        .with_env("DB_USER", scs_off_server["user"]).with_env("DB_PASSWORD", scs_off_server["password"])
        .with_env("DB_NAME", scs_off_server["dbname"])
        .with_env("AUTH_TYPE", "plain").with_env("POOL_MODE", "transaction")
        .with_exposed_ports(5432)
    )
    with container:
        settings = {**scs_off_server, "host": container.get_container_host_ip(),
                    "port": int(container.get_exposed_port(5432))}
        deadline = time.monotonic() + 30
        while True:
            try:
                _admin(settings, "SELECT 1")
                break
            except Exception:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.5)
        yield settings


class LivePostgresqlClient(PostgresqlClient):
    """The real Postgres client, recording what reaches `execute_query`."""

    def execute_query(self, sql):
        self.sent.append(sql)
        return super().execute_query(sql)


def _client(settings: dict, **options) -> LivePostgresqlClient:
    client = LivePostgresqlClient(host=settings["host"], port=settings["port"], database=settings["dbname"],
                                  user=settings["user"], password=settings["password"], **options)
    client.sent = []
    return client


def _run(client, sql: str):
    code = (
        "def generate_df(ds_clients, excel_files):\n"
        f"    return ds_clients['pg'].execute_query({sql!r})\n"
    )
    df, _log, _queries = StreamingCodeExecutor().execute_code(code=code, ds_clients={"pg": client}, excel_files=[])
    return df


@pytest.fixture(params=["inprocess", "subprocess"])
def mode(request, monkeypatch):
    if request.param == "subprocess" and not sys.platform.startswith("linux"):
        pytest.skip("sandbox child needs Linux")
    monkeypatch.setenv("BOW_CODE_SANDBOX", request.param)
    if request.param == "inprocess":
        # In-process mode is for local development only; CI runs with
        # ENVIRONMENT=production, so the test names the environment itself.
        monkeypatch.setenv("ENVIRONMENT", "development")
    return request.param


def _discovery_backend(client) -> tuple:
    """(backend pid, default_transaction_read_only, transaction_read_only, 1)
    on a discovery-path connection."""
    with client.connect() as conn:
        return tuple(conn.exec_driver_sql(
            "SELECT pg_backend_pid(), current_setting('default_transaction_read_only'), "
            "current_setting('transaction_read_only'), 1"
        ).one())


# --------------------------------------------------------------- agent queries

@pytest.mark.parametrize("sql", ["SELECT add_order() AS v", "SELECT id FROM orders FOR SHARE"])
def test_a_write_the_guard_lets_through_is_refused_by_the_server(mode, server, sql):
    client = _client(server)
    assert ensure_read_only(client, sql) == sql
    with pytest.raises(Exception, match=READ_ONLY_ERROR):
        _run(client, sql)
    assert client.sent == [sql]
    assert _admin(server, "SELECT count(*) FROM orders") == [(2,)]


def test_a_call_that_never_met_the_guard_cannot_move_a_sequence(server):
    client = _client(server)
    with pytest.raises(Exception, match=READ_ONLY_ERROR):
        client.execute_query("SELECT nextval('seq') AS v")
    assert _admin(server, "SELECT last_value, is_called FROM seq") == [(1, False)]


def test_a_read_runs_read_only_and_its_pooled_session_keeps_no_read_only_default(server):
    client = _client(server)
    df = client.execute_query("SELECT current_setting('transaction_read_only') AS ro, pg_backend_pid() AS pid")
    assert df["ro"].tolist() == ["on"]
    pid = int(df["pid"].iloc[0])
    assert _discovery_backend(client) == (pid, "off", "off", 1)


def test_after_a_read_a_database_whose_default_is_read_only_stays_read_only(read_only_database):
    client = _client(read_only_database)
    pid = int(client.execute_query("SELECT pg_backend_pid() AS pid")["pid"].iloc[0])
    assert _discovery_backend(client) == (pid, "on", "on", 1)


def test_a_read_replica_keeps_serving_plain_connections_after_a_read(standby):
    client = _client(standby)
    assert client.execute_query("SELECT count(*) AS n FROM orders")["n"].tolist() == [2]
    status = client.test_connection()
    assert status["success"] is True, status["message"]
    with client.connect() as conn:
        assert conn.exec_driver_sql("SELECT pg_is_in_recovery()").scalar() is True


def test_a_large_object_made_during_a_read_is_gone_after_it(server):
    """A large object can be made inside a read-only transaction; only the
    rollback that ends every read undoes it."""
    client = _client(server)
    agent_oid = int(client.execute_query("SELECT lo_from_bytea(0, 'x') AS oid")["oid"].iloc[0])
    # A limit above the row count, so the read ends normally rather than
    # being cancelled.
    _cols, rows = source_for(client).preview("SELECT lo_from_bytea(0, 'y') AS oid", 5)
    fast_oid = int(rows[0][0])
    assert _admin(server, f"SELECT count(*) FROM pg_largeobject_metadata WHERE oid IN ({agent_oid}, {fast_oid})") \
        == [(0,)]


def test_a_nul_byte_is_refused_before_anything_is_sent(mode, server):
    client = _client(server)
    with pytest.raises(UnsafeSQLError):
        _run(client, "SELECT 'a" + chr(0) + "b' AS v")
    assert client.sent == []


def test_a_read_that_leaves_standard_strings_off_does_not_return_its_connection(server):
    client = _client(server)
    df = client.execute_query(
        "SELECT set_config('standard_conforming_strings', 'off', false) AS v, pg_backend_pid() AS pid")
    pid = int(df["pid"].iloc[0])
    assert _discovery_backend(client)[0] != pid


CLIENT_LOGGER = "app.data_sources.clients.postgresql_client"


def _client_warnings(caplog) -> list:
    return [r for r in caplog.records if r.name == CLIENT_LOGGER and r.levelno == logging.WARNING]


@pytest.mark.parametrize("read_only", [True, False])
def test_a_schema_setting_the_server_cannot_apply_fails_with_an_error_naming_it(server, caplog, read_only):
    # alembic's logging config disables loggers that already exist.
    logging.getLogger(CLIENT_LOGGER).disabled = False
    caplog.set_level(logging.WARNING, logger=CLIENT_LOGGER)
    schema = "needs-quoting"
    client = PostgresqlClient(host=server["host"], port=server["port"], database=server["dbname"],
                              user=server["user"], password=server["password"], schema=schema)
    with pytest.raises(RuntimeError, match="schema setting") as error:
        with client.connect(read_only=read_only) as conn:
            conn.exec_driver_sql("SELECT 1").scalar()
    assert "current transaction is aborted" not in str(error.value)
    warnings = _client_warnings(caplog)
    assert warnings, "the failure is logged"
    assert all(schema not in r.getMessage() for r in warnings), "the log holds no schema value"


def test_a_schema_setting_failure_whose_rollback_fails_too_still_names_the_setting(server, caplog):
    import sqlalchemy

    logging.getLogger(CLIENT_LOGGER).disabled = False
    caplog.set_level(logging.WARNING, logger=CLIENT_LOGGER)
    schema = "needs-quoting"
    client = PostgresqlClient(host=server["host"], port=server["port"], database=server["dbname"],
                              user=server["user"], password=server["password"], schema=schema)

    def rollback_fails(self):
        raise ConnectionError("server closed the connection unexpectedly")

    with patch.object(sqlalchemy.engine.Connection, "rollback", rollback_fails), \
            pytest.raises(RuntimeError, match="schema setting"), client.connect(read_only=True) as conn:
        conn.exec_driver_sql("SELECT 1").scalar()
    warnings = _client_warnings(caplog)
    assert any("rollback" in r.getMessage() for r in warnings), "the failed rollback is logged"
    assert all(schema not in r.getMessage() for r in warnings), "the log holds no schema value"


def test_a_read_whose_rollback_fails_is_logged_and_its_session_is_not_reused(server, caplog):
    logging.getLogger(CLIENT_LOGGER).disabled = False
    caplog.set_level(logging.WARNING, logger=CLIENT_LOGGER)
    client = _client(server)
    with client.connect(read_only=True) as conn:
        pid = int(conn.exec_driver_sql("SELECT pg_backend_pid()").scalar())
        _admin(server, f"SELECT pg_terminate_backend({pid})")
        deadline = time.monotonic() + 10
        while _admin(server, f"SELECT count(*) FROM pg_stat_activity WHERE pid = {pid}") != [(0,)]:
            assert time.monotonic() < deadline, "the backend did not end"
            time.sleep(0.1)
    warnings = _client_warnings(caplog)
    assert warnings and all(r.exc_info for r in warnings)
    assert _discovery_backend(client)[0] != pid


# --------------------------------------------------------------- standard_conforming_strings=off

def test_a_backslash_literal_holds_its_value_on_a_server_with_standard_strings_off(mode, scs_off_server):
    client = _client(scs_off_server)
    df = _run(client, BACKSLASH_SQL)
    assert df.to_dict("records") == [{"v": BACKSLASH_VALUE, "scs": "on"}]
    # Switched for the query only: the session still has the server's setting.
    with client.connect() as conn:
        assert conn.exec_driver_sql("SHOW standard_conforming_strings").scalar() == "off"


def test_a_backslash_literal_holds_its_value_behind_pgbouncer(bouncer):
    client = _client(bouncer)
    df = client.execute_query(BACKSLASH_SQL)
    assert df.to_dict("records") == [{"v": BACKSLASH_VALUE, "scs": "on"}]


# --------------------------------------------------------------- custom tables (FAST)

def _statements(settings: dict) -> list[tuple[str, str]]:
    """(backend pid, statement) for every statement the server logged."""
    logs = settings["container"].get_wrapped_container().logs().decode()
    out = []
    for line in logs.splitlines():
        if "statement: " not in line or "[" not in line:
            continue
        pid = line.split("[", 1)[1].split("]", 1)[0]
        out.append((pid, line.split("statement: ", 1)[1]))
    return out


def _backend_log(settings: dict, pid: int, first: str, last: str) -> list[str]:
    """What backend `pid` logged from the statement holding `first` to the
    ROLLBACK after the one holding `last`."""
    deadline = time.monotonic() + 10
    while True:
        mine = [s for p, s in _statements(settings) if p == str(pid)]
        try:
            start = next(i for i, s in enumerate(mine) if first in s)
            end = next(i for i, s in enumerate(mine) if i > start and last in s)
            stop = next(i for i, s in enumerate(mine) if i > end and s == "ROLLBACK")
            return mine[start:stop + 1]
        except StopIteration:
            if time.monotonic() > deadline:
                raise AssertionError(f"backend {pid} logged {mine[-12:]}")
            time.sleep(0.2)


def test_a_plain_connection_after_a_read_starts_a_plain_transaction(logged_server):
    client = _client(logged_server)
    read = "SELECT pg_backend_pid() AS pid, 'plain-1' AS m"
    pid = int(client.execute_query(read)["pid"].iloc[0])
    plain = "SELECT pg_backend_pid(), 'plain-2'"
    with client.connect() as conn:
        assert conn.exec_driver_sql(plain).scalar() == pid
    assert _backend_log(logged_server, pid, "plain-1", "plain-2") == [
        read, "ROLLBACK", "BEGIN", plain, "ROLLBACK"]


def test_fast_preview_and_estimate_hold_a_backslash_literal_to_its_value(scs_off_server):
    client = _client(scs_off_server)
    _cols, rows = extractor.preview(client, BACKSLASH_SQL)
    assert rows == [[BACKSLASH_VALUE, "on"]]
    marker = "SELECT 'est" + BS + "x' AS v"
    assert extractor.estimate(client, marker).supported is True
    statements = _statements(scs_off_server)
    at = max(i for i, (_pid, s) in enumerate(statements) if marker in s)
    pid = statements[at][0]
    same_backend = [s for p, s in statements[:at + 1] if p == pid]
    assert same_backend[-3:] == ["BEGIN READ ONLY", "SET standard_conforming_strings = on",
                                 "EXPLAIN (FORMAT JSON) " + marker]


def test_a_fast_definition_that_writes_fails_on_the_server_and_changes_nothing(scs_off_server):
    client = _client(scs_off_server)
    with pytest.raises(Exception, match=READ_ONLY_ERROR):
        extractor.preview(client, "SELECT add_order() AS v")
    with pytest.raises(Exception, match=READ_ONLY_ERROR):
        source_for(client).preview("SELECT nextval('seq') AS v", 1)
    assert _admin(scs_off_server, "SELECT count(*) FROM orders") == [(2,)]
    assert _admin(scs_off_server, "SELECT last_value, is_called FROM seq") == [(1, False)]


def test_a_full_extract_streams_every_row_through_a_cursor_inside_the_read_only_transaction(scs_off_server, tmp_path):
    client = _client(scs_off_server)
    sql = ("SELECT g AS n, current_setting('transaction_read_only') AS ro, "
           "(SELECT count(*) FROM pg_cursors) AS cursors, pg_backend_pid() AS pid FROM generate_series(1, 2500) g")
    with patch.object(artifacts, "_ARTIFACT_ROOT", tmp_path / "fast"), patch.object(extractor, "BATCH_ROWS", 500):
        result = extractor.extract_to_artifact(client, sql, "rows_copy", "conn")
    con = artifacts.connect_encrypted(result.artifact_path, result.artifact_key, read_only=True)
    try:
        count, ro_values, cursors, pids = con.execute(
            "SELECT count(*), list(DISTINCT ro), list(DISTINCT cursors), list(DISTINCT pid) FROM rows_copy"
        ).fetchone()
    finally:
        con.close()
    assert (count, ro_values, cursors) == (2500, ["on"], [1])
    assert _discovery_backend(client)[:3] == (pids[0], "off", "off")


# --------------------------------------------------------------- round trips on a reused pooled connection

@pytest.fixture
def fresh_pool():
    """The switches are read when an engine is built: each test using this
    starts from an empty engine cache and leaves it empty."""
    engine_pool.dispose_all()
    yield
    engine_pool.dispose_all()


@pytest.mark.parametrize("env,ping", [
    ({}, []),
    ({"BOW_PG_IDLE_PING_S": "0"}, ["SELECT 1"]),
    ({"BOW_PG_PING_LISTENER": "off"}, ["SELECT 1"]),
], ids=["listener", "idle-ping", "switch-off"])
@pytest.mark.parametrize("schema,setup", [(None, []), ("public", ["SET search_path TO public"])],
                         ids=["no-schema", "pinned-schema"])
def test_a_read_on_a_reused_connection_sends_begin_query_rollback(logged_server, fresh_pool, monkeypatch,
                                                                  env, ping, schema, setup):
    """Three statements (four with pinned schemas). The ping listener adds a
    `SELECT 1` only past the idle threshold; the switch off gives back the
    pre-ping's `SELECT 1` on every reuse."""
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    client = _client(logged_server, schema=schema)
    tag = uuid.uuid4().hex[:12]
    client.execute_query(f"SELECT 'reuse-1-{tag}' AS m")
    second = f"SELECT pg_backend_pid() AS pid, 'reuse-2-{tag}' AS m"
    pid = int(client.execute_query(second)["pid"].iloc[0])
    log = _backend_log(logged_server, pid, f"reuse-1-{tag}", f"reuse-2-{tag}")
    assert log[log.index("ROLLBACK") + 1:] == [*ping, "BEGIN READ ONLY", *setup, second, "ROLLBACK"]


def _end_backend(settings: dict, pid: int) -> None:
    _admin(settings, f"SELECT pg_terminate_backend({pid})")
    deadline = time.monotonic() + 10
    while _admin(settings, f"SELECT count(*) FROM pg_stat_activity WHERE pid = {pid}") != [(0,)]:
        assert time.monotonic() < deadline, "the backend did not end"
        time.sleep(0.1)


def test_a_pooled_backend_the_server_ended_is_replaced_for_a_read_and_a_streamed_read(logged_server, fresh_pool):
    client = _client(logged_server)
    pid = int(client.execute_query("SELECT pg_backend_pid() AS pid")["pid"].iloc[0])
    _end_backend(logged_server, pid)
    df = client.execute_query("SELECT 7 AS n, pg_backend_pid() AS pid")
    assert int(df["n"].iloc[0]) == 7
    replacement = int(df["pid"].iloc[0])
    assert replacement != pid
    _end_backend(logged_server, replacement)
    _cols, rows = source_for(client).preview("SELECT g AS n FROM generate_series(1, 3) g", 5)
    assert rows == [[1], [2], [3]]


def test_a_plain_connect_after_a_read_can_write_on_the_same_backend(logged_server, fresh_pool):
    client = _client(logged_server)
    pid = int(client.execute_query("SELECT pg_backend_pid() AS pid")["pid"].iloc[0])
    tag = uuid.uuid4().hex[:12]
    with client.connect() as conn:
        assert conn.exec_driver_sql("SELECT pg_backend_pid()").scalar() == pid
        conn.exec_driver_sql(f"INSERT INTO scratch VALUES ('{tag}')")
        conn.commit()
    assert _admin(logged_server, f"SELECT count(*) FROM scratch WHERE v = '{tag}'") == [(1,)]


def test_a_reused_read_behind_pgbouncer_is_still_right(bouncer, fresh_pool):
    client = _client(bouncer)
    for _ in range(2):
        df = client.execute_query(BACKSLASH_SQL)
        assert df.to_dict("records") == [{"v": BACKSLASH_VALUE, "scs": "on"}]
