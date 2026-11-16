"""System logs: keep the application's stdout/stderr as files for Settings → System logs.

Admins read the server's own logs in the UI without shell access to the pod.
Nothing is stored in the database: every worker process appends its lines to
its own files in one directory (``bow_config.system_logs.directory``), and the
page reads every file in it. Put that directory on storage the replicas share
and the page shows every instance.

How a line gets into a file
---------------------------
* ``AppLogHandler`` sits on the root logger (and on uvicorn's loggers, which
  do not propagate to root). Every log record at INFO or above is kept.
* ``install_stream_capture`` wraps ``sys.stdout``/``sys.stderr`` so writes
  that bypass ``logging`` (``print()``, raw tracebacks, ``warnings``) are
  copied too. Logging's own console handlers hold the original streams, so a
  logged line is not captured twice.
* Neither of those touches the disk. They append to an in-memory buffer that
  a writer thread appends to this process's file every second, and that every
  read flushes first, so an admin sees the newest lines of the process that
  served the request.

Files
-----
* One JSON object per line: ``t`` (UTC timestamp), ``l`` (level), ``g``
  (logger), ``s`` (stdout/stderr), ``m`` (message).
* Named ``<instance>__<pid>__<start ms>-<n>.jsonl``, so no two processes ever
  write the same file (nothing to lock, safe on network file systems), and the
  instance a line came from is in the file name. A file is reopened in append
  mode for every write, so a file deleted under a writer is simply created
  again.
* A file that reaches ``segment_bytes`` is closed and a new one started.
  ``cleanup`` deletes files whose newest line is older than the retention
  period, and above the size cap deletes the oldest files first.
* "Delete all" writes the time to ``.cleared_at`` and deletes every file.
  Other processes still hold up to a second of buffered lines; they drop the
  ones from before that time instead of writing them, and reads ignore any
  that slipped through.
"""
from __future__ import annotations

import atexit
import heapq
import itertools
import json
import logging
import os
import re
import socket
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

DEFAULT_LIMIT = 1000
MAX_LIMIT = 10_000
ERROR_LEVELS = ("ERROR", "CRITICAL")

# A single log line longer than this is cut, so one runaway payload dump
# cannot fill the directory.
MAX_MESSAGE_CHARS = 32_000
# Lines waiting for the writer. If the directory cannot be written, the oldest
# lines are dropped rather than growing memory without bound.
BUFFER_MAX = 50_000
FLUSH_INTERVAL_SECONDS = 1.0
CLEANUP_INTERVAL_SECONDS = 60.0
# Largest single file; small enough that a read can skip whole files.
MAX_SEGMENT_BYTES = 16 * 1024 ** 2
# Above the size cap, delete down to this share of it, so the next lines do
# not put the directory straight back over.
_TRIM_TARGET = 0.9
# This page's own requests; recording them would make auto-refresh fill the
# log with its own polling.
_OWN_PATH_PREFIX = "/api/system-logs"
_SUFFIX = ".jsonl"
_CLEARED_MARKER = ".cleared_at"

INSTANCE_ID = re.sub(r"[^A-Za-z0-9._-]", "-", socket.gethostname() or "unknown")[:200].replace("__", "_")

_logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


@dataclass
class LogConfig:
    directory: Path
    retention: timedelta
    max_bytes: int

    @property
    def segment_bytes(self) -> int:
        return max(64 * 1024, min(MAX_SEGMENT_BYTES, self.max_bytes // 8))


class _State:
    def __init__(self) -> None:
        # None until ``configure``: nothing is captured before that (imports,
        # test runs that never start the app).
        self.config: Optional[LogConfig] = None
        self.buffer: deque = deque(maxlen=BUFFER_MAX)
        self.dropped = 0
        # The last write failed (directory missing, read-only, full).
        self.write_error: Optional[str] = None
        self.segment: Optional[str] = None
        self.cleared_at: Optional[datetime] = None
        self.cleared_mtime: Optional[float] = None


_state = _State()
# Serialises flushes (writer thread, reads, shutdown) within this process.
_flush_lock = threading.Lock()
# Per thread: ``busy`` while this process is writing its own files or
# capturing a line (so the writer's own log lines and handler errors are not
# fed back in), ``in_logging`` while a log record is being handled.
_local = threading.local()


def _busy() -> bool:
    return getattr(_local, "busy", False)


def _in_logging() -> bool:
    return getattr(_local, "in_logging", False)


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
        "t": (ts or _utcnow()).isoformat(timespec="microseconds"),
        "l": level,
        "g": logger_name or None,
        "s": stream,
        "m": message,
    })


# ── Configuration ────────────────────────────────────────────────────────


def config_from_settings() -> LogConfig:
    from app.settings.config import settings

    cfg = settings.bow_config.system_logs
    return LogConfig(
        directory=Path(cfg.directory).expanduser().resolve(),
        retention=timedelta(days=cfg.retention_days),
        max_bytes=cfg.max_size_mb * 1024 ** 2,
    )


def configure(config: LogConfig) -> None:
    """Start capturing into ``config.directory`` (created if missing)."""
    with _flush_lock:
        _state.config = config
        _state.segment = None
        _state.cleared_at = None
        _state.cleared_mtime = None
        _state.write_error = None
        try:
            config.directory.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            _state.write_error = str(e)


def get_config() -> LogConfig:
    return _state.config or config_from_settings()


def reset() -> None:
    """Stop capturing and forget buffered lines (tests, reloads)."""
    with _flush_lock:
        _state.config = None
        _state.buffer.clear()
        _state.dropped = 0
        _state.write_error = None
        _state.segment = None
        _state.cleared_at = None
        _state.cleared_mtime = None


# ── Capture ──────────────────────────────────────────────────────────────


class AppLogHandler(logging.Handler):
    """Buffers log records for this process's log file."""

    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self._exc_formatter = logging.Formatter()

    def emit(self, record: logging.LogRecord) -> None:
        if _state.config is None or _busy():
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
        if not s or not isinstance(s, str) or _state.config is None or _busy() or _in_logging():
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


# ── Writer ───────────────────────────────────────────────────────────────


def _parse_ts(value: Any) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _read_cleared_at(directory: Path) -> Optional[datetime]:
    """The last "Delete all" time, re-read only when the marker changes."""
    marker = directory / _CLEARED_MARKER
    try:
        mtime = marker.stat().st_mtime
    except OSError:
        _state.cleared_at, _state.cleared_mtime = None, None
        return None
    if mtime != _state.cleared_mtime:
        try:
            _state.cleared_at = _parse_ts(marker.read_text().strip())
            _state.cleared_mtime = mtime
        except OSError:
            pass
    return _state.cleared_at


_segment_seq = itertools.count()


def _new_segment_name() -> str:
    # The sequence keeps two files opened in the same millisecond apart.
    return f"{INSTANCE_ID}__{os.getpid()}__{int(time.time() * 1000)}-{next(_segment_seq)}{_SUFFIX}"


def flush() -> int:
    """Append this process's buffered lines to its file. Returns how many.

    If the write fails (directory missing, read-only or full) the lines stay
    buffered for the next flush; they are lost only if the buffer overflows
    meanwhile.
    """
    config = _state.config
    if config is None or not _state.buffer:
        return 0
    previous_busy = _busy()
    _local.busy = True
    try:
        with _flush_lock:
            count = len(_state.buffer)
            if not count:
                return 0
            batch = [_state.buffer[i] for i in range(count)]
            cleared_at = _read_cleared_at(config.directory)
            if cleared_at is not None:
                batch = [r for r in batch if (_parse_ts(r["t"]) or cleared_at) > cleared_at]
            if _state.segment is None:
                _state.segment = _new_segment_name()
            try:
                if batch:
                    data = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in batch)
                    with open(config.directory / _state.segment, "a", encoding="utf-8") as f:
                        f.write(data)
                        size = f.tell()
                    if size >= config.segment_bytes:
                        _state.segment = None
                _state.write_error = None
            except OSError as e:
                if _state.write_error != str(e):
                    _logger.warning(f"System logs: could not write to {config.directory}, will retry: {e}")
                _state.write_error = str(e)
                return 0
            for _ in range(count):
                _state.buffer.popleft()
            return len(batch)
    finally:
        _local.busy = previous_busy


# ── Files ────────────────────────────────────────────────────────────────


@dataclass
class _Segment:
    path: Path
    instance_id: str
    mtime: float
    size: int


def _segments(directory: Path) -> list[_Segment]:
    out = []
    try:
        entries = list(os.scandir(directory))
    except OSError:
        return out
    for entry in entries:
        if not entry.name.endswith(_SUFFIX) or entry.name.startswith("."):
            continue
        parts = entry.name[: -len(_SUFFIX)].rsplit("__", 2)
        if len(parts) != 3:
            continue
        try:
            st = entry.stat()
        except OSError:  # deleted by another process meanwhile
            continue
        out.append(_Segment(Path(entry.path), parts[0], st.st_mtime, st.st_size))
    return out


def _unlink(path: Path) -> bool:
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False


def size_bytes(config: Optional[LogConfig] = None) -> int:
    config = config or get_config()
    return sum(s.size for s in _segments(config.directory))


def writer_status() -> dict:
    """This process's writer health: last write failed, lines lost so far."""
    return {"write_error": _state.write_error is not None, "dropped_lines": _state.dropped}


def list_instances(config: Optional[LogConfig] = None) -> list[str]:
    config = config or get_config()
    return sorted({s.instance_id for s in _segments(config.directory)})


def cleanup(config: Optional[LogConfig] = None, now: Optional[datetime] = None) -> dict:
    """Apply retention and the size cap; returns what was deleted.

    A file goes when its newest line (its modification time) is older than the
    retention period or from before the last "Delete all". Over the cap the
    oldest files go first until the total is under 90% of it. Several
    processes may run this at once; a file another one deleted is skipped.
    """
    config = config or get_config()
    now_ts = (now or _utcnow()).replace(tzinfo=timezone.utc).timestamp()
    cutoff = now_ts - config.retention.total_seconds()
    cleared_at = _read_cleared_at(config.directory)
    cleared_ts = cleared_at.replace(tzinfo=timezone.utc).timestamp() if cleared_at else None

    expired = 0
    kept: list[_Segment] = []
    for seg in _segments(config.directory):
        if seg.mtime < cutoff or (cleared_ts is not None and seg.mtime <= cleared_ts):
            expired += _unlink(seg.path)
        else:
            kept.append(seg)

    trimmed = 0
    total = sum(s.size for s in kept)
    if total > config.max_bytes:
        target = config.max_bytes * _TRIM_TARGET
        for seg in sorted(kept, key=lambda s: s.mtime):
            if total <= target:
                break
            trimmed += _unlink(seg.path)
            total -= seg.size
    return {"expired": expired, "trimmed": trimmed, "size_bytes": max(total, 0)}


def delete_all(config: Optional[LogConfig] = None, now: Optional[datetime] = None) -> int:
    """Delete every log file of every instance. Returns the number of files.

    The time goes to ``.cleared_at`` first, so other processes drop the lines
    they buffered before it instead of writing them afterwards.
    """
    config = config or get_config()
    now = now or _utcnow()
    with _flush_lock:
        _state.buffer.clear()
    config.directory.mkdir(parents=True, exist_ok=True)
    marker = config.directory / _CLEARED_MARKER
    tmp = config.directory / f"{_CLEARED_MARKER}.{os.getpid()}.tmp"
    tmp.write_text(now.isoformat(timespec="microseconds"))
    os.replace(tmp, marker)
    _read_cleared_at(config.directory)
    deleted = 0
    for seg in _segments(config.directory):
        deleted += _unlink(seg.path)
    return deleted


# ── Reads ────────────────────────────────────────────────────────────────


def _read_segment(seg: _Segment, cleared_at, start, end, errors_only, needle):
    """Yield (timestamp, entry) for the lines of one file that match."""
    name = seg.path.name
    try:
        with open(seg.path, "rb") as f:
            raw = f.read()
    except FileNotFoundError:
        return
    # A cheap pre-check on the raw line; safe only when the search text is
    # written to the file unescaped.
    raw_needle = needle if needle and not any(c in needle for c in '"\\') and needle.isprintable() else None
    offset = 0
    for line in raw.split(b"\n"):
        line_offset = offset
        offset += len(line) + 1
        if not line:
            continue
        try:
            text = line.decode("utf-8")
        except UnicodeDecodeError:
            continue
        if raw_needle and raw_needle not in text.lower():
            continue
        try:
            rec = json.loads(text)
        except ValueError:  # a line cut short by a crash
            continue
        ts = _parse_ts(rec.get("t"))
        if ts is None:
            continue
        if cleared_at is not None and ts <= cleared_at:
            continue
        if start is not None and ts < start:
            continue
        if end is not None and ts > end:
            continue
        level, stream, message = rec.get("l"), rec.get("s") or "stdout", rec.get("m") or ""
        if errors_only and level not in ERROR_LEVELS and stream != "stderr":
            continue
        if needle and needle not in message.lower():
            continue
        yield ts, {
            "id": f"{name}:{line_offset}",
            "timestamp": ts,
            "instance_id": seg.instance_id,
            "level": level,
            "logger": rec.get("g"),
            "stream": stream,
            "message": message,
        }


def query_logs(
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
    instance_id: Optional[str] = None,
    errors_only: bool = False,
    search: Optional[str] = None,
    limit: int = DEFAULT_LIMIT,
    config: Optional[LogConfig] = None,
) -> tuple[list[dict], bool]:
    """Newest-first lines matching the filters, and whether more matched.

    Times are naive UTC. Files are read newest first; a file's modification
    time is its newest line, so once ``limit`` newer matches are in hand the
    remaining (older) files are not read at all.
    """
    config = config or get_config()
    cleared_at = _read_cleared_at(config.directory)
    needle = search.lower() if search else None
    segments = [
        s for s in _segments(config.directory)
        if (not instance_id or s.instance_id == instance_id)
        # Margin for clock granularity between a line's stamp and the write.
        and (start is None or s.mtime + 2 >= start.replace(tzinfo=timezone.utc).timestamp())
    ]
    segments.sort(key=lambda s: s.mtime, reverse=True)

    # Min-heap of the newest limit+1 matches: (timestamp, tiebreak, entry).
    keep = limit + 1
    heap: list = []
    for seg in segments:
        if len(heap) >= keep:
            oldest_kept = heap[0][0].replace(tzinfo=timezone.utc).timestamp()
            if seg.mtime + 2 < oldest_kept:
                break
        for ts, entry in _read_segment(seg, cleared_at, start, end, errors_only, needle):
            item = (ts, entry["id"], entry)
            if len(heap) < keep:
                heapq.heappush(heap, item)
            elif item > heap[0]:
                heapq.heapreplace(heap, item)
    ordered = [e for _, _, e in sorted(heap, key=lambda x: (x[0], x[1]), reverse=True)]
    return ordered[:limit], len(ordered) > limit


# ── Background writer ────────────────────────────────────────────────────


_writer: Optional[threading.Thread] = None
_writer_stop = threading.Event()


def _writer_loop() -> None:
    _local.busy = True  # nothing this thread logs is captured
    last_cleanup = 0.0
    while not _writer_stop.wait(FLUSH_INTERVAL_SECONDS):
        try:
            flush()
            if time.monotonic() - last_cleanup >= CLEANUP_INTERVAL_SECONDS:
                last_cleanup = time.monotonic()
                if _state.config is not None:
                    cleanup(_state.config)
        except Exception as e:  # noqa: BLE001 — a bad tick must not kill the thread
            _logger.warning(f"System logs writer tick failed: {e}")
    try:
        flush()
    except Exception:  # noqa: BLE001
        pass


def start(config: Optional[LogConfig] = None) -> None:
    """Configure from bow_config and start this process's writer thread.

    Runs in every worker process (each has its own buffer and files).
    """
    global _writer
    configure(config or config_from_settings())
    if _writer is None or not _writer.is_alive():
        _writer_stop.clear()
        _writer = threading.Thread(target=_writer_loop, name="system-logs-writer", daemon=True)
        _writer.start()


def stop(timeout: float = 5.0) -> None:
    """Stop the writer thread after a final flush."""
    global _writer
    _writer_stop.set()
    if _writer is not None:
        _writer.join(timeout)
        _writer = None


# A worker that exits without shutdown events (a crash) still writes what it
# has buffered, including the traceback that killed it.
atexit.register(lambda: flush() if _state.config is not None else None)
