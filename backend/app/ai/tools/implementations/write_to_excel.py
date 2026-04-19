import logging
from typing import AsyncIterator, Dict, Any, Type
from pydantic import BaseModel

from app.ai.tools.base import Tool
from app.ai.tools.metadata import ToolMetadata
from app.ai.tools.schemas.write_to_excel import WriteToExcelInput, WriteToExcelOutput
from app.ai.tools.schemas.events import (
    ToolEvent,
    ToolStartEvent,
    ToolProgressEvent,
    ToolEndEvent,
)

logger = logging.getLogger(__name__)


class WriteToExcelTool(Tool):
    """Write tabular data directly to the connected Excel spreadsheet."""

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="write_to_excel",
            description=(
                "Write structured tabular data directly into the user's Excel spreadsheet. "
                "Use when the user asks to put, write, insert, or add data to their spreadsheet. "
                "Do NOT use when the user just wants to see data in the chat — use answer_question or create_data instead."
            ),
            category="action",
            version="1.0.0",
            input_schema=WriteToExcelInput.model_json_schema(),
            output_schema=WriteToExcelOutput.model_json_schema(),
            allowed_platforms=["excel"],
            tags=["excel", "spreadsheet", "write"],
            timeout_seconds=30,
        )

    @property
    def input_model(self) -> Type[BaseModel]:
        return WriteToExcelInput

    @property
    def output_model(self) -> Type[BaseModel]:
        return WriteToExcelOutput

    async def run_stream(self, tool_input: Dict[str, Any], runtime_ctx: Dict[str, Any]) -> AsyncIterator[ToolEvent]:
        data = WriteToExcelInput(**tool_input)

        yield ToolStartEvent(type="tool.start", payload={"title": "Writing to Excel"})
        yield ToolProgressEvent(type="tool.progress", payload={"stage": "preparing_data"})

        try:
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

            # The excel_action payload matches the structure expected by the
            # taskpane's appendDataToExcel() via the applyToExcel postMessage protocol.
            yield ToolEndEvent(
                type="tool.end",
                payload={
                    "output": {
                        "success": True,
                        "row_count": row_count,
                        "column_count": col_count,
                        "excel_action": {
                            "type": "applyToExcel",
                            "data": {
                                "widget": {
                                    "last_step": {
                                        "data": {
                                            "columns": columns,
                                            "rows": data.rows,
                                        }
                                    }
                                }
                            },
                        },
                    },
                    "observation": {
                        "summary": f"Wrote {row_count} rows x {col_count} columns to Excel",
                        "success": True,
                    },
                },
            )
        except Exception as e:
            logger.error(f"write_to_excel failed: {e}", exc_info=True)
            yield ToolEndEvent(
                type="tool.end",
                payload={
                    "output": {
                        "success": False,
                        "error_message": str(e),
                    },
                    "observation": {
                        "summary": f"write_to_excel failed: {e}",
                        "success": False,
                    },
                },
            )
