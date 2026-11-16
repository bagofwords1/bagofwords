"""SQL Server: BOW must be a reader that gives way to the customer's writers.

Under SQL Server's default READ COMMITTED (without READ_COMMITTED_SNAPSHOT),
every BOW SELECT takes shared locks, so it can deadlock with the customer's
writers or block them. The promises pinned here:

  * every session BOW opens is set up (deadlock priority, lock timeout,
    isolation level) before any query runs on it, pooled or probe;
  * connections whose settings differ never share a session;
  * a session whose setup fails is never used;
  * the connection test fails when SNAPSHOT is chosen but the database
    does not allow it, and warns when the login cannot KILL;
  * a timed-out query that BOW could not stop on the server is reported
    as still running, with the missing permission named.

No SQL Server is needed. The ODBC driver is the boundary: it is replaced
by a SQLite connection that records every statement and answers the few
SQL Server catalog queries with canned values. SQLAlchemy's real pool and
event machinery run unchanged.
"""

from __future__ import annotations

import sqlite3
import threading
from urllib.parse import unquote_plus

import pytest
import sqlalchemy

from app.data_sources import engine_pool
from app.data_sources import query_cancellation as qc
from app.data_sources.clients.mssql_client import MSSQLClient


# --------------------------------------------------------------------------
# Fake SQL Server driver
# --------------------------------------------------------------------------

class FakeServer:
    """What the fake driver records and how it answers.

    `answers` maps a substring of an incoming statement to the SQLite SQL
    run in its place, to an exception raised instead, or to a function of
    the statement returning either (for answers that change between calls).
    """

    def __init__(self, answers=None, fail_setup=False):
        self.answers = dict(answers or {})
        self.fail_setup = fail_setup
        self.sessions = []  # one list of statements per physical connection
        self.closed = 0     # physical connections the client closed
        self.lock = threading.Lock()

    def ran(self, needle):
        """How many times a statement containing `needle` reached the driver."""
        return sum(needle in q for session in self.sessions for q in session)


class RecordingCursor:
    def __init__(self, real, log, server):
        self._real = real
        self._log = log
        self._server = server

    def execute(self, sql, *params):
        text = str(sql)
        with self._server.lock:
            self._log.append(text)
        if text.lstrip().upper().startswith("SET "):
            if self._server.fail_setup:
                raise sqlite3.OperationalError("SET failed on the server")
            return self  # SQL Server session options: nothing to run in SQLite
        for needle, answer in self._server.answers.items():
            if needle in text:
                if callable(answer):
                    answer = answer(text)
                if isinstance(answer, BaseException):
                    raise answer
                return self._real.execute(answer)
        return self._real.execute(sql, *params)

    def __getattr__(self, name):
        return getattr(self._real, name)


class RecordingConnection:
    def __init__(self, real, server):
        self._real = real
        self._server = server
        self._log = []
        with server.lock:
            server.sessions.append(self._log)

    def cursor(self, *a, **k):
        return RecordingCursor(self._real.cursor(*a, **k), self._log, self._server)

    def close(self):
        with self._server.lock:
            self._server.closed += 1
        return self._real.close()

    def __getattr__(self, name):
        return getattr(self._real, name)


@pytest.fixture
def fake_server(monkeypatch, tmp_path):
    """Route every engine the SQL Server client builds to the fake driver."""
    real_create_engine = sqlalchemy.create_engine
    server = FakeServer()
    db_file = str(tmp_path / "fake_mssql.db")

    def create_engine(uri, **kwargs):
        def creator():
            return RecordingConnection(sqlite3.connect(db_file, check_same_thread=False), server)
        kwargs.pop("connect_args", None)
        return real_create_engine(f"sqlite:///{db_file}", creator=creator, **kwargs)

    monkeypatch.setattr(sqlalchemy, "create_engine", create_engine)
    engine_pool.dispose_all()
    try:
        yield server
    finally:
        engine_pool.dispose_all()


def _client(**kwargs):
    return MSSQLClient("sql.example.test", 1433, "sales", user="bow_reader", password="pw", **kwargs)


def _setup_batch(session):
    return [s for s in session if s.lstrip().upper().startswith("SET ")]


def _run_query(client, sql="SELECT 42"):
    with client.connect() as conn:
        return conn.exec_driver_sql(sql).scalar()


# --------------------------------------------------------------------------
# Every session is set up before it is used
# --------------------------------------------------------------------------

@pytest.mark.parametrize("settings,expected", [
    ({}, ["DEADLOCK_PRIORITY LOW", "LOCK_TIMEOUT 15000", "ISOLATION LEVEL READ COMMITTED"]),
    ({"lock_timeout_seconds": 30, "isolation_level": "snapshot", "deadlock_priority_low": False},
     ["DEADLOCK_PRIORITY NORMAL", "LOCK_TIMEOUT 30000", "ISOLATION LEVEL SNAPSHOT"]),
    ({"lock_timeout_seconds": 0, "isolation_level": "read_uncommitted"},
     ["DEADLOCK_PRIORITY LOW", "LOCK_TIMEOUT -1", "ISOLATION LEVEL READ UNCOMMITTED"]),
])
def test_every_session_is_set_up_before_its_first_query(fake_server, settings, expected):
    client = _client(**settings)
    for _ in range(3):
        assert _run_query(client) == 42

    used = [s for s in fake_server.sessions if "SELECT 42" in s]
    assert used, "the query never reached the fake driver"
    for session in used:
        setup = _setup_batch(session)
        # Once per physical connection, not once per checkout.
        assert len(setup) == 1
        assert session.index(setup[0]) < session.index("SELECT 42")
        for clause in expected:
            assert clause in setup[0]


def test_probe_sessions_are_set_up_too(fake_server):
    """The connection test runs on throwaway engines (engine_pool.ephemeral);
    it must see the same session the agent's queries will."""
    client = _client(lock_timeout_seconds=7)
    with engine_pool.ephemeral():
        assert _run_query(client) == 42
    (session,) = [s for s in fake_server.sessions if "SELECT 42" in s]
    assert "LOCK_TIMEOUT 7000" in _setup_batch(session)[0]


def test_connections_with_different_settings_never_share_a_session(fake_server):
    """Same server, same login, different lock settings: the second must not be
    handed a pooled session set up for the first."""
    patient = _client(lock_timeout_seconds=0)
    strict = _client(lock_timeout_seconds=5)
    _run_query(patient, "SELECT 1 AS patient")
    _run_query(strict, "SELECT 2 AS strict")
    _run_query(patient, "SELECT 3 AS patient_again")

    def session_running(marker):
        (s,) = [s for s in fake_server.sessions if any(marker in q for q in s)]
        return _setup_batch(s)[0]

    assert "LOCK_TIMEOUT -1" in session_running("AS patient")
    assert "LOCK_TIMEOUT 5000" in session_running("AS strict")
    assert "LOCK_TIMEOUT -1" in session_running("AS patient_again")


def test_a_session_whose_setup_fails_is_never_used(fake_server):
    fake_server.fail_setup = True
    with pytest.raises(RuntimeError):
        _run_query(_client())
    assert not any("SELECT 42" in q for s in fake_server.sessions for q in s)


@pytest.mark.parametrize("attempts", [1, 3])
def test_a_session_whose_setup_fails_is_closed_not_left_open(fake_server, attempts):
    """SQLAlchemy drops the pool record when the connect hook raises, but does
    not close the driver connection; on a real server it stays open."""
    fake_server.fail_setup = True
    client = _client()
    for _ in range(attempts):
        with pytest.raises(RuntimeError):
            _run_query(client)
    assert len(fake_server.sessions) == attempts
    assert fake_server.closed == attempts


@pytest.mark.parametrize("kwargs", [
    {"lock_timeout_seconds": -1},
    {"isolation_level": "serializable"},
    {"isolation_level": "READ COMMITTED; DROP TABLE x"},
])
def test_invalid_safety_settings_are_rejected(kwargs):
    with pytest.raises(ValueError):
        _client(**kwargs)


def test_config_schema_defaults_are_the_safe_ones():
    from app.schemas.data_sources.configs import MssqlConfig

    cfg = MssqlConfig(host="h", database="d")
    assert cfg.deadlock_priority_low is True
    assert 0 < cfg.lock_timeout_seconds < 60  # shorter than the default query timeout
    assert cfg.isolation_level == "default"   # dirty reads only by explicit choice
    with pytest.raises(ValueError):
        MssqlConfig(host="h", database="d", isolation_level="serializable")


def test_sql_login_sessions_are_named_bagofwords_and_cannot_be_renamed():
    """Makes BOW's sessions identifiable in a deadlock graph. User params must
    not override it (APP is the Kerberos pool discriminator)."""
    client = _client(additional_params={"APP": "SomethingElse", "ApplicationIntent": "ReadOnly"})
    odbc = unquote_plus(client.sql_server_uri)
    assert "APP=BagOfWords;" in odbc
    assert "SomethingElse" not in odbc
    assert "ApplicationIntent=ReadOnly;" in odbc


# --------------------------------------------------------------------------
# Connection test
# --------------------------------------------------------------------------

SNAPSHOT_STATE = "snapshot_isolation_state"
KILL_PERMISSION = "ALTER ANY CONNECTION'"


@pytest.mark.parametrize("state,ok", [(0, False), (2, False), (1, True)])
def test_connection_test_checks_snapshot_is_allowed_when_chosen(fake_server, state, ok):
    fake_server.answers = {SNAPSHOT_STATE: f"SELECT {state}", KILL_PERMISSION: "SELECT 1"}
    result = _client(isolation_level="snapshot").test_connection()
    assert result["success"] is ok
    if not ok:
        assert "ALLOW_SNAPSHOT_ISOLATION" in result["message"]


SNAPSHOT_REFUSED = sqlite3.OperationalError(
    "[Microsoft][ODBC Driver 18 for SQL Server][SQL Server]Snapshot isolation transaction "
    "failed accessing database 'sales' because snapshot isolation is not allowed in this "
    "database. (3952) (SQLExecDirectW)")


@pytest.mark.parametrize("refused_on", [SNAPSHOT_STATE, "SELECT 1"])
def test_connection_test_explains_a_snapshot_refusal_wherever_it_happens(fake_server, refused_on):
    """The server refuses with error 3952 on the first read in the database,
    which may be the catalog check or the probe that runs before it."""
    fake_server.answers = {refused_on: SNAPSHOT_REFUSED}
    result = _client(isolation_level="snapshot").test_connection()
    assert result["success"] is False
    assert "ALLOW_SNAPSHOT_ISOLATION" in result["message"]


@pytest.mark.parametrize("isolation", ["default", "read_uncommitted"])
def test_connection_test_ignores_snapshot_state_when_not_chosen(fake_server, isolation):
    fake_server.answers = {SNAPSHOT_STATE: "SELECT 0", KILL_PERMISSION: "SELECT 1"}
    assert _client(isolation_level=isolation).test_connection()["success"] is True


@pytest.mark.parametrize("answer,warned", [
    ("SELECT 0", True),
    ("SELECT 1", False),
    (sqlite3.OperationalError("server-level permissions not supported"), False),
])
def test_connection_test_warns_when_bow_cannot_stop_its_queries(fake_server, answer, warned):
    """Missing ALTER ANY CONNECTION does not break the connection, so the test
    still passes, but the admin must be told."""
    fake_server.answers = {KILL_PERMISSION: answer}
    result = _client().test_connection()
    assert result["success"] is True
    assert bool(result.get("warnings")) is warned
    assert ("ALTER ANY CONNECTION" in result["message"]) is warned


EDITION = "EngineEdition"
AZURE_KILL_PERMISSION = "KILL DATABASE CONNECTION'"


@pytest.mark.parametrize("granted", [False, True])
def test_on_azure_sql_database_the_kill_check_is_the_database_permission(fake_server, granted):
    """Azure SQL Database (EngineEdition 5) has no server-level permissions:
    advising ALTER ANY CONNECTION there would name a grant that cannot be made."""
    fake_server.answers = {
        EDITION: "SELECT 5",
        AZURE_KILL_PERMISSION: f"SELECT {int(granted)}",
        KILL_PERMISSION: "SELECT 0",  # must not be what decides
    }
    result = _client().test_connection()
    assert result["success"] is True
    assert bool(result.get("warnings")) is (not granted)
    assert "ALTER ANY CONNECTION" not in result["message"]
    assert ("KILL DATABASE CONNECTION" in result["message"]) is (not granted)


@pytest.mark.parametrize("edition", ["SELECT 2", "SELECT 8", sqlite3.OperationalError("no SERVERPROPERTY")])
def test_elsewhere_the_kill_check_is_alter_any_connection(fake_server, edition):
    """On-premises editions, Managed Instance (8), and a server that cannot
    report its edition all use the server-level check."""
    fake_server.answers = {EDITION: edition, AZURE_KILL_PERMISSION: "SELECT 1", KILL_PERMISSION: "SELECT 0"}
    result = _client().test_connection()
    assert result.get("warnings")
    assert "ALTER ANY CONNECTION" in result["message"]


# --------------------------------------------------------------------------
# Errors BOW causes on purpose are explained, and only those
# --------------------------------------------------------------------------

def _driver_error(native_code, sqlstate="HY000", text="something went wrong"):
    """An error shaped like pyodbc's: SQLSTATE first, native code before the call name."""
    return sqlite3.OperationalError(
        sqlstate, f"[{sqlstate}] [Microsoft][ODBC Driver 18 for SQL Server][SQL Server]{text}. "
                  f"({native_code}) (SQLExecDirectW)")


LOCK_TIMEOUT = _driver_error(1222, text="Lock request time out period exceeded")
DEADLOCKED = _driver_error(1205, "40001", "Transaction (Process ID 63) was deadlocked on lock resources")


@pytest.mark.parametrize("lock_timeout", [3, 15])
def test_a_lock_timeout_is_explained_not_passed_through(fake_server, lock_timeout):
    from app.data_sources.clients.mssql_client import MssqlLockConflictError

    fake_server.answers = {"FROM orders": LOCK_TIMEOUT}
    with pytest.raises(MssqlLockConflictError) as raised:
        _client(lock_timeout_seconds=lock_timeout).execute_query("SELECT status FROM orders")
    message = str(raised.value)
    assert message.startswith("SQL Server error 1222")  # survives the 200-char cut
    assert f"{lock_timeout}s" in message
    assert "Lock request time out" not in message
    # The driver's own error is kept for whoever needs it.
    assert "(1222)" in str(raised.value.__cause__)
    assert fake_server.ran("FROM orders") == 1  # a lock timeout is not retried


def test_a_lost_deadlock_is_retried_once_and_then_succeeds(fake_server):
    calls = []

    def first_deadlocks(text):
        calls.append(text)
        return DEADLOCKED if len(calls) == 1 else "SELECT 'open' AS status"

    fake_server.answers = {"FROM orders": first_deadlocks}
    df = _client().execute_query("SELECT status FROM orders")
    assert df["status"].tolist() == ["open"]
    assert fake_server.ran("FROM orders") == 2


@pytest.mark.parametrize("deadlock", [
    DEADLOCKED,
    # Recognised by SQLSTATE alone, whatever the message says.
    sqlite3.OperationalError("40001", "[40001] Transaction was chosen as the victim"),
])
def test_a_deadlock_lost_twice_is_explained_after_one_retry(fake_server, deadlock):
    from app.data_sources.clients.mssql_client import MssqlLockConflictError

    fake_server.answers = {"FROM orders": deadlock}
    with pytest.raises(MssqlLockConflictError) as raised:
        _client().execute_query("SELECT status FROM orders")
    assert str(raised.value).startswith("SQL Server error 1205")
    assert fake_server.ran("FROM orders") == 2  # one retry, not more


@pytest.mark.parametrize("code", [3951, 3952])
def test_a_snapshot_refusal_at_query_time_is_explained(fake_server, code):
    from app.data_sources.clients.mssql_client import (
        MssqlLockConflictError, MssqlSnapshotNotAllowedError,
    )

    fake_server.answers = {"FROM other_db": _driver_error(code, text="Snapshot isolation transaction failed")}
    with pytest.raises(MssqlSnapshotNotAllowedError) as raised:
        _client(isolation_level="snapshot").execute_query("SELECT * FROM other_db.dbo.orders")
    assert not isinstance(raised.value, MssqlLockConflictError)  # it is not a lock conflict
    assert "ALLOW_SNAPSHOT_ISOLATION" in str(raised.value)


@pytest.mark.parametrize("number", [1205, 1222, 3951, 3952])
@pytest.mark.parametrize("shape", ["IN ({n})", "= ({n})", "-- ({n}) (SQLExecDirectW)\n"])
def test_numbers_in_the_query_text_are_not_read_as_error_codes(fake_server, number, shape):
    """The wrapped exception text includes the SQL that ran. A query that fails
    for its own reason must keep its own error, and must not be rerun."""
    from app.data_sources.clients.mssql_client import MssqlQueryError

    sql = f"SELECT nope FROM missing_table WHERE store_id {shape.format(n=number)}"
    with pytest.raises(Exception) as raised:
        _client().execute_query(sql)
    assert not isinstance(raised.value, MssqlQueryError)
    assert "missing_table" in str(raised.value)
    assert fake_server.ran("missing_table") == 1


def test_other_server_errors_pass_through_unchanged(fake_server):
    from app.data_sources.clients.mssql_client import MssqlQueryError

    fake_server.answers = {"FROM orders": _driver_error(208, "42S02", "Invalid object name 'orders'")}
    with pytest.raises(Exception) as raised:
        _client().execute_query("SELECT status FROM orders")
    assert not isinstance(raised.value, MssqlQueryError)
    assert "Invalid object name" in str(raised.value)


# --------------------------------------------------------------------------
# A KILL that fails is visible
# --------------------------------------------------------------------------

class _SideConn:
    def __init__(self, error):
        self._error = error

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def exec_driver_sql(self, statement):
        raise self._error


class _SideEngine:
    def __init__(self, error):
        self._error = error

    def execution_options(self, **kw):
        return self

    def connect(self):
        return _SideConn(self._error)

    def dispose(self):
        pass


class _MssqlConn:
    class engine:
        class dialect:
            name = "mssql"
        url = "mssql+pyodbc:///?odbc_connect=x"

    class connection:
        dbapi_connection = object()

    def __init__(self):
        self.info = {"bow_spid": 61}


@pytest.mark.parametrize("error,permission", [
    (Exception("[42000] [Microsoft][ODBC Driver 18 for SQL Server][SQL Server]User does not "
               "have permission to use the KILL statement. (6102) (SQLExecDirectW)"), True),
    (ConnectionError("Login timeout expired"), False),
])
def test_a_failed_kill_is_reported_with_its_reason(monkeypatch, error, permission):
    monkeypatch.setattr(sqlalchemy, "create_engine", lambda *a, **k: _SideEngine(error))
    client = object()
    with qc.track(client, _MssqlConn()):
        outcome = qc.cancel_thread(client, threading.get_ident())
    assert qc.cancellation_failed(outcome)
    assert ("ALTER ANY CONNECTION" in outcome) is permission


class _MysqlRaw:
    def thread_id(self):
        return 4242


class _MysqlConn:
    class engine:
        class dialect:
            name = "mysql"
        url = "mysql+pymysql://u:p@h/d"

    class connection:
        dbapi_connection = _MysqlRaw()

    info = {}


@pytest.mark.parametrize("conn,error,own,other", [
    (_MssqlConn(), Exception("User does not have permission to use the KILL statement. (6102) (SQLExecDirectW)"),
     "ALTER ANY CONNECTION", "CONNECTION_ADMIN"),
    (_MysqlConn(), Exception("(1095, 'You are not owner of thread 4242')"),
     "CONNECTION_ADMIN", "ALTER ANY CONNECTION"),
])
def test_a_refused_kill_names_only_its_own_engines_permission(monkeypatch, conn, error, own, other):
    monkeypatch.setattr(sqlalchemy, "create_engine", lambda *a, **k: _SideEngine(error))
    client = object()
    with qc.track(client, conn):
        outcome = qc.cancel_thread(client, threading.get_ident())
    assert qc.cancellation_failed(outcome)
    assert own in outcome and other not in outcome


@pytest.mark.parametrize("outcome,still_running", [
    ("failed: permission denied (KILL needs ALTER ANY CONNECTION)", True),
    ("failed: OperationalError", True),
    ("session killed (spid 61)", False),
    ("cancelled (postgresql cancel)", False),
    ("unsupported: snowflake", False),
    ("not_running", False),
    (None, False),
])
def test_a_timeout_error_says_whether_the_query_is_still_running(outcome, still_running):
    from app.ai.code_execution.code_execution import QueryTimeoutError

    err = QueryTimeoutError(60, sql="SELECT 1", cancellation=outcome)
    assert err.source_still_running is still_running
    assert ("could not stop" in str(err)) is still_running


@pytest.mark.parametrize("outcome,still_running", [
    ("failed: permission denied (KILL needs ALTER ANY CONNECTION)", True),
    ("session killed (spid 61)", False),
])
def test_a_timed_out_query_records_whether_it_is_still_running(monkeypatch, outcome, still_running):
    from app.ai.code_execution.code_execution import (
        QueryCapturingClientWrapper, QueryTimeoutError,
    )

    class SlowClient:
        def execute_query(self, sql):
            threading.Event().wait(30)

    monkeypatch.setattr(qc, "cancel_thread", lambda client, ident: outcome)
    timings = []
    w = QueryCapturingClientWrapper(SlowClient(), [], timings, query_timeout_seconds=1)
    with pytest.raises(QueryTimeoutError):
        w.execute_query("SELECT * FROM big_table")

    (timing,) = timings
    assert timing["source_still_running"] is still_running
    assert timing["cancellation"] == outcome
    # Both what the planner reads and the clipped trace field name the fix.
    assert ("ALTER ANY CONNECTION" in timing["error_message"]) is still_running
    assert ("ALTER ANY CONNECTION" in timing["error"]) is still_running


@pytest.mark.parametrize("can_kill", [False, True])
def test_pre_save_connection_test_shows_the_kill_warning(fake_server, can_kill):
    """The service replaces the client's success message with its own table
    count; the warning has to survive that."""
    import asyncio

    from app.services.connection_service import ConnectionService

    fake_server.answers = {
        KILL_PERMISSION: f"SELECT {int(can_kill)}",
        "INFORMATION_SCHEMA": "SELECT 'dbo', 'orders', 'id', 'int', NULL, NULL",
    }
    result = asyncio.run(ConnectionService().test_connection_params(
        "MSSQL",
        {"host": "sql.example.test", "port": 1433, "database": "sales"},
        {"user": "bow_reader", "password": "pw"},
    ))
    assert result["success"] is True, result
    assert ("ALTER ANY CONNECTION" in result["message"]) is (not can_kill)
    assert bool(result.get("warnings")) is (not can_kill)
