# Tool-level audit logging helper
# Creates its own DB session to avoid sharing the agent's long-lived session.
# Calls enqueue audit events and return immediately; audit failures never break
# tool execution.

import asyncio
import contextlib
import logging
import time as _time
from dataclasses import dataclass
from typing import Optional, Dict, Any

from app.ee.audit.service import audit_service

logger = logging.getLogger(__name__)

# Max length for individual query strings stored in audit details
_MAX_QUERY_LEN = 500
_MAX_QUERIES = 10
_QUEUE_MAXSIZE = 1000
_SHUTDOWN_DRAIN_TIMEOUT_SECONDS = 5.0
_SLOW_AUDIT_WRITE_MS = 1000.0


@dataclass(frozen=True)
class ToolAuditEvent:
    organization_id: str
    action: str
    user_id: Optional[str]
    resource_type: Optional[str]
    resource_id: Optional[str]
    details: Optional[dict]


_audit_queue: Optional[asyncio.Queue] = None
_audit_worker_task: Optional[asyncio.Task] = None
_enqueued_count = 0
_written_count = 0
_failed_count = 0
_dropped_count = 0


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


async def _write_event(event: ToolAuditEvent) -> None:
    from app.dependencies import async_session_maker

    started = _time.monotonic()
    async with async_session_maker() as session:
        await audit_service.log(
            db=session,
            organization_id=event.organization_id,
            action=event.action,
            user_id=event.user_id,
            resource_type=event.resource_type,
            resource_id=event.resource_id,
            details=event.details,
        )
    duration_ms = (_time.monotonic() - started) * 1000.0
    if duration_ms >= _SLOW_AUDIT_WRITE_MS:
        logger.warning(
            "Tool audit write was slow: action=%s resource_type=%s resource_id=%s duration_ms=%.1f",
            event.action,
            event.resource_type,
            event.resource_id,
            duration_ms,
        )
    else:
        logger.debug(
            "Tool audit write completed: action=%s duration_ms=%.1f",
            event.action,
            duration_ms,
        )


async def _audit_worker(queue: asyncio.Queue) -> None:
    global _written_count, _failed_count

    while True:
        event = await queue.get()
        try:
            await _write_event(event)
            _written_count += 1
        except asyncio.CancelledError:
            raise
        except Exception:
            _failed_count += 1
            logger.warning(
                "Tool audit write failed: action=%s resource_type=%s resource_id=%s",
                getattr(event, "action", None),
                getattr(event, "resource_type", None),
                getattr(event, "resource_id", None),
                exc_info=True,
            )
        finally:
            queue.task_done()


def _ensure_worker() -> asyncio.Queue:
    global _audit_queue, _audit_worker_task

    if _audit_queue is None or _audit_worker_task is None or _audit_worker_task.done():
        _audit_queue = asyncio.Queue(maxsize=_QUEUE_MAXSIZE)
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
    """Drain then stop the background audit worker."""
    global _audit_queue, _audit_worker_task

    queue = _audit_queue
    task = _audit_worker_task

    if drain and queue is not None:
        try:
            await asyncio.wait_for(queue.join(), timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning(
                "Timed out draining tool audit queue after %.1fs; pending_events=%s",
                timeout,
                queue.qsize(),
            )

    if task is not None and not task.done():
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    _audit_queue = None
    _audit_worker_task = None


def get_tool_audit_queue_stats() -> dict:
    """Expose lightweight counters for diagnostics and tests."""
    return {
        "queued": _audit_queue.qsize() if _audit_queue is not None else 0,
        "enqueued": _enqueued_count,
        "written": _written_count,
        "failed": _failed_count,
        "dropped": _dropped_count,
    }


async def log_tool_audit(
    runtime_ctx: Dict[str, Any],
    action: str,
    resource_type: Optional[str] = None,
    resource_id: Optional[str] = None,
    details: Optional[dict] = None,
) -> None:
    """Enqueue a non-blocking audit log from within an AI tool execution.

    Extracts user/org/execution metadata from runtime_ctx and places an audit
    event on a bounded background queue. The await only covers enqueue work, not
    DB I/O.
    """
    global _enqueued_count, _dropped_count

    try:
        event = _build_event(runtime_ctx, action, resource_type, resource_id, details)
        if event is None:
            return

        queue = _ensure_worker()
        try:
            queue.put_nowait(event)
            _enqueued_count += 1
        except asyncio.QueueFull:
            _dropped_count += 1
            logger.warning(
                "Tool audit queue full; dropping audit event: action=%s resource_type=%s resource_id=%s",
                action,
                resource_type,
                resource_id,
            )
    except Exception:
        logger.warning("log_tool_audit enqueue failed", exc_info=True)
