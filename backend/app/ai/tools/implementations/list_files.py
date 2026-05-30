"""list_files agent tool — enumerate files in a file-based data source."""
from __future__ import annotations

from typing import Any, AsyncIterator, Dict, Type

from pydantic import BaseModel

from app.ai.tools.base import Tool
from app.ai.tools.metadata import ToolMetadata
from app.ai.tools.schemas import ToolEndEvent, ToolEvent, ToolStartEvent
from app.ai.tools.schemas.file_tools import FileEntry, ListFilesInput, ListFilesOutput
from app.data_sources.clients.base import Capability

from ._file_tool_common import resolve_file_client

_MAX_RESULTS = 500


class ListFilesTool(Tool):
    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="list_files",
            description=(
                "List files available in a SharePoint, OneDrive, or Google Drive data "
                "source. Returns file IDs, names, paths, types, sizes, and modified "
                "timestamps for the connection's configured folder scope. Use the "
                "returned file IDs with read_file. Use this before read_file when you "
                "need to discover what's available; if you already have a file ID from "
                "an earlier message, skip this and call read_file directly."
            ),
            category="research",
            input_schema=ListFilesInput.model_json_schema(),
            output_schema=ListFilesOutput.model_json_schema(),
            idempotent=True,
            timeout_seconds=30,
            tags=["files", "sharepoint", "onedrive", "drive", "list"],
            requires_capability="list_files",
        )

    @property
    def input_model(self) -> Type[BaseModel]:
        return ListFilesInput

    @property
    def output_model(self) -> Type[BaseModel]:
        return ListFilesOutput

    async def run_stream(
        self, tool_input: Dict[str, Any], runtime_ctx: Dict[str, Any]
    ) -> AsyncIterator[ToolEvent]:
        data = ListFilesInput(**tool_input)
        yield ToolStartEvent(type="tool.start", payload={
            "title": "Listing files",
            "connection_id": data.connection_id,
        })

        client, err = await resolve_file_client(
            runtime_ctx, data.connection_id, Capability.LIST_FILES
        )
        if err:
            yield ToolEndEvent(type="tool.end", payload={
                "output": {"success": False, "connection_id": data.connection_id, "error": err},
                "observation": {"summary": err, "success": False},
            })
            return

        try:
            files = await client.alist_files(folder_id=data.folder_id, recursive=data.recursive)
            if data.name_pattern:
                import fnmatch
                pat = data.name_pattern.lower()
                files = [f for f in files if fnmatch.fnmatch(str(f.get("name", "")).lower(), pat)]
        except Exception as e:
            err = f"list_files failed: {e}"
            yield ToolEndEvent(type="tool.end", payload={
                "output": {"success": False, "connection_id": data.connection_id, "error": err},
                "observation": {"summary": err, "success": False},
            })
            return

        truncated = len(files) > _MAX_RESULTS
        if truncated:
            files = files[:_MAX_RESULTS]

        entries = [FileEntry(
            id=f.get("id"),
            name=f.get("name"),
            path=f.get("path"),
            mime_type=f.get("mime_type"),
            size=f.get("size"),
            modified_at=f.get("modified_at"),
            web_url=f.get("web_url"),
        ).model_dump() for f in files]

        yield ToolEndEvent(type="tool.end", payload={
            "output": {
                "success": True,
                "connection_id": data.connection_id,
                "file_count": len(entries),
                "files": entries,
                "truncated": truncated,
            },
            "observation": {
                "summary": f"Listed {len(entries)} file(s)"
                           + (f" (truncated at {_MAX_RESULTS})" if truncated else ""),
                "success": True,
            },
        })
