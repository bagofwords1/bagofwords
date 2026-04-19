import json
import logging
from typing import Any, AsyncIterator, Dict, Type

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
from app.ai.tools.schemas.read_excel_range import (
    ReadExcelRangeInput,
    ReadExcelRangeOutput,
)

logger = logging.getLogger(__name__)


def _build_read_range_code(
    sheet_name: str,
    ranges: list[str],
    include_formulas: bool,
    cell_limit: int,
) -> str:
    """Canonical Office.js for reading ranges — parameters are injected as JSON."""
    params = json.dumps({
        "sheet": sheet_name,
        "ranges": ranges,
        "includeFormulas": include_formulas,
        "cellLimit": cell_limit,
    })
    return f"""
const P = {params};
const sheet = ctx.workbook.worksheets.getItem(P.sheet);
const objs = P.ranges.map(a => sheet.getRange(a));
for (const o of objs) {{ o.load(['rowCount','columnCount']); }}
await ctx.sync();

let total = 0;
let truncated = false;
const toLoad = [];
for (let i = 0; i < objs.length; i++) {{
  const rc = objs[i].rowCount;
  const cc = objs[i].columnCount;
  if (total + rc * cc > P.cellLimit) {{ truncated = true; continue; }}
  total += rc * cc;
  const fields = ['address','values','rowCount','columnCount'];
  if (P.includeFormulas) fields.push('formulas');
  objs[i].load(fields);
  toLoad.push(i);
}}
await ctx.sync();

const results = [];
for (const i of toLoad) {{
  const o = objs[i];
  const item = {{
    address: o.address,
    row_count: o.rowCount,
    col_count: o.columnCount,
    values: o.values,
  }};
  if (P.includeFormulas) item.formulas = o.formulas;
  results.push(item);
}}

return {{ success: true, ranges: results, truncated, total_cells: total }};
"""


class ReadExcelRangeTool(Tool):
    """Read one or more A1-notation ranges from the user's Excel spreadsheet.

    The Office.js body is generated server-side from structured inputs, so the
    LLM never writes the JS and the class of missing-`.load()` / wrong-method
    bugs doesn't exist here. Use this instead of write_officejs_code whenever
    you only need to READ cells — it's cheaper, safer, and faster.
    """

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="read_excel_range",
            description=(
                "Read cell values (and optionally formulas) from one or more A1 ranges "
                "in the user's Excel spreadsheet. Prefer this over write_officejs_code "
                "for reads — it is cheaper, safer, and doesn't risk Office.js timeouts "
                "from LLM-authored JS. Use for inspecting known ranges by address. For "
                "whole-table exports prefer read_excel_as_csv."
            ),
            category="research",
            version="1.0.0",
            input_schema=ReadExcelRangeInput.model_json_schema(),
            output_schema=ReadExcelRangeOutput.model_json_schema(),
            allowed_platforms=["excel"],
            tags=["excel", "spreadsheet", "read"],
            timeout_seconds=60,
        )

    @property
    def input_model(self) -> Type[BaseModel]:
        return ReadExcelRangeInput

    @property
    def output_model(self) -> Type[BaseModel]:
        return ReadExcelRangeOutput

    async def run_stream(
        self,
        tool_input: Dict[str, Any],
        runtime_ctx: Dict[str, Any],
    ) -> AsyncIterator[ToolEvent]:
        data = ReadExcelRangeInput(**tool_input)
        tool_call_id = runtime_ctx.get("tool_call_id")
        system_completion = runtime_ctx.get("system_completion")
        completion_id = str(system_completion.id) if system_completion is not None else None
        sigkill_event = runtime_ctx.get("sigkill_event")

        yield ToolStartEvent(
            type="tool.start",
            payload={"title": f"Reading {len(data.ranges)} range(s) from '{data.sheet_name}'"},
        )

        if not tool_call_id:
            yield ToolEndEvent(
                type="tool.end",
                payload={
                    "output": {"success": False, "error": "Missing tool_call_id in runtime context."},
                    "observation": {
                        "summary": "read_excel_range misconfigured (no tool_call_id)",
                        "success": False,
                    },
                },
            )
            return

        code = _build_read_range_code(
            data.sheet_name,
            data.ranges,
            data.include_formulas,
            data.cell_limit,
        )
        description = f"Read {len(data.ranges)} range(s) from {data.sheet_name}"

        yield ToolPartialEvent(
            type="tool.partial",
            payload={
                "excel_action": make_run_action(
                    tool_call_id=tool_call_id,
                    code=code,
                    description=description,
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
                    "observation": {"summary": "read_excel_range cancelled", "success": False},
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
                    "observation": {"summary": "read_excel_range timed out", "success": False},
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
                        "summary": f"read_excel_range failed: {err}",
                        "success": False,
                    },
                },
            )
            return

        rv = result.get("return_value") or {}
        ranges_payload = rv.get("ranges") or []
        truncated = bool(rv.get("truncated"))
        total_cells = rv.get("total_cells")

        output = {
            "success": True,
            "ranges": ranges_payload,
            "truncated": truncated,
            "error": None,
        }

        addresses = ", ".join(r.get("address", "?") for r in ranges_payload[:3])
        if len(ranges_payload) > 3:
            addresses += f" (+{len(ranges_payload) - 3} more)"
        summary = f"Read {len(ranges_payload)} range(s)"
        if total_cells is not None:
            summary += f", {total_cells} cells"
        if addresses:
            summary += f": {addresses}"
        if truncated:
            summary += " — TRUNCATED (cell_limit hit)"

        yield ToolEndEvent(
            type="tool.end",
            payload={
                "output": output,
                "observation": {"summary": summary, "success": True},
            },
        )
