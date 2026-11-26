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
* an agent read on a reused pooled connection costs two messages: `BEGIN
  READ ONLY;` (with `SET LOCAL search_path` when schemas are pinned) and the
  query in one, then `ROLLBACK`; three on a server reporting the literal
  setting off. No health-check ping unless the connection looks dead or sat
  idle past the threshold (`BOW_PG_PING_LISTENER=off` gives back the
  pre-ping's `SELECT 1`); `BOW_PG_ONE_MESSAGE=off` gives back the
  read-only `connect()` read (`BEGIN READ ONLY`, the query, `ROLLBACK`). A
  backend the server ended is replaced without failing the next read;
* the one message leaves nothing behind on any exit (errors, cancels,
  kills, a query that ends its own transaction), also behind PgBouncer, and
  its results and errors read as the read-only `connect()` read's do. A
  connection left idle by a message (a query that committed, a syntax
  error the server refused the whole message for) is dropped; one whose
  transaction failed is rolled back and reused.

Needs Docker. CI does not collect tests/integrations; these run on the test
harness. Without a Docker daemon they skip, or fail when CI is set.
"""

from __future__ import annotations

import logging
import os
import random
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from unittest.mock import patch

import pandas as pd
import pytest
import sqlalchemy

from app.ai.code_execution.code_execution import StreamingCodeExecutor, UnsafeSQLError
from app.ai.code_execution.sql_guard import ensure_read_only
from app.data_sources import engine_pool, query_cancellation
from app.data_sources.clients import postgresql_client
from app.data_sources.clients._exact_sql import read_sql_exact
from app.data_sources.clients._pg_session import prepare_pg_session, verify_pg_session_after
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
# What an agent read's one message starts with, before the checked query.
ONE_MESSAGE = "BEGIN READ ONLY;\n"
SWITCH_ON_FIRST = "BEGIN READ ONLY; SET LOCAL standard_conforming_strings = on"


def _one_message(sql: str, schema: str | None = None) -> str:
    if schema:
        return f"BEGIN READ ONLY; SET LOCAL search_path TO {schema};\n{sql}"
    return ONE_MESSAGE + sql

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
    """(backend pid, statement) for every statement the server logged; a
    message holding several statements is one entry, its line breaks kept
    (the server log continues it on lines that start with a tab)."""
    logs = settings["container"].get_wrapped_container().logs().decode()
    out = []
    continuing = False
    for line in logs.split("\n"):
        if continuing and line.startswith("\t"):
            pid, statement = out[-1]
            out[-1] = (pid, statement + "\n" + line[1:])
            continue
        continuing = False
        if "statement: " not in line or "[" not in line:
            continue
        pid = line.split("[", 1)[1].split("]", 1)[0]
        out.append((pid, line.split("statement: ", 1)[1]))
        continuing = True
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
        ONE_MESSAGE + read, "ROLLBACK", "BEGIN", plain, "ROLLBACK"]


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


PING_SETTINGS = pytest.mark.parametrize("env,ping", [
    ({}, []),
    ({"BOW_PG_IDLE_PING_S": "0"}, ["SELECT 1"]),
    ({"BOW_PG_PING_LISTENER": "off"}, ["SELECT 1"]),
], ids=["listener", "idle-ping", "pre-ping"])


def _reused_read_log(settings, client, tag: str, second: str) -> list[str]:
    """What the server logged for `second`, a read on the connection the
    read before it returned, after that read's ROLLBACK."""
    client.execute_query(f"SELECT 'reuse-1-{tag}' AS m")
    pid = int(client.execute_query(second)["pid"].iloc[0])
    log = _backend_log(settings, pid, f"reuse-1-{tag}", f"reuse-2-{tag}")
    return log[log.index("ROLLBACK") + 1:]


@PING_SETTINGS
@pytest.mark.parametrize("schema", [None, "public"], ids=["no-schema", "pinned-schema"])
def test_a_read_on_a_reused_connection_is_one_message_and_a_rollback(logged_server, fresh_pool, monkeypatch,
                                                                     env, ping, schema):
    """Two messages, pinned schemas or not. The ping listener adds a
    `SELECT 1` only past the idle threshold; the listener off gives back the
    pre-ping's `SELECT 1` on every reuse."""
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    client = _client(logged_server, schema=schema)
    tag = uuid.uuid4().hex[:12]
    second = f"SELECT pg_backend_pid() AS pid, 'reuse-2-{tag}' AS m"
    assert _reused_read_log(logged_server, client, tag, second) == [*ping, _one_message(second, schema), "ROLLBACK"]


@PING_SETTINGS
@pytest.mark.parametrize("schema,setup", [(None, []), ("public", ["SET search_path TO public"])],
                         ids=["no-schema", "pinned-schema"])
def test_with_the_one_message_switch_off_a_reused_read_is_begin_query_rollback(logged_server, fresh_pool,
                                                                              monkeypatch, env, ping, schema, setup):
    monkeypatch.setenv("BOW_PG_ONE_MESSAGE", "off")
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    client = _client(logged_server, schema=schema)
    tag = uuid.uuid4().hex[:12]
    second = f"SELECT pg_backend_pid() AS pid, 'reuse-2-{tag}' AS m"
    assert _reused_read_log(logged_server, client, tag, second) == [
        *ping, "BEGIN READ ONLY", *setup, second, "ROLLBACK"]


@pytest.mark.parametrize("schema", [None, "public"], ids=["no-schema", "pinned-schema"])
def test_on_a_server_with_standard_strings_off_a_reused_read_is_three_messages(scs_off_server, fresh_pool, schema):
    """The switch is its own message: the server parses a whole message
    before it runs any of it."""
    client = _client(scs_off_server, schema=schema)
    tag = uuid.uuid4().hex[:12]
    second = (f"SELECT pg_backend_pid() AS pid, current_setting('standard_conforming_strings') AS scs, "
              f"'reuse-2-{tag}' AS m")
    body = f"SET LOCAL search_path TO {schema};\n{second}" if schema else second
    assert _reused_read_log(scs_off_server, client, tag, second) == [SWITCH_ON_FIRST, body, "ROLLBACK"]
    assert client.execute_query(second)["scs"].tolist() == ["on"]


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


# --------------------------------------------------------------- the one message leaves nothing behind

def _idle_in_transaction(settings: dict) -> list:
    return _admin(settings, "SELECT pid, state FROM pg_stat_activity WHERE datname = current_database() "
                            "AND pid <> pg_backend_pid() AND state LIKE 'idle in transaction%'")


def _state(settings: dict, pid: int):
    return _admin(settings, f"SELECT state FROM pg_stat_activity WHERE pid = {pid}")


def _running(settings: dict, marker: str) -> int:
    """The backend running the statement holding `marker`."""
    deadline = time.monotonic() + 20
    while True:
        rows = _admin(settings, "SELECT pid FROM pg_stat_activity WHERE state = 'active' "
                                f"AND pid <> pg_backend_pid() AND query LIKE '%{marker}%'")
        if rows:
            return int(rows[0][0])
        assert time.monotonic() < deadline, f"nothing ran {marker}"
        time.sleep(0.05)


def _in_thread(fn, *args):
    """Start `fn(*args)` in a thread; (thread, outcome list)."""
    outcome = []

    def run():
        try:
            outcome.append(fn(*args))
        except BaseException as e:  # the test inspects it
            outcome.append(e)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, outcome


@pytest.fixture(scope="module")
def bouncer_schema(scs_off_server):
    """A schema and the pg_trgm extension on the server behind PgBouncer."""
    _admin(scs_off_server, "CREATE SCHEMA IF NOT EXISTS rvw_pgb")
    _admin(scs_off_server, "CREATE TABLE IF NOT EXISTS rvw_pgb.rvw_t AS SELECT 'rvw_pgb'::text AS m")
    _admin(scs_off_server, "CREATE EXTENSION IF NOT EXISTS pg_trgm")
    return "rvw_pgb"


def test_behind_pgbouncer_a_read_sees_its_schema_and_changes_nothing_for_another_client(
        bouncer, bouncer_schema, scs_off_server, fresh_pool):
    import psycopg2

    client = _client(bouncer, schema=bouncer_schema)
    for _ in range(3):
        assert client.execute_query("SELECT m FROM rvw_t")["m"].tolist() == [bouncer_schema]
    baseline = _admin(bouncer, "SELECT current_setting('statement_timeout'), show_limit(), "
                               "current_setting('search_path')")
    # LISTEN needs a session of its own: straight to the server.
    listener = psycopg2.connect(host=scs_off_server["host"], port=scs_off_server["port"],
                                user=scs_off_server["user"], password=scs_off_server["password"],
                                dbname=scs_off_server["dbname"])
    listener.autocommit = True
    try:
        with listener.cursor() as cur:
            cur.execute("LISTEN rvw_chan")
        df = client.execute_query(
            "SELECT set_config('statement_timeout', '4321', false) AS a, public.set_limit(0.9) AS b, "
            "set_config('search_path', 'pg_catalog', false) AS c, pg_notify('rvw_chan', 'x') AS d, "
            "pg_backend_pid() AS pid")
        assert df["a"].tolist() == ["4321ms"]
        pid = int(df["pid"].iloc[0])
        # Another client through PgBouncer, on the same server backend.
        other = _admin(bouncer, "SELECT pg_backend_pid(), current_setting('statement_timeout'), show_limit(), "
                                "current_setting('search_path')")
        assert other == [(pid, *baseline[0])]
        time.sleep(0.2)
        listener.poll()
        assert listener.notifies == []
    finally:
        listener.close()


def test_behind_pgbouncer_the_backend_is_idle_after_an_error_and_after_a_cancel(bouncer, scs_off_server,
                                                                              fresh_pool):
    # The server's view comes from a session of its own, straight to the
    # server: through PgBouncer it could be the backend under test.
    client = _client(bouncer)
    with pytest.raises(sqlalchemy.exc.DataError, match="division by zero"):
        client.execute_query("SELECT 1/0 AS v")
    assert _idle_in_transaction(scs_off_server) == []
    tag = uuid.uuid4().hex[:12]
    thread, outcome = _in_thread(client.execute_query, f"SELECT pg_sleep(30) AS s, 'pgb-cancel-{tag}' AS m")
    pid = _running(scs_off_server, f"pgb-cancel-{tag}")
    assert query_cancellation.cancel_thread(client, thread.ident) == "cancelled (postgresql cancel)"
    thread.join(30)
    assert "canceling statement" in str(outcome[0])
    assert _state(scs_off_server, pid) == [("idle",)]
    assert _idle_in_transaction(scs_off_server) == []
    assert client.execute_query("SELECT 2 AS n")["n"].tolist() == [2]


def test_a_query_that_ends_its_own_transaction_returns_no_rows_and_its_connection_is_not_reused(
        logged_server, fresh_pool):
    """Below the guard (it refuses transaction control). Known gap of this
    check: a query that opens a transaction again afterwards passes it, and
    is ended by the ROLLBACK like any other."""
    client = _client(logged_server)
    pid = int(client.execute_query("SELECT pg_backend_pid() AS pid")["pid"].iloc[0])
    with pytest.raises(RuntimeError, match="transaction"):
        client.execute_query("COMMIT; SELECT 1 AS v")
    assert _idle_in_transaction(logged_server) == []
    assert int(client.execute_query("SELECT pg_backend_pid() AS pid")["pid"].iloc[0]) != pid
    # A write ahead of the COMMIT is refused by the read-only transaction.
    tag = uuid.uuid4().hex[:12]
    with pytest.raises(Exception, match=READ_ONLY_ERROR):
        client.execute_query(f"INSERT INTO scratch VALUES ('{tag}'); COMMIT; SELECT 1 AS v")
    assert _admin(logged_server, f"SELECT count(*) FROM scratch WHERE v = '{tag}'") == [(0,)]


@pytest.mark.parametrize("sql, fails", [
    ("COMMIT; BEGIN; SELECT pg_backend_pid() AS pid", False),
    ("COMMIT; BEGIN; SELECT 1/0 AS pid", True),
])
def test_a_smuggled_begin_never_stays_in_the_pool(logged_server, fresh_pool, sql, fails):
    client = _client(logged_server)
    pid = int(client.execute_query("SELECT pg_backend_pid() AS pid")["pid"].iloc[0])
    if fails:
        with pytest.raises(sqlalchemy.exc.DataError):
            client.execute_query(sql)
    else:
        assert int(client.execute_query(sql)["pid"].iloc[0]) == pid
    assert _state(logged_server, pid) == [("idle",)]
    assert int(client.execute_query("SELECT pg_backend_pid() AS pid")["pid"].iloc[0]) == pid


@pytest.mark.parametrize("sql", ["SELECT id FROM orders FOR UPDATE", "SELECT add_order() AS v",
                                 "SELECT nextval('seq') AS v", "SELECT id FROM orders FOR SHARE"])
def test_writes_and_locking_reads_are_refused_by_the_server(server, sql):
    with pytest.raises(Exception, match=READ_ONLY_ERROR):
        _client(server).execute_query(sql)
    assert _admin(server, "SELECT count(*) FROM orders") == [(2,)]
    assert _admin(server, "SELECT last_value, is_called FROM seq") == [(1, False)]


def test_show_and_explain_analyze_still_work(server):
    client = _client(server)
    assert client.execute_query("SHOW standard_conforming_strings").to_dict("records") == [
        {"standard_conforming_strings": "on"}]
    plan = client.execute_query("EXPLAIN ANALYZE SELECT id FROM orders")
    assert "Execution Time" in " ".join(plan.iloc[:, 0])


def test_a_backend_ended_between_the_rows_and_the_rollback_still_returns_the_rows(logged_server, fresh_pool,
                                                                                  monkeypatch):
    client = _client(logged_server)
    after_read = postgresql_client.verify_pg_session_after
    ended = []

    def end_the_backend_then_check(conn):
        ended.append(conn.connection.dbapi_connection.get_backend_pid())
        _end_backend(logged_server, ended[-1])
        after_read(conn)

    with monkeypatch.context() as m:
        m.setattr(postgresql_client, "verify_pg_session_after", end_the_backend_then_check)
        assert client.execute_query("SELECT 7 AS n").to_dict("records") == [{"n": 7}]
    assert ended
    assert int(client.execute_query("SELECT pg_backend_pid() AS pid")["pid"].iloc[0]) != ended[0]


def test_a_query_killed_while_running_is_reported_and_not_run_again(logged_server, fresh_pool):
    client = _client(logged_server)
    tag = uuid.uuid4().hex[:12]
    marker = f"killed-{tag}"
    sql = f"SELECT pg_sleep(30) AS s, '{marker}' AS m"
    thread, outcome = _in_thread(client.execute_query, sql)
    _end_backend(logged_server, _running(logged_server, marker))
    thread.join(30)
    assert isinstance(outcome[0], sqlalchemy.exc.OperationalError)
    time.sleep(0.5)
    assert sum(s.endswith(sql) for _pid, s in _statements(logged_server)) == 1
    assert client.execute_query("SELECT 1 AS n")["n"].tolist() == [1]


def test_cancel_through_query_cancellation_stops_the_read_and_the_backend_serves_the_next(
        logged_server, fresh_pool):
    import psycopg2.errors

    client = _client(logged_server)
    tag = uuid.uuid4().hex[:12]
    marker = f"cancel-{tag}"
    thread, outcome = _in_thread(client.execute_query, f"SELECT pg_sleep(30) AS s, '{marker}' AS m")
    pid = _running(logged_server, marker)
    assert query_cancellation.cancel_thread(client, thread.ident) == "cancelled (postgresql cancel)"
    thread.join(30)
    assert isinstance(getattr(outcome[0], "orig", None), psycopg2.errors.QueryCanceled)
    assert _state(logged_server, pid) == [("idle",)]
    assert int(client.execute_query("SELECT pg_backend_pid() AS pid")["pid"].iloc[0]) == pid


def _read_log(settings: dict, pid: int, marker: str) -> list[str]:
    """Backend `pid`'s statement holding `marker`, with the one before and
    the one after it."""
    deadline = time.monotonic() + 10
    while True:
        mine = [s for p, s in _statements(settings) if p == str(pid)]
        at = [i for i, s in enumerate(mine) if marker in s]
        if at and at[0] + 1 < len(mine):
            return mine[at[0] - 1:at[0] + 2]
        assert time.monotonic() < deadline, f"backend {pid} logged {mine[-6:]}"
        time.sleep(0.2)


@pytest.mark.parametrize("order", [("read", "fast", "read", "read", "fast"),
                                   ("fast", "read", "read", "fast", "read")])
def test_a_fast_read_and_an_agent_read_share_one_pooled_connection(logged_server, fresh_pool, order):
    client = _client(logged_server)
    tag = uuid.uuid4().hex[:12]
    pids, reads = [], []
    for i, kind in enumerate(order):
        sql = (f"SELECT pg_backend_pid() AS pid, current_setting('transaction_read_only') AS ro, "
               f"'share-{i}-{tag}' AS m")
        if kind == "read":
            rows = client.execute_query(sql).values.tolist()
            reads.append((i, sql))
        else:
            # FAST streams through a named cursor, which needs a transaction.
            _cols, rows = source_for(client).preview(sql, 5)
        assert [r[1:] for r in rows] == [["on", f"share-{i}-{tag}"]]
        pids.append(int(rows[0][0]))
    assert len(set(pids)) == 1
    for i, sql in reads:
        if i > 0:
            assert _read_log(logged_server, pids[0], f"share-{i}-{tag}") == ["ROLLBACK", ONE_MESSAGE + sql, "ROLLBACK"]


HEBREW = chr(0x05E9) + chr(0x05DC) + chr(0x05D5) + chr(0x05DD)
SCHEMAS = [('"Sales"', "Sales"), ('"a:b"', "a:b"), ('"a :b"', "a :b"), ('"123"', "123"), ("rvw_a", "rvw_a"),
           (HEBREW, HEBREW)]


@pytest.fixture(scope="module")
def schema_server(server):
    for name, marker in SCHEMAS:
        _admin(server, f"CREATE SCHEMA {name}")
        _admin(server, f"CREATE TABLE {name}.rvw_t AS SELECT '{marker}'::text AS m")
    _admin(server, "CREATE TABLE public.rvw_t AS SELECT 'public'::text AS m")
    return server


@pytest.mark.parametrize("setting, expected", [
    ("'Sales'", "Sales"), ('"a:b"', "a:b"), ('"a :b"', "a :b"), ("123", "123"), ("RVW_A", "rvw_a"),
    ('"$user", public', "public"), (HEBREW, HEBREW),
], ids=["single-quoted", "colon", "bind-like-colon", "digits", "upper-case", "user-public", "hebrew"])
def test_a_schema_setting_that_works_today_works_for_agent_and_fast_reads(schema_server, fresh_pool, setting,
                                                                          expected):
    client = _client(schema_server, schema=setting)
    assert client.execute_query("SELECT m FROM rvw_t")["m"].tolist() == [expected]
    _cols, rows = source_for(client).preview("SELECT m FROM rvw_t", 5)
    assert rows == [[expected]]


@pytest.mark.parametrize("setting", ["rvw_a;", "rvw_a --x", "rvw" + BS + "a"],
                         ids=["semicolon", "comment", "backslash"])
def test_a_schema_setting_outside_the_rule_is_refused_naming_the_setting(schema_server, fresh_pool, setting):
    client = _client(schema_server, schema=setting)
    with pytest.raises(RuntimeError, match="schema") as error:
        client.execute_query("SELECT m FROM rvw_t")
    assert setting not in str(error.value)


def test_four_threads_with_random_cancels_get_their_own_rows_and_leave_nothing_open(server, fresh_pool):
    import psycopg2.errors

    client = _client(server)
    engine = engine_pool.get_engine(client.pg_uri, connect_args={"sslmode": client.sslmode})
    returned_in_autocommit = []
    sqlalchemy.event.listen(engine, "reset",
                            lambda dbapi, record, state: returned_in_autocommit.append(bool(dbapi.autocommit)),
                            insert=True)
    wrong, unexpected, cancelled = [], [], []
    idents = []
    done = threading.Event()

    def worker(w):
        idents.append(threading.get_ident())
        rng = random.Random(w)
        for i in range(50):
            n = w * 1000 + i
            try:
                df = client.execute_query(f"SELECT {n} AS n, pg_sleep({rng.uniform(0.002, 0.02):.3f}) AS s")
                if df["n"].tolist() != [n]:
                    wrong.append((n, df["n"].tolist()))
            except Exception as e:
                if isinstance(getattr(e, "orig", None), psycopg2.errors.QueryCanceled):
                    cancelled.append(n)
                else:
                    unexpected.append(repr(e))

    def canceller():
        rng = random.Random(99)
        while not done.is_set():
            time.sleep(rng.uniform(0.002, 0.03))
            if idents:
                query_cancellation.cancel_thread(client, rng.choice(idents))

    workers = [threading.Thread(target=worker, args=(w,)) for w in range(4)]
    stopper = threading.Thread(target=canceller)
    stopper.start()
    for t in workers:
        t.start()
    for t in workers:
        t.join(120)
    done.set()
    stopper.join(10)
    assert wrong == [] and unexpected == []
    assert cancelled, "the cancels reached some reads"
    assert _idle_in_transaction(server) == []
    assert returned_in_autocommit and not any(returned_in_autocommit)


# Queries whose results the one message must give exactly as the read-only
# connect() read does: columns, dtypes and values.
PARITY_SHAPES = [
    "SELECT 1 AS a;",
    "SELECT 1 AS a -- a note at the end",
    "/* a note */ SELECT 1 AS a",
    "WITH x AS (SELECT 2 AS b) SELECT b FROM x",
    "SELECT ':x' AS a, '%' AS b, 'a%%b' AS c, '12:30'::time AS t, 1.25::numeric(5, 2) AS n, '1'::int::text AS s",
    "SELECT now()::date AS d, NULL::int AS z, ARRAY[1, 2] AS arr, '{\"a\": 1}'::jsonb AS j, 1.5::float8 AS f, "
    "true AS b",
    "SELECT g AS n, md5(g::text) AS h FROM generate_series(1, 100000) g",
    "SHOW standard_conforming_strings",
    "EXPLAIN SELECT 1",
    "SELECT 1 AS a,\n  2 AS b\n  -- two lines",
]


def _read_only_connect_read(client, sql):
    with client.connect(read_only=True) as conn:
        prepare_pg_session(conn, sql)
        df = read_sql_exact(sql, conn)
        verify_pg_session_after(conn)
    return df


def test_results_are_the_same_as_the_read_only_connect_reads(server, fresh_pool):
    from tests.unit.test_sql_guard_postgres import LEGITIMATE_READS

    client = _client(server)
    for sql in LEGITIMATE_READS + PARITY_SHAPES:
        pd.testing.assert_frame_equal(client.execute_query(sql), _read_only_connect_read(client, sql), obj=sql)


def _failure(client, sql) -> tuple:
    with pytest.raises(Exception) as raised:
        client.execute_query(sql)
    e = raised.value
    return type(e), e.args, getattr(e, "statement", None), str(getattr(e, "orig", None))


ERROR_SHAPES = [
    "SELECT 1 AS a,\n  no_such_column AS b",
    "SELECT 1 AS a" + "\n" * 8 + ", no_such_column AS b",
    "SELEC 1",
    "SELECT 1/0 AS a",
    "",
    " ",
    "-- only a note",
    ";",
]


@pytest.mark.parametrize("schema", [None, "public"], ids=["no-schema", "pinned-schema"])
def test_errors_read_as_the_read_only_connect_reads_errors(server, fresh_pool, monkeypatch, schema):
    client = _client(server, schema=schema)
    one_message = [_failure(client, sql) for sql in ERROR_SHAPES]
    engine_pool.dispose_all()
    monkeypatch.setenv("BOW_PG_ONE_MESSAGE", "off")
    read_only_connect = [_failure(client, sql) for sql in ERROR_SHAPES]
    assert one_message == read_only_connect
    assert "LINE 9: , no_such_column" in one_message[1][3]
    assert not any("BEGIN READ ONLY" in str(failure) for failure in one_message)


def test_an_error_inside_the_prefix_is_the_schema_setting_error_on_both_paths(server, fresh_pool, monkeypatch):
    """A reserved word passes the schema rule and fails every query, as it
    did before; on both paths it is the schema setting error, without the
    schema names, chained from the server's error."""
    client = _client(server, schema="user")
    with pytest.raises(RuntimeError) as one_message:
        client.execute_query("SELECT 1 AS a")
    assert isinstance(one_message.value.__cause__, sqlalchemy.exc.ProgrammingError)
    engine_pool.dispose_all()
    monkeypatch.setenv("BOW_PG_ONE_MESSAGE", "off")
    with pytest.raises(RuntimeError) as read_only_connect:
        client.execute_query("SELECT 1 AS a")
    assert str(one_message.value) == str(read_only_connect.value)
    assert str(one_message.value).startswith("The connection's schema setting could not be applied on the server")
    assert "user" not in str(one_message.value)


# --------------------------------------------------------------- idle after a send: dropped

def _logs_from(settings: dict, first: str, last: str) -> dict:
    """Each backend's statements from the one holding `first` on, read once
    the ROLLBACK after the one holding `last` is logged."""
    deadline = time.monotonic() + 10
    while True:
        statements = _statements(settings)
        at = [i for i, (_p, s) in enumerate(statements) if last in s]
        if at and any(p == statements[at[0]][0] and s == "ROLLBACK" for p, s in statements[at[0] + 1:]):
            start = next(i for i, (_p, s) in enumerate(statements) if first in s)
            out: dict = {}
            for p, s in statements[start:]:
                out.setdefault(int(p), []).append(s)
            return out
        assert time.monotonic() < deadline, f"{last} was never rolled back: {statements[-8:]}"
        time.sleep(0.2)


def _three_reads(settings: dict, bad: str, error, schema=None):
    """A read, `bad` (which raises `error`), a read: (first pid, last pid,
    last read's statement_timeout, each backend's statements from the
    first read on)."""
    client = _client(settings, schema=schema)
    tag = uuid.uuid4().hex[:12]
    first = f"SELECT pg_backend_pid() AS pid, 'drop-1-{tag}' AS m"
    last = f"SELECT pg_backend_pid() AS pid, current_setting('statement_timeout') AS t, 'drop-3-{tag}' AS m"
    pid = int(client.execute_query(first)["pid"].iloc[0])
    with pytest.raises(error):
        client.execute_query(bad.format(tag=tag))
    df = client.execute_query(last)
    assert _idle_in_transaction(settings) == []
    logs = _logs_from(settings, f"drop-1-{tag}", f"drop-3-{tag}")
    return pid, int(df["pid"].iloc[0]), df["t"].iloc[0], logs, first, bad.format(tag=tag), last


@pytest.mark.parametrize("bad, error", [
    ("SELECT set_config('statement_timeout', '4321', false) AS a, 'drop-2-{tag}' AS m; COMMIT",
     sqlalchemy.exc.ResourceClosedError),
    ("SELECT set_config('statement_timeout', '4321', false) AS a, 'drop-2-{tag}' AS m; COMMIT; SELECT 1/0 AS b",
     sqlalchemy.exc.DataError),
], ids=["no-rows-after-commit", "error-after-commit"])
def test_a_query_that_commits_and_then_fails_drops_its_connection_and_its_session_change(logged_server, fresh_pool,
                                                                                       bad, error):
    """Below the guard (it refuses transaction control). The session change
    committed after BOW's transaction stays with the dropped backend."""
    baseline = _admin(logged_server, "SELECT current_setting('statement_timeout')")[0][0]
    pid, next_pid, timeout, logs, first, sent, last = _three_reads(logged_server, bad, error)
    assert next_pid != pid
    assert timeout == baseline
    assert logs[pid] == [ONE_MESSAGE + first, "ROLLBACK", ONE_MESSAGE + sent]
    assert logs[next_pid] == [ONE_MESSAGE + last, "ROLLBACK"]


@pytest.mark.parametrize("bad, error", [
    ("SELECT no_such_column, 'drop-2-{tag}' AS m", sqlalchemy.exc.ProgrammingError),
    ("SELECT 1/0 AS a, 'drop-2-{tag}' AS m", sqlalchemy.exc.DataError),
], ids=["missing-column", "division-by-zero"])
def test_an_error_that_leaves_the_transaction_failed_is_rolled_back_on_the_same_backend(logged_server, fresh_pool,
                                                                                      bad, error):
    pid, next_pid, _timeout, logs, first, sent, last = _three_reads(logged_server, bad, error)
    assert next_pid == pid
    assert logs[pid] == [ONE_MESSAGE + first, "ROLLBACK", ONE_MESSAGE + sent, "ROLLBACK", ONE_MESSAGE + last,
                         "ROLLBACK"]


def test_a_syntax_error_drops_the_connection_by_design(logged_server, fresh_pool):
    """The server refuses the whole message before BOW's BEGIN runs, so the
    connection is idle, as after a COMMIT: dropped. It logs no statement."""
    pid, next_pid, _timeout, logs, first, _sent, last = _three_reads(
        logged_server, "SELEC 'drop-2-{tag}' AS m", sqlalchemy.exc.ProgrammingError)
    assert next_pid != pid
    assert logs[pid] == [ONE_MESSAGE + first, "ROLLBACK"]
    assert logs[next_pid] == [ONE_MESSAGE + last, "ROLLBACK"]


def test_a_syntax_error_after_the_standard_strings_switch_stays_on_the_same_backend(scs_off_server, fresh_pool):
    """BEGIN ran in the switch's message: the refused query leaves the
    transaction failed, and the ROLLBACK ends it."""
    pid, next_pid, _timeout, logs, first, _sent, last = _three_reads(
        scs_off_server, "SELEC 'drop-2-{tag}' AS m", sqlalchemy.exc.ProgrammingError)
    assert next_pid == pid
    assert logs[pid] == [first, "ROLLBACK", SWITCH_ON_FIRST, "ROLLBACK", SWITCH_ON_FIRST, last, "ROLLBACK"]


# --------------------------------------------------------------- interrupts and the pool's backstop

def _next_checkout(client) -> tuple:
    """(backend pid, autocommit, transaction status) of the connection the
    pool hands out next, before anything is sent on it."""
    engine = engine_pool.get_engine(client.pg_uri, connect_args={"sslmode": client.sslmode})
    with engine.connect() as conn:
        raw = conn.connection.dbapi_connection
        state = (raw.autocommit, raw.get_transaction_status())
        return (raw.get_backend_pid(), *state)


@pytest.mark.parametrize("where", ["fetch", "rollback"])
def test_an_interrupt_during_the_read_or_the_rollback_never_pools_an_open_or_autocommit_connection(
        logged_server, fresh_pool, monkeypatch, where):
    from psycopg2.extensions import TRANSACTION_STATUS_IDLE

    client = _client(logged_server)
    pid = int(client.execute_query("SELECT pg_backend_pid() AS pid")["pid"].iloc[0])
    if where == "fetch":
        def interrupted(self, *args, **kwargs):
            raise KeyboardInterrupt

        monkeypatch.setattr(sqlalchemy.engine.CursorResult, "fetchall", interrupted)
    else:
        send = sqlalchemy.engine.Connection.exec_driver_sql

        def interrupted(self, statement, *args, **kwargs):
            if statement == "ROLLBACK":
                raise KeyboardInterrupt
            return send(self, statement, *args, **kwargs)

        monkeypatch.setattr(sqlalchemy.engine.Connection, "exec_driver_sql", interrupted)
    with pytest.raises(KeyboardInterrupt):
        client.execute_query("SELECT 1 AS n")
    monkeypatch.undo()
    next_pid, autocommit, status = _next_checkout(client)
    assert (autocommit, status) == (False, TRANSACTION_STATUS_IDLE)
    # Rolled back on the same backend, or dropped by the pool's backstop.
    assert (next_pid == pid) is (where == "fetch")
    assert _idle_in_transaction(logged_server) == []
    assert client.execute_query("SELECT 2 AS n")["n"].tolist() == [2]


def test_a_connection_returned_in_autocommit_is_dropped_by_the_pools_backstop(server, fresh_pool):
    client = _client(server)
    engine = engine_pool.get_engine(client.pg_uri, connect_args={"sslmode": client.sslmode})
    with engine.connect() as conn:
        pid = conn.exec_driver_sql("SELECT pg_backend_pid()").scalar()
        conn.rollback()
        conn.connection.dbapi_connection.autocommit = True
    next_pid, autocommit, _status = _next_checkout(client)
    assert next_pid != pid and autocommit is False
