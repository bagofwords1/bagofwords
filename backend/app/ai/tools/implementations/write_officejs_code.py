import json
import logging
from typing import Any, AsyncIterator, Dict, Optional, Type

from pydantic import BaseModel

from app.ai.tools.base import Tool
from app.ai.tools.metadata import ToolMetadata
from app.ai.tools.officejs_bridge import (
    await_result,
    make_cancel_action,
    make_run_action,
)
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


def _truncate_return_value(rv: Any, max_len: int = 400) -> Optional[str]:
    """Render return_value as a short JSON snippet for the observation summary."""
    if rv is None:
        return None
    try:
        s = json.dumps(rv, default=str)
    except Exception:
        s = str(rv)
    if len(s) > max_len:
        s = s[:max_len] + "…"
    return s


class WriteOfficeJsCodeTool(Tool):
    """Execute arbitrary Office.js code in the user's Excel taskpane.

    Dispatches the code via SSE (tool.partial), then awaits a Future that the
    taskpane resolves by POSTing the result back to /tool-results/{id}. See
    officejs_bridge for the shared await/race logic.
    """

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="write_officejs_code",
            description=(
                "Execute Office.js code in the user's Excel spreadsheet. Use for formulas, "
                "formatting, charts, pivots, multi-sheet operations, or anything the "
                "read_excel_* / write_to_excel tools cannot do. For reads prefer "
                "read_excel_range or read_excel_as_csv — they are cheaper and safer.\n\n"
                "CODE AUTHORING RULES (the `code` argument):\n"
                "- The taskpane wraps your code in Excel.run(async ctx => { ... }), so write just the body.\n"
                "- Load EVERY property before reading — including string properties like .address, .name, "
                ".title.text. Pattern: range.load(['values','address']); await ctx.sync();\n"
                "- Anchors: ctx.workbook.getActiveWorksheet(), ctx.workbook.worksheets.getItem(name), "
                "sheet.getRange('A1:C3'), sheet.getUsedRange(), ctx.workbook.getSelectedRange().\n"
                "- Writing: range.values = [[1,2],[3,4]]; range.formulas = [['=SUM(A:A)']]; "
                "range.format.fill.color = '#FFEB9C'.\n"
                "- Tables: sheet.tables.add('A1:C10', true). Charts: sheet.charts.add('ColumnClustered', range, 'Auto').\n"
                "- Return only small JSON-serializable values (e.g. a sum, an address). Large returned ranges are truncated."
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
        system_completion = runtime_ctx.get("system_completion")
        completion_id = str(system_completion.id) if system_completion is not None else None
        sigkill_event = runtime_ctx.get("sigkill_event")

        yield ToolStartEvent(
            type="tool.start",
            payload={"title": data.description or "Running Excel code"},
        )

        if not tool_call_id:
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

        yield ToolPartialEvent(
            type="tool.partial",
            payload={
                "excel_action": make_run_action(
                    tool_call_id=tool_call_id,
                    code=data.code,
                    description=data.description,
                    completion_id=completion_id,
                ),
            },
        )

        result, cancelled, timed_out = await await_result(
            tool_call_id=tool_call_id,
            sigkill_event=sigkill_event,
        )

        if cancelled or timed_out:
            yield ToolProgressEvent(
                type="tool.progress",
                payload={
                    "excel_action": make_cancel_action(tool_call_id),
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
            logger.warning(
                "write_officejs_code timed out (tool_call_id=%s, completion_id=%s) — "
                "likely a bridge silent-drop: check taskpane console and handleOfficeJsResult.",
                tool_call_id,
                completion_id,
            )
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
        if result.get("success"):
            parts = [f"Excel code executed ({len(ranges_touched)} ranges touched)"]
            rv_snippet = _truncate_return_value(result.get("return_value"))
            if rv_snippet is not None:
                parts.append(f"return_value: {rv_snippet}")
            summary = "; ".join(parts)
        else:
            summary = f"Excel code failed: {result.get('error') or 'unknown error'}"

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
