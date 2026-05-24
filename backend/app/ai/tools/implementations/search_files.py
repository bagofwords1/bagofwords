"""search_files agent tool — free-text search over a file-based data source."""
from __future__ import annotations

from typing import Any, AsyncIterator, Dict, Type

from pydantic import BaseModel

from app.ai.tools.base import Tool
from app.ai.tools.metadata import ToolMetadata
from app.ai.tools.schemas import ToolEndEvent, ToolEvent, ToolStartEvent
from app.ai.tools.schemas.file_tools import FileEntry, SearchFilesInput, SearchFilesOutput
from app.data_sources.clients.base import Capability

from ._file_tool_common import resolve_file_client


class SearchFilesTool(Tool):
    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="search_files",
            description=(
                "Search files by free-text query in a SharePoint, OneDrive, or Google "
                "Drive data source. Use when the user references a file by partial name "
                "or topic and you don't already have its ID. Returns up to ~50 matches "
                "with their IDs — pass an ID to read_file to fetch contents."
            ),
            category="research",
            input_schema=SearchFilesInput.model_json_schema(),
            output_schema=SearchFilesOutput.model_json_schema(),
            idempotent=True,
            timeout_seconds=30,
            tags=["files", "sharepoint", "onedrive", "drive", "search"],
        )

    @property
    def input_model(self) -> Type[BaseModel]:
        return SearchFilesInput

    @property
    def output_model(self) -> Type[BaseModel]:
        return SearchFilesOutput

    async def run_stream(
        self, tool_input: Dict[str, Any], runtime_ctx: Dict[str, Any]
    ) -> AsyncIterator[ToolEvent]:
        data = SearchFilesInput(**tool_input)
        yield ToolStartEvent(type="tool.start", payload={
            "title": f"Searching files: {data.query!r}",
            "data_source_id": data.data_source_id,
        })

        client, err = await resolve_file_client(
            runtime_ctx, data.data_source_id, Capability.SEARCH_FILES
        )
        if err:
            yield ToolEndEvent(type="tool.end", payload={
                "output": {
                    "success": False,
                    "data_source_id": data.data_source_id,
                    "query": data.query,
                    "error": err,
                },
                "observation": {"summary": err, "success": False},
            })
            return

        try:
            files = await client.asearch_files(data.query)
        except Exception as e:
            err = f"search_files failed: {e}"
            yield ToolEndEvent(type="tool.end", payload={
                "output": {
                    "success": False,
                    "data_source_id": data.data_source_id,
                    "query": data.query,
                    "error": err,
                },
                "observation": {"summary": err, "success": False},
            })
            return

        files = files[: data.max_results]
        entries = [FileEntry(
            id=f.get("id"),
            name=f.get("name"),
            path=f.get("path") if isinstance(f.get("path"), str) else None,
            mime_type=f.get("mime_type"),
            size=f.get("size"),
            modified_at=f.get("modified_at"),
            web_url=f.get("web_url"),
        ).model_dump() for f in files]

        yield ToolEndEvent(type="tool.end", payload={
            "output": {
                "success": True,
                "data_source_id": data.data_source_id,
                "query": data.query,
                "file_count": len(entries),
                "files": entries,
            },
            "observation": {
                "summary": f"Found {len(entries)} file(s) matching '{data.query}'",
                "success": True,
            },
        })
