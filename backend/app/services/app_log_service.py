"""System logs: copy the application's stdout/stderr into the ``app_logs`` table.

Admins switch this on from Settings → System logs, optionally for a fixed
period, to read the server's own logs in the UI without shell access to the
pod. It is off by default, and capture costs one boolean check per log line
while it is off.

How a line gets into the table
------------------------------
* ``AppLogHandler`` sits on the root logger (and on uvicorn's loggers, which
  do not propagate to root). Every log record at INFO or above becomes a row.
* ``install_stream_capture`` wraps ``sys.stdout``/``sys.stderr`` so writes
  that bypass ``logging`` (``print()``, raw tracebacks, ``warnings``) are
  copied too. Logging's own console handlers hold the original streams, so a
  logged line is not captured twice.
* Neither of those touches the database. They append to an in-memory buffer
  that ``flush`` writes in batches, from the background worker every few
  seconds and before every read, so an admin reading the page sees the newest
  lines of the instance that served the request.

Every worker process has its own buffer and worker. The on/off switch is
stored in ``instance_settings`` (key ``app_logs``) and every worker re-reads
it, so switching it on one worker reaches the others within
``SETTINGS_REFRESH_SECONDS``.

Rotation (``rotate``) deletes rows older than ``RETENTION``, and if the table
is larger than ``MAX_TABLE_BYTES`` keeps only today's rows (UTC).
"""
from __future__ import annotations

import asyncio
import contextvars
import logging
import socket
import sys
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import delete, distinct, func, insert, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.app_log import AppLog
from app.models.instance_settings import InstanceSettings

SETTING_KEY = "app_logs"
RETENTION = timedelta(days=7)
MAX_TABLE_BYTES = 1024 ** 3  # 1 GB
DEFAULT_LIMIT = 1000
MAX_LIMIT = 10_000
# Longest enable period the API accepts; "until turned off" is no duration.
MAX_DURATION_MINUTES = 7 * 24 * 60
ERROR_LEVELS = ("ERROR", "CRITICAL")

# A single log line longer than this is cut, so one runaway payload dump
# cannot fill the table.
MAX_MESSAGE_CHARS = 32_000
# Lines waiting for the writer. When the database cannot keep up, the oldest
# lines are dropped rather than growing memory without bound.
BUFFER_MAX = 50_000
FLUSH_BATCH = 2_000
# Every write is SQLite write contention, which the scheduler's job store
# already feels as "database is locked" (see ResilientAsyncIOScheduler), so
# batch for a few seconds. Reads flush first, so the page is never stale.
FLUSH_INTERVAL_SECONDS = 5.0
SETTINGS_REFRESH_SECONDS = 10.0
ROTATE_INTERVAL_SECONDS = 600.0
# Per-row overhead added to the message bytes when estimating table size.
_ROW_OVERHEAD_BYTES = 200
# Rows whose message length is averaged for the size estimate (the newest).
_SIZE_SAMPLE_ROWS = 1_000
# When today's lines alone exceed the cap, trim to this share of it, so the
# next lines do not put the table straight back over.
_TRIM_TARGET = 0.9
# This page's own requests; recording them would make auto-refresh fill the
# log with its own polling.
_OWN_PATH_PREFIX = "/api/system-logs"

INSTANCE_ID = (socket.gethostname() or "unknown")[:255]

_logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class _CaptureState:
    def __init__(self) -> None:
        self.enabled = False
        self.expires_at: Optional[datetime] = None
        # Last "Delete all": buffered lines from before it are not written.
        self.cleared_at: Optional[datetime] = None
        self.buffer: deque = deque(maxlen=BUFFER_MAX)
        self.dropped = 0

    def active(self, now: Optional[datetime] = None) -> bool:
        if not self.enabled:
            return False
        if self.expires_at is None:
            return True
        return (now or _utcnow()) < self.expires_at


_state = _CaptureState()
# Set while this process writes to app_logs, so the writer's own log lines
# (SQLAlchemy warnings, a failed insert) are not fed back into the buffer.
_flushing: contextvars.ContextVar[bool] = contextvars.ContextVar("app_log_flushing", default=False)
# Re-entrancy guard: a handler error printed to stderr must not be captured
# from inside the handler that is reporting it.
_local = threading.local()


def _busy() -> bool:
    return _flushing.get() or getattr(_local, "busy", False)


def _in_logging() -> bool:
    return getattr(_local, "in_logging", False)


def _requeue(batch: list) -> None:
    """Put an unwritten batch back at the front, oldest first."""
    overflow = len(_state.buffer) + len(batch) - BUFFER_MAX
    if overflow > 0:
        # extendleft on a full deque discards from the right (the newest).
        _state.dropped += overflow
    _state.buffer.extendleft(reversed(batch))


def _is_own_request(record: logging.LogRecord) -> bool:
    # uvicorn.access records carry (client, method, path, http_version, status).
    args = record.args
    path = args[2] if isinstance(args, tuple) and len(args) >= 3 else None
    return isinstance(path, str) and path.startswith(_OWN_PATH_PREFIX)


def _enqueue(level: Optional[str], logger_name: Optional[str], stream: str,
             message: str, ts: Optional[datetime] = None) -> None:
    if len(message) > MAX_MESSAGE_CHARS:
        message = message[:MAX_MESSAGE_CHARS] + " … [truncated]"
    if len(_state.buffer) >= BUFFER_MAX:
        _state.dropped += 1
    _state.buffer.append({
        "id": str(uuid.uuid4()),
        "timestamp": ts or _utcnow(),
        "instance_id": INSTANCE_ID,
        "level": level,
        "logger": (logger_name or None) and logger_name[:255],
        "stream": stream,
        "message": message,
    })


# ── Capture ──────────────────────────────────────────────────────────────


class AppLogHandler(logging.Handler):
    """Buffers log records for ``app_logs`` while capture is switched on."""

    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self._exc_formatter = logging.Formatter()

    def emit(self, record: logging.LogRecord) -> None:
        if not _state.active() or _busy():
            return
        if record.name == "uvicorn.access" and _is_own_request(record):
            return
        _local.busy = True
        try:
            message = record.getMessage()
            if record.exc_info:
                message = f"{message}\n{self._exc_formatter.formatException(record.exc_info)}"
            elif record.exc_text:
                message = f"{message}\n{record.exc_text}"
            if record.stack_info:
                message = f"{message}\n{record.stack_info}"
            ts = datetime.fromtimestamp(record.created, timezone.utc).replace(tzinfo=None)
            # Same split as the console handlers in logging_config: ERROR and
            # above go to stderr, the rest to stdout.
            stream = "stderr" if record.levelno >= logging.ERROR else "stdout"
            _enqueue(record.levelname, record.name, stream, message, ts)
        except Exception:  # noqa: BLE001 — logging must never raise
            self.handleError(record)
        finally:
            _local.busy = False


class _StreamTee:
    """Writes through to the real stream and copies complete lines."""

    def __init__(self, wrapped: Any, name: str) -> None:
        self._wrapped = wrapped
        self._name = name
        self._pending = ""
        self._lock = threading.Lock()

    @property
    def wrapped(self) -> Any:
        return self._wrapped

    def write(self, s: str) -> int:
        written = self._wrapped.write(s)
        # A console handler writing a log record through this stream: the
        # record is already captured by AppLogHandler.
        if not s or not isinstance(s, str) or not _state.active() or _busy() or _in_logging():
            return written
        try:
            with self._lock:
                parts = (self._pending + s).split("\n")
                self._pending = parts.pop()
                if len(self._pending) > MAX_MESSAGE_CHARS:
                    parts.append(self._pending)
                    self._pending = ""
            for line in parts:
                if line.strip():
                    _enqueue(None, None, self._name, line.rstrip("\r"))
        except Exception:  # noqa: BLE001 — never break the console
            pass
        return written

    def flush(self) -> None:
        self._wrapped.flush()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._wrapped, name)


_handler: Optional[AppLogHandler] = None


def install_log_handler() -> None:
    """Attach the capture handler. Idempotent; call after logging is set up."""
    global _handler
    if _handler is None:
        _handler = AppLogHandler()
    root = logging.getLogger()
    if _handler not in root.handlers:
        root.addHandler(_handler)
    # uvicorn's loggers stop propagation at "uvicorn" and "uvicorn.access"
    # (uvicorn.error propagates into "uvicorn"), so root never sees them.
    for name in ("uvicorn", "uvicorn.access"):
        lg = logging.getLogger(name)
        if not lg.propagate and _handler not in lg.handlers:
            lg.addHandler(_handler)


# The Logger.callHandlers that install_stream_capture found and wrapped.
_wrapped_call_handlers = logging.Logger.callHandlers


def _call_handlers_marked(self: logging.Logger, record: logging.LogRecord) -> None:
    # Marks "a log record is being handled" for this thread, so a console
    # handler that writes through the stream copy is not captured a second
    # time. Handlers created before the copy was installed (logging_config's,
    # uvicorn's) hold the original streams and never reach it at all.
    previous = _in_logging()
    _local.in_logging = True
    try:
        _wrapped_call_handlers(self, record)
    finally:
        _local.in_logging = previous


def install_stream_capture() -> None:
    """Copy direct writes to sys.stdout / sys.stderr. Idempotent."""
    global _wrapped_call_handlers
    # Wrap whatever is installed now (another library may have patched it
    # since import), not the import-time function.
    current = logging.Logger.callHandlers
    if current is not _call_handlers_marked:
        _wrapped_call_handlers = current
        logging.Logger.callHandlers = _call_handlers_marked
    if not isinstance(sys.stdout, _StreamTee):
        sys.stdout = _StreamTee(sys.stdout, "stdout")
    if not isinstance(sys.stderr, _StreamTee):
        sys.stderr = _StreamTee(sys.stderr, "stderr")


def uninstall_stream_capture() -> None:
    # Restore only if nothing has wrapped ours since; otherwise leave the
    # chain alone (ours then just passes through).
    if logging.Logger.callHandlers is _call_handlers_marked:
        logging.Logger.callHandlers = _wrapped_call_handlers
    if isinstance(sys.stdout, _StreamTee):
        sys.stdout = sys.stdout.wrapped
    if isinstance(sys.stderr, _StreamTee):
        sys.stderr = sys.stderr.wrapped


def reset() -> None:
    """Forget this process's switch state and buffered lines (tests, reloads)."""
    _state.enabled = False
    _state.expires_at = None
    _state.cleared_at = None
    _state.buffer.clear()
    _state.dropped = 0


# ── Switch ───────────────────────────────────────────────────────────────


def _parse_dt(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


async def _get_row(db: AsyncSession) -> Optional[InstanceSettings]:
    result = await db.execute(
        select(InstanceSettings).where(
            InstanceSettings.key == SETTING_KEY,
            InstanceSettings.deleted_at.is_(None),
        )
    )
    return result.scalar_one_or_none()


async def refresh_state(db: AsyncSession) -> None:
    """Load the stored switch into this process."""
    row = await _get_row(db)
    value = (row.value or {}) if row else {}
    _state.enabled = bool(value.get("enabled"))
    _state.expires_at = _parse_dt(value.get("expires_at"))
    _state.cleared_at = _parse_dt(value.get("cleared_at"))


async def _save_value(db: AsyncSession, changes: dict) -> None:
    row = await _get_row(db)
    if row is None:
        db.add(InstanceSettings(key=SETTING_KEY, value=dict(changes)))
    else:
        # A new dict, so the JSON column is marked dirty.
        row.value = {**(row.value or {}), **changes}
    await db.commit()


def capture_status(now: Optional[datetime] = None) -> dict:
    """This process's view of the switch, after ``refresh_state``."""
    now = now or _utcnow()
    active = _state.active(now)
    return {
        "enabled": active,
        "expires_at": _state.expires_at if active else None,
        "dropped_lines": _state.dropped,
    }


async def set_enabled(
    db: AsyncSession,
    enabled: bool,
    duration_minutes: Optional[int] = None,
    user_id: Optional[str] = None,
    now: Optional[datetime] = None,
) -> dict:
    """Switch capture on (optionally for ``duration_minutes``) or off."""
    now = now or _utcnow()
    expires_at = now + timedelta(minutes=duration_minutes) if enabled and duration_minutes else None
    await _save_value(db, {
        "enabled": bool(enabled),
        "expires_at": expires_at.isoformat() if expires_at else None,
        "updated_by": user_id,
        "updated_at": now.isoformat(),
    })
    _state.enabled = bool(enabled)
    _state.expires_at = expires_at
    if not enabled:
        # Lines buffered before the switch went off are still written.
        await flush()
    return capture_status(now)


# ── Writer ───────────────────────────────────────────────────────────────


async def flush() -> int:
    """Write this process's buffered lines. Returns how many were written.

    A batch that fails to write (SQLite "database is locked", a dropped
    connection, cancellation) goes back to the front of the buffer for the
    next flush; lines are lost only if the buffer overflows meanwhile.
    """
    from app.dependencies import async_session_maker

    if not _state.buffer:
        return 0
    written = 0
    token = _flushing.set(True)
    try:
        # Re-read the switch first: another worker may have run "Delete all"
        # (or turned recording off) since these lines were buffered.
        try:
            async with async_session_maker() as db:
                await refresh_state(db)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — write with what this worker knows
            pass
        while _state.buffer:
            batch = []
            while _state.buffer and len(batch) < FLUSH_BATCH:
                batch.append(_state.buffer.popleft())
            cleared_at = _state.cleared_at
            if cleared_at is not None:
                batch = [r for r in batch if r["timestamp"] > cleared_at]
                if not batch:
                    continue
            try:
                async with async_session_maker() as db:
                    await db.execute(insert(AppLog), batch)
                    await db.commit()
                written += len(batch)
            except asyncio.CancelledError:
                _requeue(batch)
                raise
            except Exception as e:  # noqa: BLE001 — retry on the next flush
                _requeue(batch)
                _logger.warning(f"System logs: could not write {len(batch)} lines, will retry: {e}")
                break
    finally:
        _flushing.reset(token)
    return written


# ── Rotation ─────────────────────────────────────────────────────────────


async def _logical_size_bytes(db: AsyncSession) -> int:
    """Row count times the average size of the newest rows.

    Summing every message would read the whole table; counting rows only walks
    an index, and recent lines are representative of what rotation keeps.
    """
    rows = int(await db.scalar(select(func.count()).select_from(AppLog)) or 0)
    if not rows:
        return 0
    sample = (
        select(func.length(AppLog.message).label("n"))
        .order_by(AppLog.timestamp.desc())
        .limit(_SIZE_SAMPLE_ROWS)
        .subquery()
    )
    avg_len = float(await db.scalar(select(func.avg(sample.c.n))) or 0)
    return int(rows * (avg_len + _ROW_OVERHEAD_BYTES))


async def table_size_bytes(db: AsyncSession, max_bytes: int = MAX_TABLE_BYTES) -> int:
    """Approximate storage used by app_logs.

    On PostgreSQL the on-disk size is cheap to read but does not shrink after a
    DELETE until VACUUM, so above the limit it is confirmed against the size of
    the rows that are actually there. Elsewhere the row estimate is used.
    """
    if db.get_bind().dialect.name == "postgresql":
        physical = (await db.execute(text("SELECT pg_total_relation_size('app_logs')"))).scalar() or 0
        if int(physical) <= max_bytes:
            return int(physical)
    return await _logical_size_bytes(db)


async def rotate(
    db: AsyncSession,
    now: Optional[datetime] = None,
    max_bytes: int = MAX_TABLE_BYTES,
) -> dict:
    """Apply retention and the size cap.

    * Rows older than ``RETENTION`` are deleted.
    * Rows from before the last "Delete all" are deleted (lines another worker
      wrote while the delete was happening).
    * Over ``max_bytes``: only today's rows (UTC) are kept, and if today's rows
      alone are still over, the oldest are deleted until the estimate is under
      the cap.
    """
    now = now or _utcnow()
    expired = await db.execute(delete(AppLog).where(AppLog.timestamp < now - RETENTION))
    removed = expired.rowcount or 0
    await refresh_state(db)
    if _state.cleared_at is not None:
        stale = await db.execute(delete(AppLog).where(AppLog.timestamp <= _state.cleared_at))
        removed += stale.rowcount or 0
    await db.commit()

    size = await table_size_bytes(db, max_bytes)
    trimmed = 0
    if size > max_bytes:
        start_of_today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        result = await db.execute(delete(AppLog).where(AppLog.timestamp < start_of_today))
        await db.commit()
        trimmed += result.rowcount or 0
        size = await table_size_bytes(db, max_bytes)
    if size > max_bytes:
        rows = int(await db.scalar(select(func.count()).select_from(AppLog)) or 0)
        keep = int(rows * (max_bytes * _TRIM_TARGET) / size)
        if keep <= 0:
            result = await db.execute(delete(AppLog))
        else:
            # The oldest row still kept; everything before it goes.
            boundary = await db.scalar(
                select(AppLog.timestamp).order_by(AppLog.timestamp.desc()).offset(keep - 1).limit(1)
            )
            result = await db.execute(delete(AppLog).where(AppLog.timestamp < boundary))
        await db.commit()
        trimmed += result.rowcount or 0
        size = await table_size_bytes(db, max_bytes)
    return {"expired": removed, "trimmed": trimmed, "size_bytes": size}


# ── Reads ────────────────────────────────────────────────────────────────


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


async def query_logs(
    db: AsyncSession,
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
    instance_id: Optional[str] = None,
    errors_only: bool = False,
    search: Optional[str] = None,
    limit: int = DEFAULT_LIMIT,
) -> tuple[list[AppLog], bool]:
    """Newest-first rows matching the filters, and whether more matched."""
    stmt = select(AppLog)
    if start is not None:
        stmt = stmt.where(AppLog.timestamp >= start)
    if end is not None:
        stmt = stmt.where(AppLog.timestamp <= end)
    if instance_id:
        stmt = stmt.where(AppLog.instance_id == instance_id)
    if errors_only:
        stmt = stmt.where(or_(AppLog.level.in_(ERROR_LEVELS), AppLog.stream == "stderr"))
    if search:
        stmt = stmt.where(AppLog.message.ilike(f"%{_escape_like(search)}%", escape="\\"))
    stmt = stmt.order_by(AppLog.timestamp.desc(), AppLog.id.desc()).limit(limit + 1)
    rows = list((await db.execute(stmt)).scalars().all())
    return rows[:limit], len(rows) > limit


async def list_instances(db: AsyncSession) -> list[str]:
    result = await db.execute(select(distinct(AppLog.instance_id)).order_by(AppLog.instance_id))
    return [r for r in result.scalars().all() if r]


async def delete_all(db: AsyncSession, now: Optional[datetime] = None) -> int:
    """Delete every stored line, on every instance.

    Other workers still hold a few seconds of buffered lines; ``cleared_at``
    tells them (at their next flush) to discard the ones from before now.
    """
    now = now or _utcnow()
    _state.buffer.clear()
    _state.cleared_at = now
    result = await db.execute(delete(AppLog))
    await _save_value(db, {"cleared_at": now.isoformat()})
    return result.rowcount or 0


# ── Background worker ────────────────────────────────────────────────────


async def load_at_startup() -> None:
    from app.dependencies import async_session_maker

    try:
        async with async_session_maker() as db:
            await refresh_state(db)
    except Exception as e:  # noqa: BLE001 — never block startup on this
        _logger.warning(f"System logs: could not load settings: {e}")


async def run_app_log_worker(stop_event: Optional[asyncio.Event] = None) -> None:
    """Flush buffered lines, follow the switch, and rotate, until stopped.

    Runs in every worker process (each has its own buffer and switch cache),
    so it is deliberately not behind the scheduler-leader lock.
    """
    from app.dependencies import async_session_maker

    last_refresh = time.monotonic()
    last_rotate = 0.0  # rotate on the first tick
    while True:
        stopping = False
        if stop_event is not None:
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=FLUSH_INTERVAL_SECONDS)
                stopping = True
            except asyncio.TimeoutError:
                pass
        else:
            await asyncio.sleep(FLUSH_INTERVAL_SECONDS)

        try:
            await flush()
            if stopping:
                return
            now = time.monotonic()
            if now - last_refresh >= SETTINGS_REFRESH_SECONDS:
                last_refresh = now
                async with async_session_maker() as db:
                    await refresh_state(db)
            if now - last_rotate >= ROTATE_INTERVAL_SECONDS:
                last_rotate = now
                token = _flushing.set(True)
                try:
                    async with async_session_maker() as db:
                        await rotate(db)
                finally:
                    _flushing.reset(token)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 — a bad tick must not kill the loop
            _logger.warning(f"System logs worker tick failed: {e}")
            if stopping:
                return
