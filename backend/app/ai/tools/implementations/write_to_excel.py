import logging
from typing import AsyncIterator, Dict, Any, Type
from pydantic import BaseModel

from app.ai.tools.base import Tool
from app.ai.tools.metadata import ToolMetadata
from app.ai.tools.officejs_bridge import (
    await_result,
    make_apply_action,
    make_cancel_action,
)
from app.ai.tools.schemas.write_to_excel import WriteToExcelInput, WriteToExcelOutput
from app.ai.tools.schemas.events import (
    ToolEvent,
    ToolStartEvent,
    ToolPartialEvent,
    ToolProgressEvent,
    ToolEndEvent,
)

logger = logging.getLogger(__name__)


class WriteToExcelTool(Tool):
    """Write tabular data directly to the connected Excel spreadsheet.

    Dispatches an applyToExcel action to the taskpane and awaits the ack
    (same bridge as write_officejs_code). Success therefore means the taskpane
    actually wrote the range — the earlier fire-and-forget version reported
    success even when nothing was listening.
    """

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="write_to_excel",
            description=(
                "Write structured tabular data directly into the user's Excel spreadsheet "
                "at their current selection. "
                "Use when the user asks to put, write, insert, or add data to their spreadsheet. "
                "Do NOT use when the user just wants to see data in the chat — use create_data or respond directly instead."
            ),
            category="action",
            version="1.1.0",
            input_schema=WriteToExcelInput.model_json_schema(),
            output_schema=WriteToExcelOutput.model_json_schema(),
            allowed_platforms=["excel"],
            tags=["excel", "spreadsheet", "write"],
            timeout_seconds=60,
        )

    @property
    def input_model(self) -> Type[BaseModel]:
        return WriteToExcelInput

    @property
    def output_model(self) -> Type[BaseModel]:
        return WriteToExcelOutput

    async def run_stream(self, tool_input: Dict[str, Any], runtime_ctx: Dict[str, Any]) -> AsyncIterator[ToolEvent]:
        data = WriteToExcelInput(**tool_input)
        tool_call_id = runtime_ctx.get("tool_call_id")
        system_completion = runtime_ctx.get("system_completion")
        completion_id = str(system_completion.id) if system_completion is not None else None
        sigkill_event = runtime_ctx.get("sigkill_event")

        yield ToolStartEvent(type="tool.start", payload={"title": "Writing to Excel"})

        if not tool_call_id:
            yield ToolEndEvent(
                type="tool.end",
                payload={
                    "output": {"success": False, "error": "Missing tool_call_id in runtime context."},
                    "observation": {
                        "summary": "write_to_excel misconfigured (no tool_call_id)",
                        "success": False,
                    },
                },
            )
            return

        row_count = len(data.rows)
        col_count = len(data.columns)

        # Normalize columns to ensure each has field + headerName
        columns = []
        for c in data.columns:
            if isinstance(c, dict):
                columns.append({
                    "field": c.get("field", c.get("headerName", "")),
                    "headerName": c.get("headerName", c.get("field", "")),
                })
            else:
                columns.append({"field": str(c), "headerName": str(c)})

        yield ToolPartialEvent(
            type="tool.partial",
            payload={
                "excel_action": make_apply_action(
                    tool_call_id=tool_call_id,
                    completion_id=completion_id,
                    columns=columns,
                    rows=data.rows,
                    title=data.title,
                ),
            },
        )

        user = runtime_ctx.get("user") or runtime_ctx.get("current_user")
        result, cancelled, timed_out = await await_result(
            tool_call_id=tool_call_id,
            sigkill_event=sigkill_event,
            completion_id=completion_id,
            user_id=str(user.id) if user is not None else None,
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
                    "observation": {"summary": "write_to_excel cancelled", "success": False},
                },
            )
            return

        if timed_out:
            yield ToolEndEvent(
                type="tool.end",
                payload={
                    "output": {
                        "success": False,
                        "error": (
                            "Timed out waiting for the Excel taskpane to confirm the write. "
                            "The data may not have been written — the taskpane may be closed, "
                            "outdated, or the session may not actually be inside Excel."
                        ),
                    },
                    "observation": {"summary": "write_to_excel timed out (no taskpane ack)", "success": False},
                },
            )
            return

        if not result or not result.get("success"):
            err = (result or {}).get("error") or "No result returned."
            yield ToolEndEvent(
                type="tool.end",
                payload={
                    "output": {"success": False, "error": err},
                    "observation": {
                        "summary": f"write_to_excel failed: {err}",
                        "success": False,
                    },
                },
            )
            return

        rv = result.get("return_value") or {}
        wrote_to = rv.get("wrote_to")

        summary = f"Wrote {row_count} rows x {col_count} columns to Excel"
        if wrote_to:
            summary += f" at {wrote_to}"

        yield ToolEndEvent(
            type="tool.end",
            payload={
                "output": {
                    "success": True,
                    "row_count": row_count,
                    "column_count": col_count,
                    "wrote_to": wrote_to,
                    "error": None,
                },
                "observation": {"summary": summary, "success": True},
            },
        )
