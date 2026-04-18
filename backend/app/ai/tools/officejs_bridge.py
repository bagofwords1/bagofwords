"""Shared helper for tools that dispatch Office.js code to the Excel taskpane.

The forward hop is SSE (tool.partial with excel_action). The backward hop is a
separate HTTP POST from the report iframe to /tool-results/{id}, which resolves
a Future in pending_officejs_registry. Tools that use this pattern all share
the same await-with-race logic — this module centralizes it.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, Optional, Tuple

from app.ai.tools.officejs_registry import pending_officejs_registry

logger = logging.getLogger(__name__)

DEFAULT_WAIT_TIMEOUT_S = 55  # Below the tool hard_timeout / runner timeout.


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


def make_cancel_action(tool_call_id: str) -> Dict[str, Any]:
    return {"type": "cancelOfficeJs", "id": tool_call_id}


async def await_result(
    *,
    tool_call_id: str,
    sigkill_event: Optional[asyncio.Event],
    timeout_s: float = DEFAULT_WAIT_TIMEOUT_S,
) -> Tuple[Optional[Dict[str, Any]], bool, bool]:
    """Register a pending Future and race it against sigkill + wall-clock timeout.

    Returns (result, cancelled, timed_out). Exactly one of these is truthy
    (cancelled/timed_out) or `result` is set.
    """
    future = pending_officejs_registry.register(tool_call_id)
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

    try:
        done, pending = await asyncio.wait(
            waiters,
            timeout=timeout_s,
            return_when=asyncio.FIRST_COMPLETED,
        )

        if sigkill_task is not None and sigkill_task in done:
            cancelled = True
        elif result_task in done:
            try:
                result = result_task.result()
            except Exception as e:
                logger.error("officejs future errored: %s", e, exc_info=True)
                result = {"success": False, "error": f"Internal error awaiting result: {e}"}
        else:
            timed_out = True

        for task in pending:
            task.cancel()
    finally:
        pending_officejs_registry.forget(tool_call_id)

    return result, cancelled, timed_out
