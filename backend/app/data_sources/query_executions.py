"""Every query BOW sends to a connection: recorded, limited app-wide, killable.

One ``query_executions`` row per query (``app/models/query_execution.py``). The
row is written by the query wrapper (``QueryCapturingClientWrapper`` in
``app/ai/code_execution/code_execution.py``) on the thread that runs the query,
through a small synchronous engine of its own — query threads are plain
threads, not event-loop tasks, and must not borrow the request's session.

**The limit is app-wide.** A query may run only while fewer than the
connection's ``max_concurrent_queries`` live rows are ``running`` or queued
ahead of it (so a burst is served first come, first served). Live means the
row's lease has not expired. The count and the switch to ``running`` happen in
one transaction holding a lock on the connection's row (``SELECT … FOR UPDATE``
on Postgres; ``BEGIN IMMEDIATE`` on SQLite, which has a single writer anyway),
so two workers cannot both take the last slot. Because the count is read from
the shared database, every worker and every replica sees the same number —
the per-process semaphore this replaces admitted N × the limit with N
processes.

**Leases make a dead worker harmless.** A per-process supervisor thread renews
the lease on that process's queued and running rows every few seconds. If the
process dies, its leases run out, its rows stop counting against the limit,
and they read as ``stale`` (a leader job later stamps them so).

**Kill crosses workers through the database.** A manager's kill sets
``kill_requested_at`` on the row. The supervisor of the worker that owns the
row sees it on its next tick (about a second) and sets the query's kill event;
the wrapper stops waiting, asks the source to cancel the statement
(``query_cancellation``) and raises ``QueryKilledError``. A queued query that
is killed simply stops waiting. The API also signals this process's registry
directly, so a kill handled by the owning worker is immediate.

**SQLite never stalls a query on its writer lock.** SQLite has one writer, and
the flow running a query can hold it (the rule usage metering already
follows). The slot claim waits at most ``SQLITE_BUSY_TIMEOUT_MS`` for it. If
another writer still holds it, the query is admitted against this process's
own running count at the same limit, and its row writes are queued for the
supervisor, which records them once the lock frees. Postgres has no such
fallback: a slot that cannot be recorded fails the query.

**Attribution** comes from the query tag (report, run, step, query, entity,
user, source) plus :func:`attribution` below, which the tool runner and the
code executor use to name the tool call and the attempt numbers.

All SQL goes through SQLAlchemy Core on the model's table so timestamps are
bound with the column type (on SQLite they are compared as text, and a raw
datetime parameter would not match the ORM's stored format).
"""
from __future__ import annotations

import atexit
import contextlib
import json
import logging
import os
import socket
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import and_, case, column, delete, event, func, insert, or_, select, table, update
from sqlalchemy.exc import OperationalError

logger = logging.getLogger(__name__)

# A queued or running row whose lease is older than this belongs to a worker
# that stopped renewing it — it no longer holds a slot.
LEASE_SECONDS = 30
# How often the owning worker renews its leases (well inside LEASE_SECONDS).
HEARTBEAT_SECONDS = 5
# How often the supervisor looks for kill requests on this worker's rows.
SUPERVISOR_TICK_SECONDS = 1.0
# How long a query waits for a free slot before failing with
# ConnectionBusyError (never longer than its own query timeout).
DEFAULT_SLOT_WAIT_SECONDS = 60
# Query text stored per row. A generated query is rarely near this; the cap
# keeps a pathological IN-list from bloating the table.
MAX_QUERY_TEXT_CHARS = 100_000
# Finished rows older than this are deleted by the daily purge job.
HISTORY_RETENTION_DAYS = 90
# A lease must be this far past expiry before the sweep stamps the row stale,
# so a worker that is merely slow to renew is not declared dead.
STALE_GRACE_SECONDS = 60
STALE_ERROR = "The worker running this query stopped responding"
# SQLite only: how long one bookkeeping write waits for the database's single
# writer lock. BOW's own flows can hold that lock while a query runs, so the
# query path must never wait out the app's 30 s busy_timeout (the same rule
# usage metering follows); see ``begin`` for what happens when it is held.
SQLITE_BUSY_TIMEOUT_MS = 1000
# Row writes deferred while SQLite was locked, flushed by the supervisor. The
# cap only matters if the database stays locked for a very long time.
MAX_PENDING_WRITES = 10_000

LIVE_STATUSES = ("queued", "running")
FINAL_STATUSES = ("success", "error", "timeout", "busy", "cancelled", "stale")


def slot_wait_seconds(query_timeout_seconds: float | None) -> float:
    """Wait budget for a free slot: the configured cap, never above the query timeout."""
    try:
        configured = float(os.environ.get("BOW_QUERY_SLOT_WAIT_SECONDS") or DEFAULT_SLOT_WAIT_SECONDS)
    except ValueError:
        configured = DEFAULT_SLOT_WAIT_SECONDS
    configured = max(1.0, configured)
    if query_timeout_seconds and query_timeout_seconds > 0:
        return min(configured, float(query_timeout_seconds))
    return configured


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ConnectionBusyError(Exception):
    """No slot opened on the connection within the wait budget.

    Terminal for the code attempt: the code was not the problem, so asking the
    model to rewrite it (code retry) would only wait in line again. The message
    says how busy the connection is and whether the queries holding it are this
    run's own, so the planner can tell the user instead of retrying.
    """

    terminal_execution_error = True

    def __init__(
        self,
        connection_name: str | None,
        limit: int,
        waited: float,
        running: int | None = None,
        own_running: int | None = None,
    ):
        target = connection_name or "this connection"
        if own_running:
            advice = (
                f"{own_running} of them belong to this run: run fewer queries at once — "
                "combine them into one query, or run them one after another."
            )
        else:
            advice = (
                "They belong to other users or jobs, so rewriting the query will not help: "
                "tell the user the connection is busy and to try again shortly. Do not retry right away."
            )
        running_text = f"{running} running, " if running is not None else ""
        super().__init__(
            f"Connection busy: {target} is already running its maximum of {limit} concurrent "
            f"queries ({running_text}limit {limit}). Waited {waited:.0f}s for a free slot. {advice}"
        )
        self.connection_name = connection_name
        self.limit = limit
        self.waited = waited
        self.running = running
        self.own_running = own_running


class QueryKilledError(Exception):
    """A connection manager cancelled this query from the connection's Queries view.

    Terminal: neither the code retry nor the tool retry may send it again.
    """

    terminal_execution_error = True

    def __init__(self, cancellation: str | None = None):
        super().__init__(
            "Query cancelled by a connection admin. Do not run this query again; "
            "tell the user it was cancelled by an admin of the connection."
        )
        self.cancellation = cancellation


# ---------------------------------------------------------------------------
# Attribution: tool call, attempt numbers, live progress
# ---------------------------------------------------------------------------

_attribution: ContextVar[dict[str, Any] | None] = ContextVar("bow_query_attribution", default=None)
_ATTRIBUTION_KEYS = ("tool_execution_id", "tool_attempt", "code_attempt", "progress_sink")


@contextlib.contextmanager
def attribution(**fields: Any) -> Iterator[None]:
    """Name the tool call / attempt for queries issued inside the block.

    Values set by an outer block are kept unless overridden. The wrapper
    snapshots this when it is built, on the thread that still has the run's
    context (the query itself runs on threads that inherit none).
    """
    token = set_attribution(**fields)
    try:
        yield
    finally:
        _attribution.reset(token)


def set_attribution(**fields: Any):
    current = dict(_attribution.get() or {})
    for key, value in fields.items():
        if key in _ATTRIBUTION_KEYS and value is not None:
            current[key] = value
    return _attribution.set(current)


def reset_attribution(token) -> None:
    _attribution.reset(token)


def current_attribution() -> dict[str, Any]:
    return dict(_attribution.get() or {})


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

_engine_lock = threading.Lock()
_engine_state: dict[str, Any] = {}


def _sqlite_begin_immediate(engine) -> None:
    """Let slot transactions take SQLite's write lock up front.

    Without it the count-then-claim in ``_try_acquire`` could interleave with
    another writer between the read and the write (a deferred transaction only
    takes the write lock at its first write). SQLAlchemy's documented recipe:
    hand transaction control to us, then BEGIN ourselves. Only connections
    opened by :func:`_slot_transaction` say IMMEDIATE; reads and single-row
    updates stay deferred so they do not compete for the lock needlessly.
    """
    from app.settings.database import _set_sqlite_pragmas

    @event.listens_for(engine, "connect")
    def _connect(dbapi_connection, connection_record):  # pragma: no cover - driver hook
        _set_sqlite_pragmas(dbapi_connection, connection_record)
        dbapi_connection.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MS}")
        dbapi_connection.isolation_level = None

    @event.listens_for(engine, "begin")
    def _begin(conn):  # pragma: no cover - driver hook
        immediate = conn.get_execution_options().get("bow_slot_lock")
        conn.exec_driver_sql("BEGIN IMMEDIATE" if immediate else "BEGIN")


@contextlib.contextmanager
def _slot_transaction(engine):
    """A transaction that may count and claim slots (see ``_lock_connection``)."""
    with engine.connect() as conn:
        conn.execution_options(bow_slot_lock=True)
        with conn.begin():
            yield conn


def get_engine():
    """The tracker's synchronous engine (one per process, built on first use)."""
    pid = os.getpid()
    with _engine_lock:
        if _engine_state.get("pid") == pid and _engine_state.get("engine") is not None:
            return _engine_state["engine"]
        from app.settings.config import settings

        if settings.TESTING:
            # Tests swap the database under a stable URL (a fresh clone per
            # test): a pooled connection would keep reading the old file.
            from sqlalchemy import create_engine
            from sqlalchemy.pool import NullPool

            from app.settings.database import _get_test_database_url

            url = _get_test_database_url().replace("postgres://", "postgresql://")
            url = url.replace("sqlite+aiosqlite:", "sqlite:").replace("postgresql+asyncpg://", "postgresql://")
            engine = create_engine(url, poolclass=NullPool)
        else:
            from app.settings.database import create_database_engine

            engine = create_database_engine()
        if engine.dialect.name == "sqlite":
            _sqlite_begin_immediate(engine)
        _engine_state.update(pid=pid, engine=engine)
        return engine


def _is_sqlite() -> bool:
    return get_engine().dialect.name == "sqlite"


def _is_sqlite_lock_error(exc: BaseException) -> bool:
    if not isinstance(exc, OperationalError):
        return False
    message = str(exc).lower()
    return "database is locked" in message or "database table is locked" in message


def reset_engine() -> None:
    """Test helper: drop the cached engine (e.g. after TEST_DATABASE_URL changes)."""
    with _engine_lock:
        engine = _engine_state.pop("engine", None)
        _engine_state.pop("pid", None)
    if engine is not None:
        engine.dispose()


def worker_id() -> str:
    """This process's identity on the rows it owns (host:pid:random)."""
    pid = os.getpid()
    with _engine_lock:
        cached = _engine_state.get("worker_id")
        if cached and cached[0] == pid:
            return cached[1]
        value = f"{socket.gethostname()[:80]}:{pid}:{uuid.uuid4().hex[:8]}"
        _engine_state["worker_id"] = (pid, value)
        return value


def _now() -> datetime:
    return datetime.utcnow()


_connections = table("connections", column("id"), column("organization_id"))


def _table():
    from app.models.query_execution import QueryExecution

    return QueryExecution.__table__


# ---------------------------------------------------------------------------
# Per-process registry of this worker's live rows + supervisor thread
# ---------------------------------------------------------------------------


@dataclass
class QueryHandle:
    id: str
    connection_id: str
    limit: int
    queued_at: datetime
    queued_monotonic: float
    started_monotonic: float | None = None
    state: str = "queued"
    kill_event: threading.Event = field(default_factory=threading.Event)
    finished: bool = False
    # SQLite only: admitted while the database was locked, so the row's
    # writes are queued for the supervisor instead of written in line.
    deferred: bool = False

    def request_kill(self) -> None:
        self.kill_event.set()
        with _slot_changed:
            _slot_changed.notify_all()

    @property
    def killed(self) -> bool:
        return self.kill_event.is_set()


_registry: dict[str, QueryHandle] = {}
_registry_lock = threading.Lock()
# Wakes queries waiting for a slot on this worker when one of ours frees up,
# so a same-worker hand-off does not wait out a poll interval.
_slot_changed = threading.Condition()
_supervisor: dict[str, Any] = {}


def _register(handle: QueryHandle) -> None:
    with _registry_lock:
        _registry[handle.id] = handle
    _ensure_supervisor()


def _unregister(handle: QueryHandle) -> None:
    with _registry_lock:
        _registry.pop(handle.id, None)


def live_handles() -> dict[str, QueryHandle]:
    with _registry_lock:
        return dict(_registry)


def request_kill_local(query_execution_id: str) -> bool:
    """Signal a query owned by THIS process right away (same-worker fast path)."""
    with _registry_lock:
        handle = _registry.get(str(query_execution_id))
    if handle is None:
        return False
    handle.request_kill()
    return True


# Row writes that could not get SQLite's writer lock, in order. Each entry is
# a function taking a connection; the supervisor applies them oldest first.
_pending_writes: list[Callable[[Any], None]] = []
_pending_lock = threading.Lock()


def _defer_write(op: Callable[[Any], None]) -> None:
    with _pending_lock:
        if len(_pending_writes) >= MAX_PENDING_WRITES:
            _pending_writes.pop(0)
            logger.warning("Dropping a deferred query history write: the database stayed locked")
        _pending_writes.append(op)
    _ensure_supervisor()


def pending_write_count() -> int:
    with _pending_lock:
        return len(_pending_writes)


def flush_pending_writes() -> int:
    """Apply deferred row writes in order; stop at the first locked attempt.

    Returns how many were written. Public for tests.
    """
    written = 0
    while True:
        with _pending_lock:
            if not _pending_writes:
                return written
            op = _pending_writes[0]
        try:
            with get_engine().begin() as conn:
                op(conn)
        except OperationalError as exc:
            if _is_sqlite_lock_error(exc):
                return written
            logger.warning("Dropping a deferred query history write", exc_info=True)
        except Exception:
            logger.warning("Dropping a deferred query history write", exc_info=True)
        with _pending_lock:
            if _pending_writes and _pending_writes[0] is op:
                _pending_writes.pop(0)
        written += 1


def _flush_at_exit() -> None:  # pragma: no cover - interpreter shutdown
    with contextlib.suppress(Exception):
        flush_pending_writes()


def _ensure_supervisor() -> None:
    pid = os.getpid()
    with _registry_lock:
        thread = _supervisor.get("thread")
        if thread is not None and thread.is_alive() and _supervisor.get("pid") == pid:
            return
        t = threading.Thread(target=_supervise, name="bow_query_supervisor", daemon=True)
        if _supervisor.get("atexit_pid") != pid:
            atexit.register(_flush_at_exit)
            _supervisor["atexit_pid"] = pid
        _supervisor.update(thread=t, pid=pid)
        t.start()


def supervisor_tick(last_heartbeat: float = 0.0) -> float:
    """One supervisor pass: renew leases (if due) and deliver kill requests.

    Returns the monotonic time of the last heartbeat. Public for tests.
    """
    if pending_write_count():
        flush_pending_writes()
    handles = live_handles()
    if not handles:
        return last_heartbeat
    ids = list(handles)
    qe = _table()
    with get_engine().begin() as conn:
        now = _now()
        if time.monotonic() - last_heartbeat >= HEARTBEAT_SECONDS:
            conn.execute(
                update(qe)
                .where(qe.c.id.in_(ids), qe.c.status.in_(LIVE_STATUSES))
                .values(lease_expires_at=now + timedelta(seconds=LEASE_SECONDS))
            )
            last_heartbeat = time.monotonic()
        killed = conn.execute(
            select(qe.c.id).where(qe.c.id.in_(ids), qe.c.kill_requested_at.is_not(None))
        ).scalars().all()
    for row_id in killed:
        handle = handles.get(str(row_id))
        if handle is not None and not handle.killed:
            logger.info("Kill requested for query %s on connection %s", row_id, handle.connection_id)
            handle.request_kill()
    return last_heartbeat


def _supervise() -> None:
    last_heartbeat = 0.0
    while True:
        time.sleep(SUPERVISOR_TICK_SECONDS)
        try:
            last_heartbeat = supervisor_tick(last_heartbeat)
        except Exception:
            # Never let the supervisor die: the next tick retries. A missed
            # heartbeat only matters after LEASE_SECONDS of consecutive failures.
            logger.warning("Query supervisor tick failed", exc_info=True)


# ---------------------------------------------------------------------------
# Slots
# ---------------------------------------------------------------------------


def _lock_connection(conn, connection_id: str) -> str | None:
    """Serialize slot decisions for one connection; return its organization id.

    Postgres: a row lock on the connection (``FOR UPDATE``). SQLite: the
    engine's transactions start with BEGIN IMMEDIATE, so the decision already
    holds the database's single write lock (``FOR UPDATE`` does not exist there).
    """
    stmt = select(_connections.c.organization_id).where(_connections.c.id == connection_id)
    if conn.dialect.name != "sqlite":
        stmt = stmt.with_for_update()
    return conn.execute(stmt).scalar()


def _ahead_count(conn, handle: QueryHandle, now: datetime) -> dict[str, int]:
    """Live rows on the connection that this query must wait behind."""
    qe = _table()
    ahead_in_queue = and_(
        qe.c.status == "queued",
        or_(qe.c.queued_at < handle.queued_at, and_(qe.c.queued_at == handle.queued_at, qe.c.id < handle.id)),
    )
    row = conn.execute(
        select(
            func.sum(case((qe.c.status == "running", 1), else_=0)).label("running"),
            func.sum(case((ahead_in_queue, 1), else_=0)).label("queued_ahead"),
        ).where(
            qe.c.connection_id == handle.connection_id,
            qe.c.id != handle.id,
            qe.c.status.in_(LIVE_STATUSES),
            qe.c.lease_expires_at > now,
        )
    ).one()
    return {"running": int(row.running or 0), "queued_ahead": int(row.queued_ahead or 0)}


def _try_acquire(conn, handle: QueryHandle) -> dict[str, Any]:
    """One attempt to move ``handle`` from queued to running. Caller holds the lock."""
    qe = _table()
    now = _now()
    state = conn.execute(
        select(qe.c.status, qe.c.kill_requested_at).where(qe.c.id == handle.id)
    ).one_or_none()
    if state is None:
        raise RuntimeError(f"query_executions row {handle.id} disappeared")
    if state.kill_requested_at is not None:
        return {"acquired": False, "killed": True}
    counts = _ahead_count(conn, handle, now)
    if counts["running"] + counts["queued_ahead"] < handle.limit:
        conn.execute(
            update(qe).where(qe.c.id == handle.id).values(
                status="running",
                started_at=now,
                queued_ms=round((time.monotonic() - handle.queued_monotonic) * 1000.0, 1),
                lease_expires_at=now + timedelta(seconds=LEASE_SECONDS),
                updated_at=now,
            )
        )
        return {"acquired": True, **counts}
    return {"acquired": False, **counts}


def _own_running(conn, connection_id: str, agent_execution_id: str | None) -> int | None:
    if not agent_execution_id:
        return None
    qe = _table()
    return int(conn.execute(
        select(func.count()).select_from(qe).where(
            qe.c.connection_id == connection_id,
            qe.c.agent_execution_id == agent_execution_id,
            qe.c.status == "running",
            qe.c.lease_expires_at > _now(),
        )
    ).scalar() or 0)


def _with_retries(fn: Callable, *, attempts: int = 4):
    """Retry a short bookkeeping transaction on transient lock errors."""
    delay = 0.1
    for attempt in range(attempts):
        try:
            return fn()
        except OperationalError:
            if attempt == attempts - 1:
                raise
            time.sleep(delay)
            delay *= 2


def _stored_query_text(capture: Any) -> tuple[str | None, int]:
    if capture is None:
        return None, 0
    if not isinstance(capture, str):
        try:
            capture = json.dumps(capture, default=str, ensure_ascii=False, sort_keys=True)
        except Exception:
            capture = repr(capture)
    if len(capture) > MAX_QUERY_TEXT_CHARS:
        return capture[:MAX_QUERY_TEXT_CHARS], 1
    return capture, 0


def _attempt_locked(engine, handle: QueryHandle) -> dict[str, Any]:
    with _slot_transaction(engine) as conn:
        _lock_connection(conn, handle.connection_id)
        return _try_acquire(conn, handle)


def _safe_progress(progress: Callable, value: dict[str, Any] | None) -> None:
    try:
        progress(value)
    except Exception:
        logger.debug("Slot progress callback failed", exc_info=True)


def begin(
    *,
    connection_id: str,
    limit: int,
    wait_seconds: float,
    query_text: Any,
    meta: dict[str, Any],
    connection_name: str | None = None,
    progress: Callable[[dict[str, Any] | None], None] | None = None,
) -> QueryHandle | None:
    """Record the query and wait for a slot on its connection.

    Returns a running handle, or None when the connection no longer exists
    (the query then runs untracked). Raises ``ConnectionBusyError`` when no slot opens
    within ``wait_seconds`` and ``QueryKilledError`` when a manager kills the
    query while it waits; both leave the row in its final state.
    """
    limit = max(1, int(limit or 1))
    engine = get_engine()
    now = _now()
    handle = QueryHandle(
        id=str(uuid.uuid4()),
        connection_id=str(connection_id),
        limit=limit,
        queued_at=now,
        queued_monotonic=time.monotonic(),
    )
    text_value, truncated = _stored_query_text(query_text)
    row: dict[str, Any] = {
        "id": handle.id,
        "connection_id": handle.connection_id,
        "organization_id": meta.get("organization_id"),
        "data_source_id": meta.get("data_source_id"),
        "user_id": meta.get("user_id"),
        "report_id": meta.get("report_id"),
        "completion_id": meta.get("completion_id"),
        "agent_execution_id": meta.get("agent_execution_id"),
        "tool_execution_id": meta.get("tool_execution_id"),
        "step_id": meta.get("step_id"),
        "query_id": meta.get("query_id"),
        "entity_id": meta.get("entity_id"),
        "source": meta.get("source") or None,
        "client_type": meta.get("client_type"),
        "query_text": text_value,
        "query_truncated": truncated,
        "status": "queued",
        "queued_at": now,
        "tool_attempt": meta.get("tool_attempt"),
        "code_attempt": meta.get("code_attempt"),
        "seq": meta.get("seq"),
        "worker_id": worker_id(),
        "lease_expires_at": now + timedelta(seconds=LEASE_SECONDS),
        "created_at": now,
        "updated_at": now,
    }
    sqlite = engine.dialect.name == "sqlite"

    def _insert_and_try():
        qe = _table()
        with _slot_transaction(engine) as conn:
            org_id = _lock_connection(conn, handle.connection_id)
            if org_id is None and conn.execute(
                select(_connections.c.id).where(_connections.c.id == handle.connection_id)
            ).first() is None:
                # The connection was deleted under a running client: nothing
                # to record against (the row's foreign key) and nothing left
                # to protect. The query runs untracked, as it did before.
                return {"missing": True}
            conn.execute(insert(qe).values(**{**row, "organization_id": row["organization_id"] or org_id}))
            return _try_acquire(conn, handle)

    try:
        # SQLite: one short attempt. The writer lock may be held by the very
        # flow waiting on this query, so retrying could only stall it.
        outcome = _with_retries(_insert_and_try, attempts=1 if sqlite else 4)
    except OperationalError as exc:
        if not (sqlite and _is_sqlite_lock_error(exc)):
            raise
        return _begin_local(handle, row, wait_seconds, connection_name, progress)
    if outcome.get("missing"):
        return None
    _register(handle)
    deadline = time.monotonic() + max(0.0, float(wait_seconds))
    delay = 0.2
    last_counts: dict[str, int] = {}
    reported_waiting = False
    try:
        while not outcome.get("acquired"):
            if outcome.get("killed") or handle.killed:
                finish(handle, "cancelled", error="Cancelled by a connection admin while waiting for a slot",
                       cancel_outcome="not_started")
                raise QueryKilledError(cancellation="not_started")
            last_counts = {k: outcome[k] for k in ("running", "queued_ahead") if k in outcome}
            if progress is not None:
                reported_waiting = True
                _safe_progress(progress, {
                    "connection_id": handle.connection_id,
                    "connection_name": connection_name,
                    "limit": limit,
                    **last_counts,
                })
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                waited = time.monotonic() - handle.queued_monotonic
                own = None
                try:
                    with engine.connect() as conn:
                        own = _own_running(conn, handle.connection_id, meta.get("agent_execution_id"))
                except Exception:
                    logger.debug("Could not count this run's own queries", exc_info=True)
                error = ConnectionBusyError(
                    connection_name, limit, waited, running=last_counts.get("running"), own_running=own,
                )
                finish(handle, "busy", error=str(error))
                raise error
            with _slot_changed:
                _slot_changed.wait(timeout=min(delay, remaining))
            delay = min(delay * 1.5, 1.0)
            try:
                outcome = _with_retries(lambda: _attempt_locked(engine, handle), attempts=1 if sqlite else 4)
            except OperationalError as exc:
                if not (sqlite and _is_sqlite_lock_error(exc)):
                    raise
                # Locked for a moment: still queued, try again next round.
                outcome = {"acquired": False, **last_counts}
    except (ConnectionBusyError, QueryKilledError):
        raise
    except BaseException:
        # A bookkeeping failure must not leave a queued row holding its place.
        finish(handle, "error", error="Could not get a query slot")
        raise
    finally:
        if progress is not None and reported_waiting:
            _safe_progress(progress, None)
    handle.state = "running"
    handle.started_monotonic = time.monotonic()
    return handle


def _local_running(connection_id: str) -> int:
    return sum(
        1 for h in live_handles().values()
        if h.connection_id == connection_id and h.state == "running" and not h.finished
    )


def _begin_local(
    handle: QueryHandle,
    row: dict[str, Any],
    wait_seconds: float,
    connection_name: str | None,
    progress: Callable[[dict[str, Any] | None], None] | None,
) -> QueryHandle:
    """SQLite with the writer lock held: admit against this process's count.

    The query path must not wait on a lock that the flow running the query may
    itself hold. Until the lock frees, the limit is kept per process (the
    queries this worker is running on the connection), and the row's writes
    are queued for the supervisor, which records them as soon as it can. Kill
    reaches the query once its row is written (the API also signals this
    process directly). Postgres never takes this path.
    """
    handle.deferred = True
    _register(handle)
    qe = _table()
    deadline = time.monotonic() + max(0.0, float(wait_seconds))
    reported_waiting = False

    def _record(**final: Any) -> None:
        values = {**row, **final}
        _defer_write(lambda conn: conn.execute(insert(qe).values(**values)))

    try:
        with _slot_changed:
            while True:
                if handle.killed:
                    handle.state = "finished"
                    break
                running = _local_running(handle.connection_id)
                if running < handle.limit:
                    handle.state = "running"
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                if progress is not None:
                    reported_waiting = True
                    _safe_progress(progress, {
                        "connection_id": handle.connection_id,
                        "connection_name": connection_name,
                        "limit": handle.limit,
                        "running": running,
                        "queued_ahead": 0,
                    })
                _slot_changed.wait(timeout=min(0.5, remaining))
    finally:
        if progress is not None and reported_waiting:
            _safe_progress(progress, None)

    now = _now()
    waited_ms = round((time.monotonic() - handle.queued_monotonic) * 1000.0, 1)
    if handle.state == "running":
        handle.started_monotonic = time.monotonic()
        _record(status="running", started_at=now, queued_ms=waited_ms)
        return handle
    handle.finished = True
    _unregister(handle)
    if handle.killed:
        _record(status="cancelled", ended_at=now, queued_ms=waited_ms, lease_expires_at=None,
                cancel_outcome="not_started", error="Cancelled by a connection admin while waiting for a slot")
        raise QueryKilledError(cancellation="not_started")
    error = ConnectionBusyError(connection_name, handle.limit, waited_ms / 1000.0, running=handle.limit)
    _record(status="busy", ended_at=now, queued_ms=waited_ms, lease_expires_at=None, error=str(error))
    raise error


def finish(
    handle: QueryHandle,
    status: str,
    *,
    rows: int | None = None,
    error: str | None = None,
    cancel_outcome: str | None = None,
) -> None:
    """Close the row. Never raises: history must not change the query's outcome."""
    if handle.finished:
        return
    handle.finished = True
    now = _now()
    values: dict[str, Any] = {
        "status": status if status in FINAL_STATUSES else "error",
        "ended_at": now,
        "rows": rows,
        "cancel_outcome": (str(cancel_outcome)[:255] if cancel_outcome else None),
        "lease_expires_at": None,
        "updated_at": now,
    }
    if handle.started_monotonic is not None:
        values["duration_ms"] = round((time.monotonic() - handle.started_monotonic) * 1000.0, 1)
    else:
        # Never started: the whole time was spent waiting for a slot.
        values["queued_ms"] = round((time.monotonic() - handle.queued_monotonic) * 1000.0, 1)
    if error:
        values["error"] = str(error)[:4000]
    qe = _table()

    def _apply(conn):
        conn.execute(update(qe).where(qe.c.id == handle.id).values(**values))

    def _close():
        with get_engine().begin() as conn:
            _apply(conn)

    try:
        if handle.deferred:
            # Its insert is still queued; this update must follow it.
            _defer_write(_apply)
        elif _is_sqlite():
            try:
                _close()
            except OperationalError as exc:
                if not _is_sqlite_lock_error(exc):
                    raise
                _defer_write(_apply)
        else:
            _with_retries(_close)
    except Exception:
        # The lease will run out and the leader sweep marks the row stale.
        logger.warning("Could not record the end of query %s", handle.id, exc_info=True)
    finally:
        _unregister(handle)
        with _slot_changed:
            _slot_changed.notify_all()


# ---------------------------------------------------------------------------
# Leader jobs
# ---------------------------------------------------------------------------


def sweep_stale(now: datetime | None = None) -> int:
    """Mark live rows whose worker stopped renewing their lease as stale."""
    now = now or _now()
    cutoff = now - timedelta(seconds=STALE_GRACE_SECONDS)
    qe = _table()
    with get_engine().begin() as conn:
        result = conn.execute(
            update(qe)
            .where(qe.c.status.in_(LIVE_STATUSES), qe.c.lease_expires_at < cutoff)
            .values(
                status="stale",
                ended_at=now,
                lease_expires_at=None,
                error=func.coalesce(qe.c.error, STALE_ERROR),
                updated_at=now,
            )
        )
        return int(result.rowcount or 0)


def purge_history(now: datetime | None = None, *, days: int = HISTORY_RETENTION_DAYS) -> int:
    """Delete finished rows older than the retention window."""
    now = now or _now()
    cutoff = now - timedelta(days=days)
    qe = _table()
    with get_engine().begin() as conn:
        result = conn.execute(
            delete(qe).where(qe.c.created_at < cutoff, qe.c.status.not_in(LIVE_STATUSES))
        )
        return int(result.rowcount or 0)


async def run_stale_sweep() -> None:
    import asyncio

    count = await asyncio.to_thread(sweep_stale)
    if count:
        logger.info("Marked %d query executions stale (worker stopped renewing)", count)


async def run_history_purge() -> None:
    import asyncio

    count = await asyncio.to_thread(purge_history)
    if count:
        logger.info("Purged %d query executions older than %d days", count, HISTORY_RETENTION_DAYS)
