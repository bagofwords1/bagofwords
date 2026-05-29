"""read_file agent tool — read a file from a file-based data source."""
from __future__ import annotations

from typing import Any, AsyncIterator, Dict, Type

from pydantic import BaseModel

from app.ai.tools.base import Tool
from app.ai.tools.metadata import ToolMetadata
from app.ai.tools.schemas import ToolEndEvent, ToolEvent, ToolStartEvent
from app.ai.tools.schemas.file_tools import ReadFileInput, ReadFileOutput
from app.data_sources.clients.base import Capability

from ._file_tool_common import render_file_payload, resolve_file_client


class ReadFileTool(Tool):
    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="read_file",
            description=(
                "Read a file from a SharePoint, OneDrive, or Google Drive data source. "
                "Tabular files (CSV, Excel, Google Sheets) are returned as CSV. Text "
                "and JSON files are returned as text. Binary files return only their "
                "size — feed them through a more specific reader if you need their "
                "contents. Use list_files or search_files first to obtain a file_id."
            ),
            category="research",
            input_schema=ReadFileInput.model_json_schema(),
            output_schema=ReadFileOutput.model_json_schema(),
            idempotent=True,
            timeout_seconds=60,
            tags=["files", "sharepoint", "onedrive", "drive", "read"],
        )

    @property
    def input_model(self) -> Type[BaseModel]:
        return ReadFileInput

    @property
    def output_model(self) -> Type[BaseModel]:
        return ReadFileOutput

    async def run_stream(
        self, tool_input: Dict[str, Any], runtime_ctx: Dict[str, Any]
    ) -> AsyncIterator[ToolEvent]:
        data = ReadFileInput(**tool_input)
        yield ToolStartEvent(type="tool.start", payload={
            "title": f"Reading file {data.file_id}",
            "connection_id": data.connection_id,
        })

        client, err = await resolve_file_client(
            runtime_ctx, data.connection_id, Capability.READ_FILE
        )
        if err:
            yield ToolEndEvent(type="tool.end", payload={
                "output": {
                    "success": False,
                    "connection_id": data.connection_id,
                    "file_id": data.file_id,
                    "error": err,
                },
                "observation": {"summary": err, "success": False},
            })
            return

        try:
            payload = await client.aread_file(data.file_id, sheet=data.sheet)
        except Exception as e:
            err = f"read_file failed: {e}"
            yield ToolEndEvent(type="tool.end", payload={
                "output": {
                    "success": False,
                    "connection_id": data.connection_id,
                    "file_id": data.file_id,
                    "error": err,
                },
                "observation": {"summary": err, "success": False},
            })
            return

        rendered = render_file_payload(
            name=None, payload=payload, max_rows=data.max_rows, max_chars=data.max_chars
        )

        output = {
            "success": True,
            "connection_id": data.connection_id,
            "file_id": data.file_id,
            **rendered,
        }
        # render_file_payload set file_name=None; remove if empty
        if output.get("file_name") is None:
            output.pop("file_name", None)

        summary_bits = [f"Read {data.file_id}", rendered.get("content_type", "?")]
        if rendered.get("content_type") == "tabular":
            summary_bits.append(f"{rendered.get('row_count')} rows × {rendered.get('col_count')} cols")
        if rendered.get("truncated"):
            summary_bits.append("(truncated)")

        yield ToolEndEvent(type="tool.end", payload={
            "output": output,
            "observation": {"summary": " — ".join(summary_bits), "success": True},
        })
