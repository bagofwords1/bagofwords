"""Shared helper for tools that dispatch Office.js code to the Excel taskpane.

The forward hop is SSE (tool.partial with excel_action). The backward hop is a
separate HTTP POST from the report iframe to /tool-results/{id}. That POST
usually lands on a *different uvicorn worker* than the one running this tool,
so the pending call is persisted as an ``officejs_pending_results`` row that
any worker can resolve; the waiting tool polls it. The in-process registry
(``pending_officejs_registry``) is kept purely as a same-worker fast path so a
local result wakes the tool instantly instead of on the next poll.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, Optional, Tuple

from app.ai.tools.officejs_registry import pending_officejs_registry
from app.services.officejs_result_service import OfficeJsResultService

logger = logging.getLogger(__name__)

DEFAULT_WAIT_TIMEOUT_S = 55  # Below the tool hard_timeout / runner timeout.
POLL_INTERVAL_S = 0.75


def make_run_action(
    *,
    tool_call_id: str,
    code: str,
    description: Optional[str],
    completion_id: Optional[str],
) -> Dict[str, Any]:
    """Build the excel_action payload to ship in a tool.partial event.

    `completion_id` is echoed by the taskpane in its officeJsResult so the
    report iframe can POST to the correct completion without relying on a
    Vue ref being set (avoids the null-ref silent-drop bug).
    """
    action: Dict[str, Any] = {
        "type": "runOfficeJs",
        "id": tool_call_id,
        "code": code,
    }
    if description is not None:
        action["description"] = description
    if completion_id is not None:
        action["completion_id"] = completion_id
    return action


def make_apply_action(
    *,
    tool_call_id: str,
    completion_id: Optional[str],
    columns: list,
    rows: list,
    title: Optional[str] = None,
) -> Dict[str, Any]:
    """Build the applyToExcel excel_action for write_to_excel.

    Carries `id`/`completion_id` like runOfficeJs so the taskpane can ack the
    write with an officeJsResult (older, id-less applyToExcel payloads were
    fire-and-forget and reported success even when nothing was listening).
    """
    action: Dict[str, Any] = {
        "type": "applyToExcel",
        "id": tool_call_id,
        "data": {"columns": columns, "rows": rows},
    }
    if title is not None:
        action["data"]["title"] = title
    if completion_id is not None:
        action["completion_id"] = completion_id
    return action


def make_cancel_action(tool_call_id: str) -> Dict[str, Any]:
    return {"type": "cancelOfficeJs", "id": tool_call_id}


async def await_result(
    *,
    tool_call_id: str,
    sigkill_event: Optional[asyncio.Event],
    timeout_s: float = DEFAULT_WAIT_TIMEOUT_S,
    completion_id: Optional[str] = None,
    user_id: Optional[str] = None,
) -> Tuple[Optional[Dict[str, Any]], bool, bool]:
    """Register the pending call (in-memory + durable row), then race the
    same-worker future, the durable row's poll, sigkill, and the wall clock.

    ``completion_id`` / ``user_id`` bind the durable row to the run that
    dispatched the action so only the initiating user may resolve it.

    Returns (result, cancelled, timed_out). Exactly one of these is truthy
    (cancelled/timed_out) or `result` is set.
    """
    service = OfficeJsResultService()
    future = pending_officejs_registry.register(tool_call_id)
    await service.create_pending(
        tool_call_id=tool_call_id,
        completion_id=completion_id,
        user_id=user_id,
        timeout_seconds=timeout_s,
    )

    result: Optional[Dict[str, Any]] = None
    cancelled = False
    timed_out = False

    result_task = asyncio.ensure_future(future)
    sigkill_task = (
        asyncio.ensure_future(sigkill_event.wait())
        if sigkill_event is not None
        else None
    )
    waiters = [result_task] + ([sigkill_task] if sigkill_task is not None else [])
    deadline = time.monotonic() + timeout_s

    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break

            done, _pending = await asyncio.wait(
                waiters,
                timeout=min(POLL_INTERVAL_S, remaining),
                return_when=asyncio.FIRST_COMPLETED,
            )

            if sigkill_task is not None and sigkill_task in done:
                cancelled = True
                break
            if result_task in done:
                try:
                    result = result_task.result()
                except Exception as e:
                    logger.error("officejs future errored: %s", e, exc_info=True)
                    result = {"success": False, "error": f"Internal error awaiting result: {e}"}
                break

            # No same-worker signal — check whether another worker resolved
            # the durable row.
            polled = await service.poll_result(tool_call_id)
            if polled is not None:
                result = polled
                break

        for task in (result_task, sigkill_task):
            if task is not None and not task.done():
                task.cancel()
    finally:
        pending_officejs_registry.forget(tool_call_id)
        await service.discard(tool_call_id)

    return result, cancelled, timed_out
