"""A timed-out query must be stopped on the source, not just abandoned.

The live proof runs against real Postgres/MariaDB (a `pg_sleep` cancelled and
then confirmed gone from `pg_stat_activity`). These are the parts that can be
pinned without a server: the registry's bookkeeping, the per-dialect dispatch,
and — most importantly — that nothing here can raise, because a failed
cancellation must never mask the timeout the caller is actually reporting.
"""

import threading

import pytest

from app.data_sources import query_cancellation as qc


class FakeDialect:
    def __init__(self, name):
        self.name = name


class FakeEngine:
    def __init__(self, name):
        self.dialect = FakeDialect(name)
        self.url = f"{name}://u:p@h/d"


class FakeRaw:
    def __init__(self, thread_id=None, cancel_raises=False):
        self.cancelled = 0
        self._thread_id = thread_id
        self._cancel_raises = cancel_raises

    def cancel(self):
        if self._cancel_raises:
            raise RuntimeError("driver said no")
        self.cancelled += 1

    def thread_id(self):
        return self._thread_id


class FakeFairy:
    def __init__(self, raw):
        self.dbapi_connection = raw


class FakeConn:
    def __init__(self, dialect, raw=None, info=None):
        self.engine = FakeEngine(dialect)
        self.connection = FakeFairy(raw if raw is not None else FakeRaw())
        self.info = info if info is not None else {}


# --------------------------------------------------------------------------
# Registry
# --------------------------------------------------------------------------

def test_track_registers_and_always_releases():
    client = object()
    conn = FakeConn("postgresql")
    assert qc.active_count() == 0
    with qc.track(client, conn):
        assert qc.active_count() == 1
    assert qc.active_count() == 0


def test_track_releases_even_when_the_body_raises():
    client = object()
    with pytest.raises(ValueError):
        with qc.track(client, FakeConn("postgresql")):
            raise ValueError("query blew up")
    assert qc.active_count() == 0


def test_cancelling_a_finished_query_is_not_an_error():
    """The query can complete between the timeout firing and the cancel."""
    assert qc.cancel_thread(object(), threading.get_ident()) == "not_running"


def test_the_registry_is_keyed_by_thread_not_just_client():
    """One client can have several queries in flight; cancelling a timed-out
    one must not touch the others."""
    client = object()
    other_raw = FakeRaw()
    done = threading.Event()
    started = threading.Event()

    def hold():
        with qc.track(client, FakeConn("postgresql", raw=other_raw)):
            started.set()
            done.wait(5)

    t = threading.Thread(target=hold, daemon=True)
    t.start()
    started.wait(5)
    try:
        # This thread has nothing registered, even though the client does.
        assert qc.cancel_thread(client, threading.get_ident()) == "not_running"
        assert other_raw.cancelled == 0
    finally:
        done.set()
        t.join(5)


# --------------------------------------------------------------------------
# Per-dialect dispatch
# --------------------------------------------------------------------------

def _cancel_in_place(conn):
    """Register `conn` for this thread, then cancel this thread."""
    client = object()
    with qc.track(client, conn):
        return qc.cancel_thread(client, threading.get_ident())


@pytest.mark.parametrize("dialect", ["postgresql", "oracle"])
def test_drivers_with_a_native_cancel_use_it(dialect):
    raw = FakeRaw()
    outcome = _cancel_in_place(FakeConn(dialect, raw=raw))
    assert raw.cancelled == 1
    assert outcome == f"cancelled ({dialect} cancel)"


@pytest.mark.parametrize("dialect", ["mysql", "mariadb"])
def test_mysql_family_kills_the_statement_by_thread_id(dialect, monkeypatch):
    sent = {}

    def fake_kill(sa_conn, statement, success):
        sent["statement"] = statement
        return success

    monkeypatch.setattr(qc, "_kill_via_side_connection", fake_kill)
    outcome = _cancel_in_place(FakeConn(dialect, raw=FakeRaw(thread_id=4242)))
    # KILL QUERY, not KILL: the statement dies, the session lives.
    assert sent["statement"] == "KILL QUERY 4242"
    assert outcome == "statement killed (thread 4242)"


def test_mysql_without_a_thread_id_reports_unsupported():
    outcome = _cancel_in_place(FakeConn("mysql", raw=FakeRaw(thread_id=None)))
    assert outcome == "unsupported: no thread id"


def test_sql_server_kills_the_captured_spid(monkeypatch):
    sent = {}

    def fake_kill(sa_conn, statement, success):
        sent["statement"] = statement
        return success

    monkeypatch.setattr(qc, "_kill_via_side_connection", fake_kill)
    conn = FakeConn("mssql", info={"bow_spid": 77})
    outcome = _cancel_in_place(conn)
    assert sent["statement"] == "KILL 77"
    assert outcome == "session killed (spid 77)"


def test_sql_server_without_a_captured_spid_reports_unsupported():
    assert _cancel_in_place(FakeConn("mssql")) == "unsupported: no spid"


def test_unknown_dialect_reports_unsupported_rather_than_guessing():
    """Trino/Snowflake/etc. have no safe cross-thread cancel here. Reporting it
    is correct; closing the connection under a C driver is not."""
    assert _cancel_in_place(FakeConn("trino")) == "unsupported: trino"


def test_a_driver_that_raises_during_cancel_does_not_propagate():
    """The caller is in the middle of raising QueryTimeoutError — a failed
    cancel must not replace it with a driver error."""
    conn = FakeConn("postgresql", raw=FakeRaw(cancel_raises=True))
    outcome = _cancel_in_place(conn)
    assert outcome == "failed: RuntimeError"


# --------------------------------------------------------------------------
# SPID capture
# --------------------------------------------------------------------------

def test_spid_is_captured_once_per_physical_connection():
    calls = []

    class SpidConn(FakeConn):
        def execute(self, stmt):
            calls.append(str(stmt))

            class R:
                def scalar(self_inner):
                    return 99
            return R()

    conn = SpidConn("mssql")
    qc.capture_identity(conn)
    qc.capture_identity(conn)   # info persists across pool checkouts
    assert conn.info["bow_spid"] == 99
    assert len(calls) == 1


def test_spid_capture_failure_is_swallowed():
    """Losing the SPID costs cancellation, not the query."""

    class Broken(FakeConn):
        def execute(self, stmt):
            raise RuntimeError("connection is busy")

    conn = Broken("mssql")
    qc.capture_identity(conn)
    assert "bow_spid" not in conn.info


# --------------------------------------------------------------------------
# The wrapper wires it up
# --------------------------------------------------------------------------

def test_timeout_asks_the_source_to_cancel_and_records_the_outcome(monkeypatch):
    from app.ai.code_execution.code_execution import (
        QueryCapturingClientWrapper, QueryTimeoutError,
    )

    class SlowClient:
        def execute_query(self, sql):
            threading.Event().wait(30)

    seen = {}

    def fake_cancel(client, thread_ident):
        seen["client"] = client
        seen["thread"] = thread_ident
        return "cancelled (postgresql cancel)"

    monkeypatch.setattr(qc, "cancel_thread", fake_cancel)

    client = SlowClient()
    timings = []
    w = QueryCapturingClientWrapper(client, [], timings, query_timeout_seconds=1)
    with pytest.raises(QueryTimeoutError):
        w.execute_query("SELECT pg_sleep(30)")

    assert seen["client"] is client
    # The abandoned worker, not the caller — the caller is still alive.
    assert seen["thread"] != threading.get_ident()
    assert timings[0]["error_type"] == "timeout"
    assert timings[0]["cancellation"] == "cancelled (postgresql cancel)"


def test_a_successful_query_never_touches_cancellation(monkeypatch):
    from app.ai.code_execution.code_execution import QueryCapturingClientWrapper

    def boom(*a, **k):
        raise AssertionError("cancel_thread called on a healthy query")

    monkeypatch.setattr(qc, "cancel_thread", boom)

    class FastClient:
        def execute_query(self, sql):
            return [1, 2, 3]

    timings = []
    w = QueryCapturingClientWrapper(FastClient(), [], timings, query_timeout_seconds=10)
    assert w.execute_query("SELECT 1") == [1, 2, 3]
    assert "cancellation" not in timings[0]


def test_the_kill_side_connection_runs_outside_a_transaction():
    """SQL Server rejects KILL inside a user transaction (error 6115), and
    SQLAlchemy opens one on connect. Verified against SQL Server 2022: every
    cancellation failed this way until the side connection was switched to
    AUTOCOMMIT, and the failure was invisible — the timeout still surfaced, the
    query just kept running on the server."""
    import inspect

    src = inspect.getsource(qc._kill_via_side_connection)
    assert "AUTOCOMMIT" in src, (
        "the KILL side connection no longer forces autocommit; SQL Server will "
        "reject it with 'KILL command cannot be used inside user transactions'"
    )
