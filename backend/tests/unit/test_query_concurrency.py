"""The per-connection query limit is app-wide, queues bursts, and is killable.

What a connection manager is promised:

- No more than the connection's limit of queries run at once — counted across
  every worker and replica, not per process (two real processes below).
- A burst waits its turn, first come first served; only a query that cannot get
  a slot within its wait budget fails, with a terminal "connection busy" error.
- A dead worker cannot hold slots forever: its leases run out.
- Every query leaves a history row with the exact text sent and how it ended.
- A manager can kill a running or a queued query; the source is asked to stop
  the statement and the query fails with a terminal "cancelled by admin" error.
- Paths without a connection (indexing, health checks) are neither limited nor
  recorded.
"""
from __future__ import annotations

import multiprocessing
import os
import threading
import time
import uuid
from datetime import datetime, timedelta

import pandas as pd
import pytest
from sqlalchemy import insert, select, update

from app.ai.code_execution.code_execution import QueryCapturingClientWrapper, QueryTimeoutError
from app.data_sources import query_concurrency as qcc
from app.data_sources import query_executions as qe
from app.data_sources.query_concurrency import ConnectionBusyError
from app.data_sources.query_executions import QueryKilledError


class FakeClient:
    def __init__(self, **attrs):
        for k, v in attrs.items():
            setattr(self, k, v)


class FakeSettings:
    def __init__(self, value):
        self._value = value

    def get_config(self, name):
        assert name == "max_concurrent_queries_per_connection"
        return type("C", (), {"value": self._value})()


# --------------------------------------------------------------------------
# Limit resolution (no database)
# --------------------------------------------------------------------------


def test_connection_override_wins_over_the_org_default():
    client = FakeClient(_bow_connection_max_concurrent_queries=2)
    assert qcc.effective_limit(client, FakeSettings(9)) == 2


def test_org_default_applies_without_a_connection_override():
    assert qcc.effective_limit(FakeClient(), FakeSettings(6)) == 6


def test_falls_back_to_the_builtin_without_any_setting():
    assert qcc.effective_limit(FakeClient(), None) == qcc.DEFAULT_MAX_CONCURRENT_QUERIES


@pytest.mark.parametrize("bad", [0, -3, None, "4"])
def test_a_zero_does_not_mean_unlimited(bad):
    client = FakeClient(_bow_connection_max_concurrent_queries=bad)
    assert qcc.effective_limit(client, None) == qcc.DEFAULT_MAX_CONCURRENT_QUERIES


def test_broken_org_settings_do_not_break_queries():
    class Broken:
        def get_config(self, name):
            raise RuntimeError("settings unavailable")

    assert qcc.effective_limit(FakeClient(), Broken()) == qcc.DEFAULT_MAX_CONCURRENT_QUERIES


def test_busy_and_killed_errors_end_the_attempt():
    # Terminal: the code retry must not rewrite and resend the same query.
    assert ConnectionBusyError("db", 2, 3.0).terminal_execution_error is True
    assert QueryKilledError().terminal_execution_error is True


# --------------------------------------------------------------------------
# The database-backed limiter
# --------------------------------------------------------------------------


@pytest.fixture
def connection_id():
    from app.models.connection import Connection
    from app.models.organization import Organization

    qe.reset_engine()
    org_id, conn_id = str(uuid.uuid4()), str(uuid.uuid4())
    now = datetime.utcnow()
    with qe.get_engine().begin() as conn:
        conn.execute(insert(Organization.__table__).values(id=org_id, name="Org", created_at=now, updated_at=now))
        conn.execute(insert(Connection.__table__).values(
            id=conn_id, name="warehouse", type="postgresql", config={}, organization_id=org_id,
            created_at=now, updated_at=now,
        ))
    yield conn_id
    qe.reset_engine()


def _rows(connection_id):
    table = qe._table()
    with qe.get_engine().connect() as conn:
        return conn.execute(
            select(table).where(table.c.connection_id == connection_id).order_by(table.c.queued_at)
        ).mappings().all()


def _row(row_id):
    table = qe._table()
    with qe.get_engine().connect() as conn:
        return conn.execute(select(table).where(table.c.id == row_id)).mappings().one()


def _foreign_running(connection_id, *, lease_seconds=30, worker="other-host:1:x"):
    """A query held by another worker (or a dead one, with a lapsed lease)."""
    table = qe._table()
    row_id = str(uuid.uuid4())
    now = datetime.utcnow()
    with qe.get_engine().begin() as conn:
        conn.execute(insert(table).values(
            id=row_id, connection_id=connection_id, status="running", queued_at=now - timedelta(seconds=5),
            started_at=now, worker_id=worker, lease_expires_at=now + timedelta(seconds=lease_seconds),
            query_text="SELECT 1", query_truncated=0, created_at=now, updated_at=now,
        ))
    return row_id


def _request_kill(row_id):
    """What the API does: stamp the row; the owning worker's supervisor acts."""
    table = qe._table()
    with qe.get_engine().begin() as conn:
        conn.execute(update(table).where(table.c.id == row_id).values(kill_requested_at=datetime.utcnow()))


def _wait_for(predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError("condition not met in time")


def _begin(connection_id, *, limit, wait=5.0, text="SELECT 1"):
    return qe.begin(connection_id=connection_id, limit=limit, wait_seconds=wait, query_text=text, meta={})


@pytest.mark.db
def test_queries_held_by_other_workers_count_against_the_limit(connection_id):
    _foreign_running(connection_id)
    _foreign_running(connection_id)

    t0 = time.monotonic()
    with pytest.raises(ConnectionBusyError) as err:
        _begin(connection_id, limit=2, wait=1.0)
    assert time.monotonic() - t0 >= 1.0
    assert "limit 2" in str(err.value)

    busy = [r for r in _rows(connection_id) if r["worker_id"] == qe.worker_id()]
    assert [r["status"] for r in busy] == ["busy"]
    assert busy[0]["started_at"] is None


@pytest.mark.db
def test_a_dead_workers_queries_stop_holding_slots(connection_id):
    _foreign_running(connection_id, lease_seconds=-1)  # its worker stopped renewing
    _foreign_running(connection_id, lease_seconds=-1)

    handle = _begin(connection_id, limit=2, wait=1.0)
    assert _row(handle.id)["status"] == "running"
    qe.finish(handle, "success", rows=1)


@pytest.mark.db
def test_a_burst_waits_its_turn_first_come_first_served(connection_id):
    limit, n = 2, 6
    order, peak, lock = [], [0], threading.Lock()
    running = [0]

    def one(i):
        time.sleep(i * 0.05)  # arrive in a known order
        handle = _begin(connection_id, limit=limit, wait=20.0, text=f"SELECT {i}")
        with lock:
            order.append(i)
            running[0] += 1
            peak[0] = max(peak[0], running[0])
        time.sleep(0.3)
        with lock:
            running[0] -= 1
        qe.finish(handle, "success", rows=1)

    threads = [threading.Thread(target=one, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)

    assert peak[0] <= limit
    assert order == sorted(order)  # first come, first served
    rows = _rows(connection_id)
    assert [r["status"] for r in rows] == ["success"] * n
    assert sum(1 for r in rows if (r["queued_ms"] or 0) > 100) >= n - limit  # the rest really queued


def _child_burst(test_db_url, connection_id, limit, n, hold):
    os.environ["TEST_DATABASE_URL"] = test_db_url
    os.environ["TESTING"] = "true"
    from app.data_sources import query_executions as child_qe

    child_qe.reset_engine()

    def one(i):
        handle = child_qe.begin(connection_id=connection_id, limit=limit, wait_seconds=60,
                                query_text=f"SELECT {i}", meta={})
        time.sleep(hold)
        child_qe.finish(handle, "success", rows=1)

    threads = [threading.Thread(target=one, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(120)


@pytest.mark.db
def test_the_limit_holds_across_separate_processes(connection_id):
    limit = 2
    ctx = multiprocessing.get_context("spawn")
    url = os.environ["TEST_DATABASE_URL"]
    procs = [ctx.Process(target=_child_burst, args=(url, connection_id, limit, 4, 0.4)) for _ in range(2)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(180)
        assert p.exitcode == 0

    rows = _rows(connection_id)
    assert len(rows) == 8 and all(r["status"] == "success" for r in rows)
    assert len({r["worker_id"] for r in rows}) == 2
    # Peak overlap of the recorded run intervals never exceeds the limit.
    events = sorted(
        [(r["started_at"], 1) for r in rows] + [(r["ended_at"], -1) for r in rows],
        key=lambda e: (e[0], e[1]),
    )
    level = peak = 0
    for _, delta in events:
        level += delta
        peak = max(peak, level)
    assert peak <= limit


@pytest.mark.db
def test_killing_a_queued_query_stops_its_wait(connection_id):
    _foreign_running(connection_id)
    result = {}

    def waiter():
        try:
            _begin(connection_id, limit=1, wait=30.0)
        except BaseException as e:  # noqa: BLE001 - recorded for the assertion
            result["error"] = e

    t = threading.Thread(target=waiter)
    t.start()
    queued = _wait_for(lambda: [r for r in _rows(connection_id) if r["status"] == "queued"])
    t0 = time.monotonic()
    _request_kill(queued[0]["id"])
    t.join(10)

    assert isinstance(result.get("error"), QueryKilledError)
    assert time.monotonic() - t0 < 5
    row = _row(queued[0]["id"])
    assert row["status"] == "cancelled" and row["started_at"] is None


class _BlockingSource:
    """A source whose query runs until the database is told to cancel it."""

    def __init__(self, connection_id, **attrs):
        self._bow_connection_id = connection_id
        self.started = threading.Event()
        self.cancelled = threading.Event()
        for k, v in attrs.items():
            setattr(self, k, v)

    def execute_query(self, query):
        self.started.set()
        if self.cancelled.wait(timeout=30):
            raise RuntimeError("canceling statement due to user request")
        return pd.DataFrame({"x": [1]})


@pytest.mark.db
def test_killing_a_running_query_cancels_it_at_the_source(connection_id, monkeypatch):
    from app.data_sources import query_cancellation

    source = _BlockingSource(connection_id)

    def cancel_thread(client, ident):
        assert client is source and ident
        source.cancelled.set()
        return "cancelled"

    monkeypatch.setattr(query_cancellation, "cancel_thread", cancel_thread)
    wrapper = QueryCapturingClientWrapper(source, [], [], query_timeout_seconds=60, max_concurrent_queries=4)
    result = {}

    def run():
        try:
            wrapper.execute_query("SELECT pg_sleep(30)")
        except BaseException as e:  # noqa: BLE001
            result["error"] = e

    t = threading.Thread(target=run)
    t.start()
    assert source.started.wait(10)
    running = _wait_for(lambda: [r for r in _rows(connection_id) if r["status"] == "running"])
    t0 = time.monotonic()
    _request_kill(running[0]["id"])  # picked up by this process's supervisor
    t.join(15)

    assert isinstance(result.get("error"), QueryKilledError)
    assert time.monotonic() - t0 < 5
    assert source.cancelled.is_set(), "the source was not asked to cancel the statement"
    # The row closes once the source call has exited (it holds the slot until then).
    row = _wait_for(lambda: (r := _row(running[0]["id"]))["status"] == "cancelled" and r, timeout=5)
    assert row["status"] == "cancelled"
    assert row["cancel_outcome"] == "cancelled"
    assert row["query_text"] == "SELECT pg_sleep(30)"


@pytest.mark.db
def test_each_query_leaves_a_history_row_with_the_text_sent(connection_id):
    class Source:
        _bow_connection_id = connection_id

        def __init__(self):
            self.calls = 0

        def execute_query(self, query):
            self.calls += 1
            if "boom" in query:
                raise ValueError("relation does not exist")
            return pd.DataFrame({"n": [1, 2, 3]})

    wrapper = QueryCapturingClientWrapper(Source(), [], [], query_timeout_seconds=30, max_concurrent_queries=4)
    wrapper.execute_query("SELECT n FROM t WHERE region = :region", params={"region": "EU"})
    with pytest.raises(ValueError):
        wrapper.execute_query("SELECT boom")

    rows = _rows(connection_id)
    assert [r["status"] for r in rows] == ["success", "error"]
    # The rendered query that reached the source — values bound, no Python.
    assert "'EU'" in rows[0]["query_text"] and ":region" not in rows[0]["query_text"]
    assert rows[0]["rows"] == 3 and rows[0]["duration_ms"] is not None
    assert "relation does not exist" in rows[1]["error"]
    assert all(r["lease_expires_at"] is None for r in rows)  # no slot left held


@pytest.mark.db
def test_a_failure_after_the_source_returns_still_frees_the_slot(connection_id):
    """A step between the source returning and the history row closing can
    fail (here, a result whose length cannot be read): the slot is still
    given back and the row is closed."""
    class Unsized:
        def __len__(self):
            raise RuntimeError("no length")

    class Source:
        _bow_connection_id = connection_id

        def execute_query(self, query):
            return Unsized()

    wrapper = QueryCapturingClientWrapper(Source(), [], [], query_timeout_seconds=30, max_concurrent_queries=1)
    with pytest.raises(RuntimeError):
        wrapper.execute_query("SELECT 1")
    rows = _rows(connection_id)
    assert [r["status"] for r in rows] == ["error"]
    assert rows[0]["lease_expires_at"] is None
    handle = _begin(connection_id, limit=1, wait=1.0)  # the one slot is free again
    qe.finish(handle, "success")


@pytest.mark.db
def test_a_timed_out_query_is_recorded_and_frees_its_slot(connection_id, monkeypatch):
    from app.data_sources import query_cancellation

    source = _BlockingSource(connection_id)
    monkeypatch.setattr(query_cancellation, "cancel_thread", lambda c, i: (source.cancelled.set(), "cancelled")[1])
    wrapper = QueryCapturingClientWrapper(source, [], [], query_timeout_seconds=1, max_concurrent_queries=1)
    with pytest.raises(QueryTimeoutError):
        wrapper.execute_query("SELECT slow")
    # Closed once the cancelled source call exits, which frees the slot.
    row = _wait_for(lambda: (r := _rows(connection_id)[0])["status"] == "timeout" and r, timeout=5)
    assert row["lease_expires_at"] is None
    handle = _begin(connection_id, limit=1, wait=1.0)  # the slot is free again
    qe.finish(handle, "success")


@pytest.mark.db
def test_a_stopped_run_records_its_query_as_cancelled(connection_id, monkeypatch):
    from app.data_sources import query_cancellation

    source = _BlockingSource(connection_id)
    monkeypatch.setattr(query_cancellation, "cancel_thread", lambda c, i: (source.cancelled.set(), "cancelled")[1])
    wrapper = QueryCapturingClientWrapper(source, [], [], query_timeout_seconds=60, max_concurrent_queries=4)
    t = threading.Thread(target=lambda: pytest.raises(RuntimeError, wrapper.execute_query, "SELECT slow"))
    t.start()
    assert source.started.wait(10)
    wrapper.cancel_active_query()  # what the sandbox runner does on Stop
    t.join(10)
    row = _rows(connection_id)[0]
    assert row["status"] == "cancelled" and row["error"] == "The run was stopped"


@pytest.mark.db
def test_the_owning_worker_keeps_its_leases_alive(connection_id):
    handle = _begin(connection_id, limit=1)
    table = qe._table()
    soon = datetime.utcnow() + timedelta(seconds=1)
    with qe.get_engine().begin() as conn:
        conn.execute(update(table).where(table.c.id == handle.id).values(lease_expires_at=soon))
    qe.supervisor_tick(last_heartbeat=0.0)
    assert _row(handle.id)["lease_expires_at"] > soon + timedelta(seconds=10)
    qe.finish(handle, "success")


@pytest.mark.db
def test_sweeps_close_dead_rows_and_purge_only_old_history(connection_id):
    dead = _foreign_running(connection_id, lease_seconds=-(qe.STALE_GRACE_SECONDS + 5))
    alive = _foreign_running(connection_id)
    assert qe.sweep_stale() == 1
    assert _row(dead)["status"] == "stale"
    assert _row(alive)["status"] == "running"

    table = qe._table()
    old = datetime.utcnow() - timedelta(days=qe.HISTORY_RETENTION_DAYS + 1)
    with qe.get_engine().begin() as conn:
        conn.execute(update(table).where(table.c.id.in_([dead, alive])).values(created_at=old))
    assert qe.purge_history() == 1  # the live row is never purged
    assert {r["id"] for r in _rows(connection_id)} == {alive}


def test_paths_without_a_connection_id_are_neither_limited_nor_recorded(monkeypatch):
    def fail(**kwargs):
        raise AssertionError("must not touch query_executions")

    monkeypatch.setattr(qe, "begin", fail)

    class Source:
        def execute_query(self, query):
            return pd.DataFrame({"x": [1]})

    wrapper = QueryCapturingClientWrapper(Source(), [], [], query_timeout_seconds=5, max_concurrent_queries=1)
    assert len(wrapper.execute_query("SELECT 1")) == 1


@pytest.mark.db
def test_a_locked_sqlite_database_never_stalls_the_query(connection_id):
    """SQLite has one writer, and the flow running a query may hold it.

    The query path must not wait the app's 30 s busy_timeout for it (the
    usage-metering stall rule). While the lock is held the limit is kept per
    process, and the history rows are written as soon as the lock frees.
    """
    import sqlite3

    engine = qe.get_engine()
    if engine.dialect.name != "sqlite":
        pytest.skip("SQLite's single writer lock")
    raw = sqlite3.connect(engine.url.database)
    try:
        raw.execute("PRAGMA busy_timeout = 100")
        raw.execute("BEGIN IMMEDIATE")  # another writer holds the database

        start = time.monotonic()
        first = _begin(connection_id, limit=1, text="SELECT 'first'")
        assert first is not None and first.deferred
        assert time.monotonic() - start < 3.0

        # The limit still holds within this process: the second query waits
        # behind the first and gives up when its wait budget runs out.
        with pytest.raises(ConnectionBusyError):
            _begin(connection_id, limit=1, wait=0.5, text="SELECT 'second'")
        qe.finish(first, "success", rows=1)
        third = _begin(connection_id, limit=1, wait=2.0, text="SELECT 'third'")
        qe.finish(third, "success", rows=3)
        assert time.monotonic() - start < 6.0
        assert _rows(connection_id) == []  # readers still work; nothing written yet
        assert qe.flush_pending_writes() == 0  # still locked: kept, not dropped
    finally:
        raw.rollback()
        raw.close()

    # 3 inserts + 2 closing updates. The supervisor's own tick may already have
    # applied some of them once the lock cleared, so check they all landed
    # (nothing pending, every row below) rather than who wrote them.
    qe.flush_pending_writes()
    assert qe.pending_write_count() == 0
    rows = {r["query_text"]: r for r in _rows(connection_id)}
    assert rows["SELECT 'first'"]["status"] == "success"
    assert rows["SELECT 'first'"]["rows"] == 1
    assert rows["SELECT 'second'"]["status"] == "busy"
    assert rows["SELECT 'second'"]["started_at"] is None
    assert rows["SELECT 'third'"]["status"] == "success"
    assert all(r["lease_expires_at"] is None for r in rows.values())


@pytest.mark.db
def test_a_deleted_connection_runs_its_query_untracked(connection_id):
    # A client built before its connection was deleted: nothing to record the
    # row against (foreign key) and nothing left to protect.
    assert _begin(str(uuid.uuid4()), limit=1) is None


# --------------------------------------------------------------------------
# Review follow-ups
# --------------------------------------------------------------------------


def test_batch_sources_wait_their_whole_query_timeout_for_a_slot(monkeypatch):
    # A dashboard refresh or scheduled report sending a burst must queue and
    # finish, as it did before the limit was app-wide; only someone watching
    # (chat, a step run, a viewer refresh) gets the short cap.
    monkeypatch.setenv("BOW_QUERY_SLOT_WAIT_SECONDS", "60")
    assert qe.slot_wait_seconds(300, "agent") == 60
    assert qe.slot_wait_seconds(300, "step_run") == 60
    assert qe.slot_wait_seconds(30, "agent") == 30  # never above the query timeout
    assert qe.slot_wait_seconds(300, "dashboard_refresh.scheduled") == 300
    assert qe.slot_wait_seconds(300, "report_refresh") == 300
    assert qe.slot_wait_seconds(300, None) == 300
    assert qe.slot_wait_seconds(None, "dashboard_refresh.manual") == 60


@pytest.mark.db
def test_a_stopped_run_gives_up_its_place_in_line_without_starting(connection_id):
    """A user Stop while the query waits for a slot: the statement never starts."""
    _foreign_running(connection_id)  # the only slot is taken

    started = threading.Event()

    class Source:
        _bow_connection_id = connection_id

        def execute_query(self, query):
            started.set()
            return pd.DataFrame({"x": [1]})

    wrapper = QueryCapturingClientWrapper(Source(), [], [], query_timeout_seconds=60, max_concurrent_queries=1)
    outcome = {}

    def run():
        try:
            wrapper.execute_query("SELECT 'waits'")
        except BaseException as e:  # noqa: BLE001 - asserted below
            outcome["error"] = e

    t = threading.Thread(target=run, daemon=True)
    t.start()
    _wait_for(lambda: any(r["status"] == "queued" for r in _rows(connection_id)))
    stop_at = time.monotonic()
    wrapper.cancel_active_query()  # what the sandbox runner does on Stop
    t.join(5)

    assert not t.is_alive() and time.monotonic() - stop_at < 3.0
    assert isinstance(outcome.get("error"), qe.QueryRunStoppedError)
    assert not started.is_set()  # nothing reached the source
    [row] = [r for r in _rows(connection_id) if r["query_text"] == "SELECT 'waits'"]
    assert row["status"] == "cancelled"
    assert row["started_at"] is None
    assert row["error"] == "The run was stopped"
    assert row["cancel_outcome"] == "not_started"


@pytest.mark.db
def test_the_busy_error_counts_the_queue_ahead_not_only_running_queries(connection_id):
    # Limit 2: one running, one queued earlier. "1 running" alone reads as a
    # free slot; the error must say what this query was actually behind.
    _foreign_running(connection_id)
    table = qe._table()
    now = datetime.utcnow()
    with qe.get_engine().begin() as conn:
        conn.execute(insert(table).values(
            id=str(uuid.uuid4()), connection_id=connection_id, status="queued",
            queued_at=now - timedelta(seconds=3), worker_id="other-host:1:x",
            lease_expires_at=now + timedelta(seconds=30), query_text="SELECT 'ahead'",
            query_truncated=0, created_at=now, updated_at=now,
        ))
    with pytest.raises(ConnectionBusyError) as err:
        _begin(connection_id, limit=2, wait=0.5)
    assert err.value.running == 1 and err.value.queued_ahead == 1
    assert "1 running, 1 queued ahead of this one, limit 2" in str(err.value)


@pytest.mark.db
def test_an_open_transaction_on_the_connection_row_does_not_hold_up_queries(connection_id):
    """Postgres: slot decisions take an advisory lock, not the connection's row.

    An indexing run (or any writer) holding the row — an uncommitted update, or
    the key-share lock an FK insert takes — must not stall every query.
    """
    engine = qe.get_engine()
    if engine.dialect.name != "postgresql":
        pytest.skip("Postgres row locks")
    from sqlalchemy import text

    holder = engine.connect()
    tx = holder.begin()
    try:
        holder.execute(text("UPDATE connections SET name = name WHERE id = :id"), {"id": connection_id})
        start = time.monotonic()
        handle = _begin(connection_id, limit=1, wait=5.0)
        assert time.monotonic() - start < 2.0
        qe.finish(handle, "success", rows=1)
    finally:
        tx.rollback()
        holder.close()


# --------------------------------------------------------------------------
# Review round 2
# --------------------------------------------------------------------------


@pytest.mark.db
def test_a_killed_query_keeps_its_slot_until_the_source_call_exits(connection_id, monkeypatch):
    """Kill returns at once, but a source call the cancel did not reach still
    occupies a connection at the source: its slot is held until it exits."""
    from app.data_sources import query_cancellation

    release = threading.Event()
    entered = threading.Event()

    class Stubborn:
        _bow_connection_id = connection_id

        def execute_query(self, query):
            entered.set()
            release.wait(30)  # ignores the cancel
            return pd.DataFrame({"x": [1]})

    monkeypatch.setattr(query_cancellation, "cancel_thread", lambda client, ident: "not_running")
    wrapper = QueryCapturingClientWrapper(Stubborn(), [], [], query_timeout_seconds=60, max_concurrent_queries=1)
    outcome = {}

    def run():
        try:
            wrapper.execute_query("SELECT 'stubborn'")
        except BaseException as e:  # noqa: BLE001 - asserted below
            outcome["error"] = e

    t = threading.Thread(target=run, daemon=True)
    t.start()
    assert entered.wait(10)
    [row] = _rows(connection_id)
    qe.request_kill_local(row["id"])
    t.join(5)
    assert isinstance(outcome.get("error"), QueryKilledError)  # the caller is free

    try:
        with pytest.raises(ConnectionBusyError):
            _begin(connection_id, limit=1, wait=0.5)  # the slot is still held
        assert _row(row["id"])["status"] == "running"
    finally:
        release.set()
    _wait_for(lambda: _row(row["id"])["status"] == "cancelled")
    handle = _begin(connection_id, limit=1, wait=2.0)  # now it is free
    qe.finish(handle, "success")


@pytest.mark.db
def test_concurrent_flushes_apply_each_deferred_write_once():
    """The supervisor's tick and another caller can flush at the same moment.
    Each deferred write must still run once: a second run of an insert fails
    on the primary key and is logged as a dropped write."""
    calls = []

    def _op(name):
        def op(conn):
            calls.append(name)
            time.sleep(0.3)  # both flushers are inside flush_pending_writes now
        return op

    qe.flush_pending_writes()
    qe._defer_write(_op("first"))
    qe._defer_write(_op("second"))
    flushers = [threading.Thread(target=qe.flush_pending_writes) for _ in range(2)]
    for t in flushers:
        t.start()
    for t in flushers:
        t.join(10)
    assert calls == ["first", "second"]
    assert qe.pending_write_count() == 0


@pytest.mark.db
def test_queries_admitted_while_sqlite_was_locked_count_once_the_lock_clears(connection_id):
    """The first query ran while the writer lock was held (its row not yet
    written). Once the lock clears, a database claim must still see it."""
    import sqlite3

    engine = qe.get_engine()
    if engine.dialect.name != "sqlite":
        pytest.skip("SQLite's single writer lock")
    raw = sqlite3.connect(engine.url.database)
    raw.execute("PRAGMA busy_timeout = 100")
    raw.execute("BEGIN IMMEDIATE")
    try:
        first = _begin(connection_id, limit=1, text="SELECT 'first'")
        assert first.deferred
    finally:
        raw.rollback()
        raw.close()
    # Lock released; the supervisor may or may not have written the row yet.
    with pytest.raises(ConnectionBusyError):
        _begin(connection_id, limit=1, wait=0.5, text="SELECT 'second'")
    qe.finish(first, "success")
    qe.flush_pending_writes()
    handle = _begin(connection_id, limit=1, wait=2.0, text="SELECT 'third'")
    qe.finish(handle, "success")


@pytest.mark.db
def test_a_stop_that_lands_while_the_slot_frees_never_reaches_the_source(connection_id, monkeypatch):
    blocker = _foreign_running(connection_id)
    called = threading.Event()

    class Source:
        _bow_connection_id = connection_id

        def execute_query(self, query):
            called.set()
            return pd.DataFrame({"x": [1]})

    wrapper = QueryCapturingClientWrapper(Source(), [], [], query_timeout_seconds=60, max_concurrent_queries=1)
    real_attempt = qe._attempt_locked
    first = {"done": False}

    def stop_during_claim(engine, handle, timeout_seconds=None):
        if not first["done"]:
            first["done"] = True
            # The user presses Stop and the occupied slot frees, both just
            # before this claim: the claim succeeds.
            wrapper._run_aborted = True
            table = qe._table()
            with qe.get_engine().begin() as conn:
                conn.execute(update(table).where(table.c.id == blocker).values(status="success", lease_expires_at=None))
        return real_attempt(engine, handle, timeout_seconds)

    monkeypatch.setattr(qe, "_attempt_locked", stop_during_claim)
    with pytest.raises(qe.QueryRunStoppedError):
        wrapper.execute_query("SELECT 'stopped'")
    assert first["done"] and not called.is_set()
    [row] = [r for r in _rows(connection_id) if r["query_text"] == "SELECT 'stopped'"]
    assert row["status"] == "cancelled" and row["error"] == "The run was stopped"


@pytest.mark.db
def test_a_held_slot_lock_never_stretches_the_wait_budget(connection_id):
    """Postgres: the slot wait budget covers lock waits too."""
    engine = qe.get_engine()
    if engine.dialect.name != "postgresql":
        pytest.skip("Postgres advisory lock")
    from sqlalchemy import text

    holder = engine.connect()
    try:
        holder.execute(text("SELECT pg_advisory_lock(hashtextextended(:k, 0))"),
                       {"k": f"bow_query_slot:{connection_id}"})
        holder.commit()
        start = time.monotonic()
        with pytest.raises(ConnectionBusyError):
            _begin(connection_id, limit=4, wait=1.0)
        assert time.monotonic() - start < 2.0
    finally:
        holder.execute(text("SELECT pg_advisory_unlock_all()"))
        holder.commit()
        holder.close()


def test_runs_nobody_is_watching_wait_like_batch_jobs(monkeypatch):
    from types import SimpleNamespace

    from app.ai.agent_v2 import AgentV2

    monkeypatch.setenv("BOW_QUERY_SLOT_WAIT_SECONDS", "60")
    assert qe.slot_wait_seconds(300, "agent", True) == 60
    assert qe.slot_wait_seconds(300, "agent", None) == 60
    assert qe.slot_wait_seconds(300, "agent", False) == 300

    def watched(**head):
        fields = {"trigger_source": None, "webhook_id": None, "scheduled_prompt_id": None, **head}
        agent = SimpleNamespace(is_eval_run=False, head_completion=SimpleNamespace(**fields))
        return AgentV2._is_watched_run(agent)

    assert watched() is True
    assert watched(scheduled_prompt_id="sp-1") is False
    assert watched(webhook_id="wh-1") is False
    assert watched(trigger_source="sub_agent") is False
    assert AgentV2._is_watched_run(SimpleNamespace(is_eval_run=True, head_completion=None)) is False


def test_a_database_stamped_by_an_earlier_queryexec01_gets_the_killable_column(tmp_path):
    """A database that ran an earlier build of queryexec01 is stamped with it,
    so that revision never runs again: queryexec02 adds the missing column."""
    import importlib.util

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import create_engine, inspect, text

    path = os.path.join(os.path.dirname(__file__), "..", "..", "alembic", "versions",
                        "queryexec02_query_executions_killable.py")
    spec = importlib.util.spec_from_file_location("queryexec02", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    engine = create_engine(f"sqlite:///{tmp_path / 'old.db'}")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE query_executions (id VARCHAR(36) PRIMARY KEY, connection_id VARCHAR(36))"))
        conn.execute(text("INSERT INTO query_executions (id, connection_id) VALUES ('a', 'b')"))
        with Operations.context(MigrationContext.configure(conn)):
            module.upgrade()
            module.upgrade()  # still idempotent
    with engine.connect() as conn:
        assert "killable" in {c["name"] for c in inspect(conn).get_columns("query_executions")}
        assert conn.execute(text("SELECT killable FROM query_executions")).scalar() == 1


def test_existing_organizations_move_from_the_old_default_to_12(tmp_path):
    """Organization settings snapshot each feature's value, and that value wins
    over the schema default: queryexec03 moves the old default (4) to 12 and
    leaves any other admin-chosen value alone."""
    import importlib.util
    import json

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import create_engine, text

    path = os.path.join(os.path.dirname(__file__), "..", "..", "alembic", "versions",
                        "queryexec03_default_concurrent_queries_12.py")
    spec = importlib.util.spec_from_file_location("queryexec03", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    def feature(value):
        return {"value": value, "name": "Concurrent queries per connection", "description": "d"}

    engine = create_engine(f"sqlite:///{tmp_path / 'orgs.db'}")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE organization_settings (id VARCHAR(36) PRIMARY KEY, config JSON)"))
        for row_id, config in (
            ("default", {"max_concurrent_queries_per_connection": feature(4)}),
            ("chosen", {"max_concurrent_queries_per_connection": feature(2)}),
            ("unset", {"other": 1}),
        ):
            conn.execute(text("INSERT INTO organization_settings VALUES (:i, :c)"),
                         {"i": row_id, "c": json.dumps(config)})
        with Operations.context(MigrationContext.configure(conn)):
            module.upgrade()
            module.upgrade()  # idempotent

    with engine.connect() as conn:
        rows = {r[0]: json.loads(r[1]) for r in conn.execute(text("SELECT id, config FROM organization_settings"))}
    assert rows["default"]["max_concurrent_queries_per_connection"]["value"] == 12
    assert rows["chosen"]["max_concurrent_queries_per_connection"]["value"] == 2
    assert rows["unset"] == {"other": 1}
    assert qcc.DEFAULT_MAX_CONCURRENT_QUERIES == 12
