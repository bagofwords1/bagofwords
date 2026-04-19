import json
import logging
import os
import re
from typing import Any, AsyncIterator, Dict, Type
from uuid import uuid4

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
from app.ai.tools.schemas.read_excel_as_csv import (
    ReadExcelAsCsvInput,
    ReadExcelAsCsvOutput,
)

logger = logging.getLogger(__name__)


def _build_read_csv_code(sheet_name: str, a1_range: str, max_rows: int) -> str:
    """Canonical Office.js that reads a range and serializes to CSV inside the taskpane.

    Serializing in the taskpane keeps the return_value small (a single string)
    instead of a nested 2D array that the tool would have to re-serialize.
    """
    params = json.dumps({"sheet": sheet_name, "range": a1_range, "maxRows": max_rows})
    return f"""
const P = {params};
const sheet = ctx.workbook.worksheets.getItem(P.sheet);
const rng = sheet.getRange(P.range);
rng.load(['rowCount','columnCount']);
await ctx.sync();

const rc = rng.rowCount;
const cc = rng.columnCount;
let readRange = rng;
let truncated = false;
if (rc > P.maxRows) {{
  readRange = rng.getRow(0).getResizedRange(P.maxRows - 1, 0);
  truncated = true;
}}
readRange.load(['values']);
await ctx.sync();

const vals = readRange.values;
function csvCell(v) {{
  if (v === null || v === undefined) return '';
  const s = String(v);
  if (/[",\\n\\r]/.test(s)) return '"' + s.replace(/"/g, '""') + '"';
  return s;
}}
const lines = [];
for (let r = 0; r < vals.length; r++) {{
  const row = vals[r];
  const cells = [];
  for (let c = 0; c < row.length; c++) cells.push(csvCell(row[c]));
  lines.push(cells.join(','));
}}
const csv = lines.join('\\n');

return {{
  success: true,
  csv: csv,
  row_count: vals.length,
  col_count: cc,
  total_row_count: rc,
  truncated: truncated,
}};
"""


class ReadExcelAsCsvTool(Tool):
    """Read an Excel range and return it as a CSV string.

    Ideal for feeding data into LLM reasoning or pandas-style analysis. Cheaper
    than read_excel_range when you just need the data and don't care about
    formulas — the range is serialized inside the taskpane so the return value
    is a single string instead of a nested array.
    """

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="read_excel_as_csv",
            description=(
                "Read an Excel range and return it as a CSV string. Best for analyzing "
                "a table end-to-end (pass the full range including header row). For a "
                "handful of cells or formula inspection, use read_excel_range. For "
                "writes, use write_to_excel or write_officejs_code."
            ),
            category="research",
            version="1.0.0",
            input_schema=ReadExcelAsCsvInput.model_json_schema(),
            output_schema=ReadExcelAsCsvOutput.model_json_schema(),
            allowed_platforms=["excel"],
            tags=["excel", "spreadsheet", "read", "csv"],
            timeout_seconds=60,
        )

    @property
    def input_model(self) -> Type[BaseModel]:
        return ReadExcelAsCsvInput

    @property
    def output_model(self) -> Type[BaseModel]:
        return ReadExcelAsCsvOutput

    async def run_stream(
        self,
        tool_input: Dict[str, Any],
        runtime_ctx: Dict[str, Any],
    ) -> AsyncIterator[ToolEvent]:
        data = ReadExcelAsCsvInput(**tool_input)
        tool_call_id = runtime_ctx.get("tool_call_id")
        system_completion = runtime_ctx.get("system_completion")
        completion_id = str(system_completion.id) if system_completion is not None else None
        sigkill_event = runtime_ctx.get("sigkill_event")

        yield ToolStartEvent(
            type="tool.start",
            payload={"title": f"Reading {data.sheet_name}!{data.range} as CSV"},
        )

        if not tool_call_id:
            yield ToolEndEvent(
                type="tool.end",
                payload={
                    "output": {"success": False, "error": "Missing tool_call_id in runtime context."},
                    "observation": {
                        "summary": "read_excel_as_csv misconfigured (no tool_call_id)",
                        "success": False,
                    },
                },
            )
            return

        code = _build_read_csv_code(data.sheet_name, data.range, data.max_rows)
        description = f"Read {data.sheet_name}!{data.range} as CSV"

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
                    "observation": {"summary": "read_excel_as_csv cancelled", "success": False},
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
                    "observation": {"summary": "read_excel_as_csv timed out", "success": False},
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
                        "summary": f"read_excel_as_csv failed: {err}",
                        "success": False,
                    },
                },
            )
            return

        rv = result.get("return_value") or {}
        csv = rv.get("csv") or ""
        row_count = int(rv.get("row_count") or 0)
        col_count = int(rv.get("col_count") or 0)
        total_row_count = int(rv.get("total_row_count") or row_count)
        truncated = bool(rv.get("truncated"))

        file_id, file_name = await _persist_csv_as_file(
            csv=csv,
            sheet_name=data.sheet_name,
            a1_range=data.range,
            runtime_ctx=runtime_ctx,
        )

        output = {
            "success": True,
            "csv": csv,
            "row_count": row_count,
            "col_count": col_count,
            "truncated": truncated,
            "file_id": file_id,
            "file_name": file_name,
            "error": None,
        }

        summary = f"Read {row_count}×{col_count} as CSV from {data.sheet_name}!{data.range}"
        if truncated:
            summary += f" — TRUNCATED ({row_count}/{total_row_count} rows)"
        if file_id:
            summary += f" (file_id: {file_id})"

        yield ToolEndEvent(
            type="tool.end",
            payload={
                "output": output,
                "observation": {
                    "summary": summary,
                    "success": True,
                    "file_id": file_id,
                    "file_name": file_name,
                },
            },
        )


_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_name_fragment(s: str, limit: int = 40) -> str:
    cleaned = _SAFE_NAME_RE.sub("_", (s or "").strip()).strip("_")
    return (cleaned[:limit] or "excel")


async def _persist_csv_as_file(
    csv: str,
    sheet_name: str,
    a1_range: str,
    runtime_ctx: Dict[str, Any],
) -> tuple[str | None, str | None]:
    """Write the CSV to uploads/files, create a File row, and link it to the report.

    Returns (file_id, file_name). Returns (None, None) and logs on any failure —
    the read itself already succeeded, so persistence is best-effort.
    """
    db = runtime_ctx.get("db")
    report = runtime_ctx.get("report")
    organization = runtime_ctx.get("organization")
    user = runtime_ctx.get("user") or runtime_ctx.get("current_user")

    if db is None or user is None or organization is None:
        logger.info(
            "read_excel_as_csv: skipping file persistence (missing db/user/organization)"
        )
        return None, None

    from app.models.file import File
    from app.models.report_file_association import report_file_association
    from app.services.file_preview import _preview_csv
    from sqlalchemy import insert

    display_name = (
        f"excel_{_safe_name_fragment(sheet_name)}_{_safe_name_fragment(a1_range)}.csv"
    )
    unique_name = f"{uuid4()}_{display_name}"
    uploads_dir = os.path.join("uploads", "files")
    os.makedirs(uploads_dir, exist_ok=True)
    dest_path = os.path.join(uploads_dir, unique_name)

    try:
        with open(dest_path, "w", encoding="utf-8", newline="") as fh:
            fh.write(csv)
    except Exception as e:
        logger.warning("read_excel_as_csv: failed to write CSV to %s: %s", dest_path, e)
        return None, None

    preview = None
    try:
        preview = _preview_csv(dest_path, display_name)
    except Exception:
        preview = None

    try:
        file = File(
            filename=display_name,
            path=dest_path,
            content_type="text/csv",
            preview=preview,
            user_id=str(user.id),
            organization_id=str(organization.id),
        )
        db.add(file)
        await db.flush()

        if report is not None:
            await db.execute(
                insert(report_file_association).values(
                    report_id=str(report.id),
                    file_id=str(file.id),
                )
            )
            await db.flush()

        return str(file.id), display_name
    except Exception as e:
        logger.warning("read_excel_as_csv: failed to persist File row: %s", e)
        try:
            os.remove(dest_path)
        except Exception:
            pass
        return None, None
