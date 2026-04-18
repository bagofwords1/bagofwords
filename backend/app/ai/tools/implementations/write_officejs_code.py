import asyncio
import logging
from typing import Any, AsyncIterator, Dict, Optional, Type

from pydantic import BaseModel

from app.ai.tools.base import Tool
from app.ai.tools.metadata import ToolMetadata
from app.ai.tools.officejs_registry import pending_officejs_registry
from app.ai.tools.schemas.events import (
    ToolEndEvent,
    ToolEvent,
    ToolPartialEvent,
    ToolProgressEvent,
    ToolStartEvent,
)
from app.ai.tools.schemas.write_officejs_code import (
    WriteOfficeJsCodeInput,
    WriteOfficeJsCodeOutput,
)

logger = logging.getLogger(__name__)


WAIT_TIMEOUT_S = 55  # Below ToolMetadata.timeout_seconds and the runner's hard_timeout.


class WriteOfficeJsCodeTool(Tool):
    """Execute arbitrary Office.js code in the user's Excel taskpane.

    Novel pattern: the tool pauses on an asyncio.Future until the taskpane posts
    the result back via a side-channel HTTP endpoint. Races the future against
    sigkill_event (user-initiated stop) and a wall-clock timeout.
    """

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="write_officejs_code",
            description=(
                "Execute Office.js code in the user's Excel spreadsheet. Use for formulas, "
                "formatting, charts, pivots, multi-sheet operations, or reading specific ranges. "
                "For plain append-a-table use write_to_excel instead (cheaper, more reliable)."
            ),
            category="action",
            version="1.0.0",
            input_schema=WriteOfficeJsCodeInput.model_json_schema(),
            output_schema=WriteOfficeJsCodeOutput.model_json_schema(),
            allowed_platforms=["excel"],
            tags=["excel", "spreadsheet", "code"],
            timeout_seconds=60,
        )

    @property
    def input_model(self) -> Type[BaseModel]:
        return WriteOfficeJsCodeInput

    @property
    def output_model(self) -> Type[BaseModel]:
        return WriteOfficeJsCodeOutput

    async def run_stream(
        self,
        tool_input: Dict[str, Any],
        runtime_ctx: Dict[str, Any],
    ) -> AsyncIterator[ToolEvent]:
        data = WriteOfficeJsCodeInput(**tool_input)

        tool_call_id = runtime_ctx.get("tool_call_id")
        if not tool_call_id:
            yield ToolStartEvent(type="tool.start", payload={"title": "Running Excel code"})
            yield ToolEndEvent(
                type="tool.end",
                payload={
                    "output": {"success": False, "error": "Missing tool_call_id in runtime context."},
                    "observation": {
                        "summary": "write_officejs_code misconfigured (no tool_call_id)",
                        "success": False,
                    },
                },
            )
            return

        sigkill_event: Optional[asyncio.Event] = runtime_ctx.get("sigkill_event")

        yield ToolStartEvent(
            type="tool.start",
            payload={"title": data.description or "Running Excel code"},
        )

        future = pending_officejs_registry.register(tool_call_id)

        # Hand the code off to the taskpane. Kept on tool.partial (not tool.end)
        # so the UI keeps the tool in 'running' state while we wait.
        yield ToolPartialEvent(
            type="tool.partial",
            payload={
                "excel_action": {
                    "type": "runOfficeJs",
                    "id": tool_call_id,
                    "code": data.code,
                    "description": data.description,
                },
            },
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

        try:
            done, pending = await asyncio.wait(
                waiters,
                timeout=WAIT_TIMEOUT_S,
                return_when=asyncio.FIRST_COMPLETED,
            )

            if sigkill_task is not None and sigkill_task in done:
                cancelled = True
            elif result_task in done:
                try:
                    result = result_task.result()
                except Exception as e:
                    logger.error("write_officejs_code future errored: %s", e, exc_info=True)
                    result = {"success": False, "error": f"Internal error awaiting result: {e}"}
            else:
                timed_out = True

            for task in pending:
                task.cancel()
        finally:
            pending_officejs_registry.forget(tool_call_id)

        if cancelled or timed_out:
            # Best-effort: tell the taskpane to discard any late result.
            yield ToolProgressEvent(
                type="tool.progress",
                payload={
                    "excel_action": {"type": "cancelOfficeJs", "id": tool_call_id},
                    "stage": "cancel_notified",
                },
            )

        if cancelled:
            yield ToolEndEvent(
                type="tool.end",
                payload={
                    "output": {"success": False, "error": "Cancelled by user."},
                    "observation": {
                        "summary": "write_officejs_code cancelled",
                        "success": False,
                    },
                },
            )
            return

        if timed_out:
            yield ToolEndEvent(
                type="tool.end",
                payload={
                    "output": {
                        "success": False,
                        "error": "Timed out waiting for Excel taskpane to return a result.",
                    },
                    "observation": {
                        "summary": "write_officejs_code timed out",
                        "success": False,
                    },
                },
            )
            return

        result = result or {"success": False, "error": "No result returned."}
        ranges_touched = result.get("ranges_touched") or []
        summary = (
            f"Excel code executed successfully ({len(ranges_touched)} ranges touched)."
            if result.get("success")
            else f"Excel code failed: {result.get('error') or 'unknown error'}"
        )

        yield ToolEndEvent(
            type="tool.end",
            payload={
                "output": result,
                "observation": {
                    "summary": summary,
                    "success": bool(result.get("success")),
                },
            },
        )
