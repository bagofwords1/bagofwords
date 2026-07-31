"""
Confirmation registry for tool confirmations.

Allows tools to pause execution and wait for user approval via the frontend.
"""

import asyncio
import logging
from typing import Dict

logger = logging.getLogger(__name__)

PENDING_CONFIRMATIONS: Dict[str, asyncio.Future] = {}

# Server-side context for pending confirmations (who may respond, what tool it
# is for). Written by the tool that pauses, read by the resolve endpoint so it
# can authorize the responder and persist a remembered preference.
PENDING_CONFIRMATION_META: Dict[str, dict] = {}


def register_confirmation(confirmation_id: str, meta: dict | None = None) -> asyncio.Future:
    """Create (but don't await) a pending confirmation future.

    Use with ``resolve_confirmation`` and ``discard_confirmation``. Unlike
    ``wait_for_confirmation`` this never auto-approves — the caller owns the
    wait loop and the timeout semantics.
    """
    loop = asyncio.get_running_loop()
    future: asyncio.Future = loop.create_future()
    PENDING_CONFIRMATIONS[confirmation_id] = future
    PENDING_CONFIRMATION_META[confirmation_id] = dict(meta or {})
    logger.info(f"Confirmation {confirmation_id}: registered (pending={len(PENDING_CONFIRMATIONS)})")
    return future


def get_confirmation_meta(confirmation_id: str) -> dict | None:
    return PENDING_CONFIRMATION_META.get(confirmation_id)


def discard_confirmation(confirmation_id: str) -> None:
    PENDING_CONFIRMATIONS.pop(confirmation_id, None)
    PENDING_CONFIRMATION_META.pop(confirmation_id, None)


async def wait_for_confirmation(confirmation_id: str, timeout: float = 5.0) -> dict:
    """Wait for a user confirmation response, auto-approving on timeout."""
    loop = asyncio.get_running_loop()
    future: asyncio.Future = loop.create_future()
    PENDING_CONFIRMATIONS[confirmation_id] = future
    logger.info(f"Confirmation {confirmation_id}: waiting (timeout={timeout}s, pending={len(PENDING_CONFIRMATIONS)})")
    try:
        result = await asyncio.wait_for(future, timeout=timeout)
        logger.info(f"Confirmation {confirmation_id}: resolved by user — {result}")
        return result
    except asyncio.TimeoutError:
        logger.info(f"Confirmation {confirmation_id}: timed out, auto-approving")
        return {"approved": True}
    finally:
        PENDING_CONFIRMATIONS.pop(confirmation_id, None)


def resolve_confirmation(confirmation_id: str, response: dict) -> bool:
    """Resolve a pending confirmation. Returns True if found and resolved."""
    future = PENDING_CONFIRMATIONS.get(confirmation_id)
    if future is None or future.done():
        logger.warning(f"Confirmation {confirmation_id}: not found or already done (pending={list(PENDING_CONFIRMATIONS.keys())})")
        return False
    future.set_result(response)
    logger.info(f"Confirmation {confirmation_id}: resolved with {response}")
    return True


# ---------------------------------------------------------------------------
# Generic pause-for-approval helper (extracted from execute_mcp's ask flow)
# ---------------------------------------------------------------------------

# Same budget as execute_mcp: total wait must stay under the ToolRunner hard
# timeout (300s) and the keepalives under its idle timeout (180s).
ASK_TIMEOUT_S: float = 240.0
ASK_KEEPALIVE_S: float = 15.0
ASK_POLL_S: float = 3.0

KIND_BUILTIN_TOOL = "builtin_tool"


async def stream_user_confirmation(
    runtime_ctx: dict,
    *,
    kind: str,
    tool_name: str,
    payload: dict,
    result: dict,
    timeout_s: float = ASK_TIMEOUT_S,
):
    """Pause a BUILTIN tool for a user Allow/Deny, yielding the tool events.

    Async-generator helper mirroring execute_mcp's ask flow: registers an
    in-process future (same-worker fast path) plus a durable
    ``tool_confirmations`` row (source of truth — the approval POST is load-
    balanced across workers), emits the ``tool.confirmation`` event the
    frontend renders, then waits with keepalive progress events so the
    ToolRunner watchdog stays quiet. The caller forwards yielded events and
    then reads ``result``:

        result["response"]  -> decision dict or None (timeout / sigkill)
        result["approved"]  -> bool

    ``payload`` is merged into the confirmation event for the UI card (e.g.
    agent names + the model's reason).
    """
    from uuid import uuid4
    from app.ai.tools.schemas.events import ToolConfirmationEvent, ToolProgressEvent
    from app.services.tool_confirmation_service import ToolConfirmationService

    db = runtime_ctx.get("db")
    user = runtime_ctx.get("user")
    report = runtime_ctx.get("report")
    head = runtime_ctx.get("head_completion")
    system = runtime_ctx.get("system_completion")

    confirmation_id = str(uuid4())
    future = register_confirmation(confirmation_id, meta={
        "kind": kind,
        "user_id": str(user.id) if user else None,
        "completion_ids": [str(c.id) for c in (head, system) if c is not None],
        "tool_name": tool_name,
    })
    confirmations = ToolConfirmationService()
    await confirmations.create(
        db,
        confirmation_id=confirmation_id,
        kind=kind,
        organization_id=getattr(runtime_ctx.get("organization"), "id", None),
        report_id=str(report.id) if report else None,
        system_completion_id=str(system.id) if system is not None else None,
        head_completion_id=str(head.id) if head is not None else None,
        user_id=str(user.id) if user else None,
        tool_name=tool_name,
        arguments=payload or {},
        timeout_seconds=timeout_s,
    )
    # Release the shared session before blocking so the approval endpoint can
    # take the DB writer (SQLite would otherwise deadlock into a 500).
    try:
        await db.commit()
    except Exception as e:
        logger.warning(f"confirmation: releasing db before wait failed: {e!r}")

    response: dict | None = None
    try:
        yield ToolConfirmationEvent(type="tool.confirmation", payload={
            "kind": kind,
            "confirmation_id": confirmation_id,
            "tool_name": tool_name,
            "timeout_seconds": timeout_s,
            **(payload or {}),
        })
        sigkill = runtime_ctx.get("sigkill_event")
        waited = 0.0
        while waited < timeout_s:
            if sigkill is not None and sigkill.is_set():
                break
            try:
                response = await asyncio.wait_for(asyncio.shield(future), timeout=ASK_POLL_S)
                break
            except asyncio.TimeoutError:
                response = await confirmations.poll_decision(confirmation_id)
                if response is not None:
                    break
                waited += ASK_POLL_S
                if waited % ASK_KEEPALIVE_S < ASK_POLL_S:
                    yield ToolProgressEvent(type="tool.progress", payload={
                        "stage": "awaiting_approval", "timing": False,
                        "remaining_seconds": max(0, int(timeout_s - waited)),
                    })
    finally:
        discard_confirmation(confirmation_id)
        if response is None:
            response = await confirmations.poll_decision(confirmation_id)
        if response is None:
            await confirmations.expire(db, confirmation_id)

    result["response"] = response
    result["approved"] = bool(response and response.get("approved"))
    result["timed_out"] = response is None
    result["resolved_by_name"] = (
        (response or {}).get("resolved_by_name")
        or (getattr(user, "name", None) if user else None)
    )
