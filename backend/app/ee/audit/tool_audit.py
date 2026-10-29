# Tool-level audit logging helper
# Creates its own DB session to avoid sharing the agent's long-lived session.
# Calls enqueue audit events and return immediately; audit failures never break
# tool execution.
#
# Durability contract (docs/design/audit-log-streams.md, WP0): an event with an
# organization is never silently dropped.
#   - The worker drains the queue in batches (one session, one commit), so it
#     keeps up with bursts that a commit-per-event worker could not.
#   - A failed batch is retried with backoff, then written event by event so
#     one bad row cannot sink the rest.
#   - A full queue applies backpressure (bounded wait), then the caller writes
#     its own event inline instead of discarding it.
#   - Whatever still cannot reach the database (an outage that outlasts the
#     retries, or a shutdown that times out) is appended to a local JSONL spill
#     file, replayed into audit_logs on the next start.
# Event ids are assigned at enqueue time, so a retry or replay of an event that
# did reach the table is detected and skipped rather than written twice.
# created_at is stamped when the row is written, not when the event happened:
# log streams cursor on created_at and only wait a short lag window for late
# commits, so a row that lands minutes later (backpressure, a replayed spill)
# with an old created_at would sit behind a cursor that has already moved on.
# A delayed write keeps the original time in details["occurred_at"].

import asyncio
import contextlib
import json
import logging
import os
import time as _time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Optional, Dict, Any, List

logger = logging.getLogger(__name__)

# Max length for individual query strings stored in audit details
_MAX_QUERY_LEN = 500
_MAX_QUERIES = 10
_QUEUE_MAXSIZE = 1000
_BATCH_MAX = 200
_RETRY_DELAYS_SECONDS = (0.5, 1.0, 2.0)
_ENQUEUE_WAIT_SECONDS = 2.0
_SHUTDOWN_DRAIN_TIMEOUT_SECONDS = 5.0
_SLOW_AUDIT_WRITE_MS = 1000.0
_LATE_WRITE_SECONDS = 5.0
# A spill left by an outage is replayed while the service keeps running: after
# the worker's next successful write (at most this often) and by a periodic
# scheduler job (scheduled_spill_replay), not only on the next start.
_REPLAY_MIN_INTERVAL_SECONDS = 30.0


@dataclass(frozen=True)
class ToolAuditEvent:
    organization_id: str
    action: str
    user_id: Optional[str]
    resource_type: Optional[str]
    resource_id: Optional[str]
    details: Optional[dict]
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    occurred_at: datetime = field(default_factory=datetime.utcnow)

    def to_json(self) -> str:
        d = asdict(self)
        d["occurred_at"] = self.occurred_at.isoformat()
        return json.dumps(d, default=str)

    @classmethod
    def from_json(cls, line: str) -> "ToolAuditEvent":
        d = json.loads(line)
        d["occurred_at"] = datetime.fromisoformat(d["occurred_at"])
        return cls(**d)


_audit_queue: Optional[asyncio.Queue] = None
_audit_worker_task: Optional[asyncio.Task] = None
_replay_task: Optional[asyncio.Task] = None
_last_replay_attempt = 0.0
_stats = {
    "enqueued": 0,
    "written": 0,
    "failed": 0,
    "inline": 0,
    "spilled": 0,
    "replayed": 0,
    "dropped": 0,
}


def _reset_stats_for_tests() -> None:
    global _last_replay_attempt
    for k in _stats:
        _stats[k] = 0
    _last_replay_attempt = 0.0


def _truncate_queries(queries: list) -> list:
    """Truncate query strings to keep audit detail payload reasonable."""
    truncated = []
    for q in (queries or [])[:_MAX_QUERIES]:
        s = str(q) if q else ""
        truncated.append(s[:_MAX_QUERY_LEN] + ("..." if len(s) > _MAX_QUERY_LEN else ""))
    return truncated


def _build_event(
    runtime_ctx: Dict[str, Any],
    action: str,
    resource_type: Optional[str],
    resource_id: Optional[str],
    details: Optional[dict],
) -> Optional[ToolAuditEvent]:
    user = runtime_ctx.get("user")
    organization = runtime_ctx.get("organization")
    org_id = str(organization.id) if organization else None
    user_id = str(user.id) if user else None

    if not org_id:
        logger.debug("log_tool_audit skipped: no organization in runtime_ctx")
        return None

    # Enrich details with execution context while the runtime objects are still
    # in scope; only primitive data goes onto the background queue.
    enriched = dict(details or {})
    agent_execution_id = runtime_ctx.get("agent_execution_id")
    mode = runtime_ctx.get("mode")
    if agent_execution_id:
        enriched.setdefault("agent_execution_id", str(agent_execution_id))
    if mode:
        enriched.setdefault("execution_mode", str(mode))

    return ToolAuditEvent(
        organization_id=org_id,
        action=action,
        user_id=user_id,
        resource_type=resource_type,
        resource_id=resource_id,
        details=enriched if enriched else None,
    )


# ---------------------------------------------------------------------------
# Database writes
# ---------------------------------------------------------------------------

async def _write_batch(events: List[ToolAuditEvent]) -> int:
    """Insert events not already present; one session, one commit.

    Returns the number of rows inserted. Ids already in the table (a retry
    after a commit that succeeded but reported an error, or a replay) are
    skipped, which keeps every path at-most-once per event id.
    """
    from sqlalchemy import select
    from app.dependencies import async_session_maker
    from app.ee.audit.models import AuditLog

    started = _time.monotonic()
    async with async_session_maker() as session:
        ids = [e.id for e in events]
        existing = set((await session.execute(select(AuditLog.id).where(AuditLog.id.in_(ids)))).scalars())
        fresh = [e for e in events if e.id not in existing]
        written_at = datetime.utcnow()
        session.add_all([
            AuditLog(
                id=e.id,
                organization_id=e.organization_id,
                user_id=e.user_id,
                action=e.action,
                resource_type=e.resource_type,
                resource_id=e.resource_id,
                details=_with_occurred_at(e, written_at),
                created_at=written_at,
            )
            for e in fresh
        ])
        await session.commit()
    duration_ms = (_time.monotonic() - started) * 1000.0
    if duration_ms >= _SLOW_AUDIT_WRITE_MS:
        logger.warning("Tool audit batch write was slow: events=%d duration_ms=%.1f", len(events), duration_ms)
    return len(fresh)


def _with_occurred_at(e: ToolAuditEvent, written_at: datetime) -> Optional[dict]:
    if (written_at - e.occurred_at).total_seconds() < _LATE_WRITE_SECONDS:
        return e.details
    return {**(e.details or {}), "occurred_at": e.occurred_at.isoformat() + "Z"}


async def _persist(events: List[ToolAuditEvent]) -> List[ToolAuditEvent]:
    """Write events with retry; return the ones that still could not be written."""
    for attempt, delay in enumerate((0.0,) + _RETRY_DELAYS_SECONDS):
        if delay:
            await asyncio.sleep(delay)
        try:
            n = await _write_batch(events)
            _stats["written"] += n
            return []
        except asyncio.CancelledError:
            raise
        except Exception:
            _stats["failed"] += 1
            logger.warning(
                "Tool audit batch write failed (attempt %d/%d, events=%d)",
                attempt + 1, len(_RETRY_DELAYS_SECONDS) + 1, len(events), exc_info=True,
            )

    if len(events) == 1:
        return list(events)

    # Isolate a poison row: one attempt per event; survivors go to the spill.
    remaining: List[ToolAuditEvent] = []
    for e in events:
        try:
            n = await _write_batch([e])
            _stats["written"] += n
        except asyncio.CancelledError:
            raise
        except Exception:
            _stats["failed"] += 1
            remaining.append(e)
    return remaining


# ---------------------------------------------------------------------------
# Disk spill + replay
# ---------------------------------------------------------------------------

def _spill_dir() -> str:
    configured = os.environ.get("BOW_AUDIT_SPILL_DIR")
    if configured:
        return configured
    backend_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    return os.path.join(backend_root, "data", "audit-spill")


def _spill(events: List[ToolAuditEvent]) -> None:
    if not events:
        return
    path = None
    try:
        d = _spill_dir()
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, f"{os.getpid()}-{int(_time.time() * 1000)}-{uuid.uuid4().hex[:8]}.jsonl")
        with open(path, "a", encoding="utf-8") as f:
            for e in events:
                f.write(e.to_json() + "\n")
            f.flush()
            os.fsync(f.fileno())
        _stats["spilled"] += len(events)
        logger.warning("Spilled %d tool audit events to %s; they are replayed on next start", len(events), path)
    except Exception:
        _stats["dropped"] += len(events)
        logger.error("Could not spill %d tool audit events (path=%s); they are lost", len(events), path, exc_info=True)


def _has_spill() -> bool:
    d = _spill_dir()
    try:
        return any(n.endswith(".jsonl") for n in os.listdir(d))
    except OSError:
        return False


def _maybe_replay_spill() -> None:
    """Start a background replay when spill files exist, at most every
    _REPLAY_MIN_INTERVAL_SECONDS and never two at once in this process."""
    global _replay_task, _last_replay_attempt
    if _replay_task is not None and not _replay_task.done():
        return
    now = _time.monotonic()
    if now - _last_replay_attempt < _REPLAY_MIN_INTERVAL_SECONDS or not _has_spill():
        return
    _last_replay_attempt = now
    _replay_task = asyncio.create_task(replay_spilled_tool_audit_events(), name="bow_tool_audit_replay")


async def scheduled_spill_replay() -> None:
    """Scheduler entry point: replay spill files this host still holds."""
    if _has_spill():
        await replay_spilled_tool_audit_events()


async def replay_spilled_tool_audit_events() -> int:
    """Write every spilled event into audit_logs; return rows inserted.

    Each file is claimed with an atomic rename before reading, so concurrent
    replayers never process the same file. A file is deleted only after all of
    its events are in the table; on failure it is released for the next start.
    """
    d = _spill_dir()
    if not os.path.isdir(d):
        return 0
    inserted = 0
    for name in sorted(os.listdir(d)):
        if not name.endswith(".jsonl"):
            continue
        src = os.path.join(d, name)
        claimed = src + ".replaying"
        try:
            os.rename(src, claimed)
        except OSError:
            continue
        try:
            with open(claimed, encoding="utf-8") as f:
                events = [ToolAuditEvent.from_json(line) for line in f if line.strip()]
            for i in range(0, len(events), _BATCH_MAX):
                n = await _write_batch(events[i:i + _BATCH_MAX])
                inserted += n
                _stats["replayed"] += n
            os.remove(claimed)
        except Exception:
            logger.warning("Replaying spilled audit file %s failed; will retry", name, exc_info=True)
            with contextlib.suppress(OSError):
                os.rename(claimed, src)
    if inserted:
        logger.info("Replayed %d spilled tool audit events", inserted)
    return inserted


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------

async def _audit_worker(queue: asyncio.Queue) -> None:
    while True:
        batch = [await queue.get()]
        while len(batch) < _BATCH_MAX:
            try:
                batch.append(queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        try:
            remaining = await _persist(batch)
            _spill(remaining)
            if not remaining:
                _maybe_replay_spill()  # the database is reachable again
        except asyncio.CancelledError:
            # Shutdown interrupted an in-flight batch: keep it on disk.
            _spill(batch)
            raise
        except Exception:
            logger.warning("Tool audit worker error; spilling batch", exc_info=True)
            _spill(batch)
        finally:
            for _ in batch:
                queue.task_done()


def _ensure_worker() -> asyncio.Queue:
    global _audit_queue, _audit_worker_task

    if _audit_queue is None:
        _audit_queue = asyncio.Queue(maxsize=_QUEUE_MAXSIZE)
    if _audit_worker_task is None or _audit_worker_task.done():
        # Reuse the existing queue so events waiting in it survive a restart.
        _audit_worker_task = asyncio.create_task(_audit_worker(_audit_queue), name="bow_tool_audit_worker")
        logger.info("Started tool audit background worker")
    return _audit_queue


async def start_tool_audit_worker() -> None:
    """Start the background audit worker for app lifespan startup."""
    _ensure_worker()


async def drain_tool_audit_queue(timeout: float = _SHUTDOWN_DRAIN_TIMEOUT_SECONDS) -> None:
    """Wait for queued audit writes to finish, bounded by timeout."""
    if _audit_queue is None:
        return
    await asyncio.wait_for(_audit_queue.join(), timeout=timeout)


async def stop_tool_audit_worker(
    timeout: float = _SHUTDOWN_DRAIN_TIMEOUT_SECONDS,
    *,
    drain: bool = True,
) -> None:
    """Drain then stop the background audit worker; spill anything left."""
    global _audit_queue, _audit_worker_task

    queue = _audit_queue
    task = _audit_worker_task

    if drain and queue is not None:
        try:
            await asyncio.wait_for(queue.join(), timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning(
                "Timed out draining tool audit queue after %.1fs; spilling pending_events=%s",
                timeout,
                queue.qsize(),
            )

    if task is not None and not task.done():
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    if queue is not None:
        leftover: List[ToolAuditEvent] = []
        while True:
            try:
                leftover.append(queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        _spill(leftover)

    _audit_queue = None
    _audit_worker_task = None


def get_tool_audit_queue_stats() -> dict:
    """Expose lightweight counters for diagnostics and tests."""
    return {"queued": _audit_queue.qsize() if _audit_queue is not None else 0, **_stats}


async def log_tool_audit(
    runtime_ctx: Dict[str, Any],
    action: str,
    resource_type: Optional[str] = None,
    resource_id: Optional[str] = None,
    details: Optional[dict] = None,
) -> None:
    """Enqueue an audit event from within an AI tool execution.

    Extracts user/org/execution metadata from runtime_ctx and places an audit
    event on a bounded background queue. Normally the await only covers
    enqueue work; under overload it waits briefly for room and, failing that,
    writes the event itself rather than dropping it.
    """
    try:
        event = _build_event(runtime_ctx, action, resource_type, resource_id, details)
        if event is None:
            return

        queue = _ensure_worker()
        try:
            queue.put_nowait(event)
            _stats["enqueued"] += 1
            return
        except asyncio.QueueFull:
            pass
        try:
            await asyncio.wait_for(queue.put(event), timeout=_ENQUEUE_WAIT_SECONDS)
            _stats["enqueued"] += 1
            return
        except asyncio.TimeoutError:
            pass
        _stats["inline"] += 1
        logger.warning("Tool audit queue saturated; writing inline: action=%s", action)
        _spill(await _persist([event]))
    except Exception:
        logger.warning("log_tool_audit failed", exc_info=True)
