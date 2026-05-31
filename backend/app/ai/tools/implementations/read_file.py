"""read_file agent tool — read a file from a file-based data source."""
from __future__ import annotations

from typing import Any, AsyncIterator, Dict, Type

from pydantic import BaseModel

from app.ai.tools.base import Tool
from app.ai.tools.metadata import ToolMetadata
from app.ai.tools.schemas import ToolEndEvent, ToolEvent, ToolStartEvent
from app.ai.tools.schemas.file_tools import ReadFileInput, ReadFileOutput
from app.data_sources.clients.base import Capability

from ._file_tool_common import (
    attach_drive_file_to_session,
    render_file_payload,
    resolve_file_client,
)


class ReadFileTool(Tool):
    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="read_file",
            description=(
                "Read a file from a SharePoint / OneDrive / Google Drive connection "
                "AND attach it to the current conversation as a session file. "
                "USE THIS — not inspect_data — whenever you need to analyze a file "
                "that came from list_files or search_files on a Drive connection. "
                "Tabular files (CSV, Excel, Google Sheets) are returned as CSV plus "
                "a `session_file_id` you can pass to inspect_data / create_data / "
                "read_excel_as_csv exactly like an uploaded file. Text and JSON "
                "are returned inline. Binary files return their size only."
            ),
            category="research",
            input_schema=ReadFileInput.model_json_schema(),
            output_schema=ReadFileOutput.model_json_schema(),
            idempotent=True,
            timeout_seconds=60,
            tags=["files", "sharepoint", "onedrive", "drive", "read"],
            requires_capability="read_file",
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

        # Persist the file as a session attachment so the existing analysis
        # stack (inspect_data, read_excel_as_csv, create_data) can pick it up.
        # We serialize from the parsed payload — no second download. For
        # tabular files this means xlsx loses its sheet structure (becomes
        # csv); for analysis that's exactly what those tools want anyway.
        session_file_id = await _persist_session_file(
            runtime_ctx, file_id=data.file_id, payload=payload,
        )

        output = {
            "success": True,
            "connection_id": data.connection_id,
            "file_id": data.file_id,
            **rendered,
        }
        if session_file_id:
            output["session_file_id"] = session_file_id
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


async def _persist_session_file(
    runtime_ctx: Dict[str, Any], *, file_id: str, payload: Any,
) -> Optional[str]:
    """Serialize the parsed payload back to bytes + attach to current report.

    Tabular  → CSV bytes, filename `<file_id>.csv`
    Text/JSON → utf-8 bytes, filename `<file_id>.txt` or `.json`
    Binary   → raw bytes, filename `<file_id>.bin`

    Returns the resulting session File id, or None if attach was skipped.
    """
    import io
    import json
    import pandas as pd

    name: str
    content: bytes
    mime: Optional[str] = None

    if isinstance(payload, pd.DataFrame):
        buf = io.StringIO()
        payload.to_csv(buf, index=False)
        content = buf.getvalue().encode("utf-8")
        name = f"{file_id}.csv"
        mime = "text/csv"
    elif isinstance(payload, (dict, list)):
        content = json.dumps(payload, default=str, ensure_ascii=False).encode("utf-8")
        name = f"{file_id}.json"
        mime = "application/json"
    elif isinstance(payload, str):
        content = payload.encode("utf-8")
        name = f"{file_id}.txt"
        mime = "text/plain"
    elif isinstance(payload, (bytes, bytearray)):
        content = bytes(payload)
        name = f"{file_id}.bin"  # _ATTACHABLE_BY_EXT skips .bin → won't persist
    else:
        return None

    return await attach_drive_file_to_session(
        runtime_ctx, filename=name, content_bytes=content, mime_type=mime,
    )
