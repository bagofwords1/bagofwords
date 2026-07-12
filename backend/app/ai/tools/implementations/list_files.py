"""list_files agent tool — read the agent's cached file catalog.

Reads from the persisted catalog (DataSourceTable + per-user UserOverlayTable)
rather than calling the upstream client. Mirrors how SQL data sources work —
the schema is cached after refresh, and reads don't re-query the source on
every tool call. For files that aren't in the cache yet (e.g. just uploaded),
use search_files; refresh of the cache happens on the explicit /refresh_schema
button or post-OAuth.
"""
from __future__ import annotations

import fnmatch
from typing import Any, AsyncIterator, Dict, Type

from pydantic import BaseModel

from app.ai.tools.base import Tool
from app.ai.tools.metadata import ToolMetadata
from app.ai.tools.schemas import ToolEndEvent, ToolEvent, ToolStartEvent
from app.ai.tools.schemas.file_tools import FileEntry, ListFilesInput, ListFilesOutput

from ._file_tool_common import resolve_file_data_source

_MAX_RESULTS = 500

# Connector-specific metadata keys on the cached Table's metadata_json.
# Keep both so a future ds_type doesn't need a code change — just stash
# under a new sub-key and add it here.
_FILE_METADATA_KEYS = ("graph", "google_drive", "network_dir", "s3")


class ListFilesTool(Tool):
    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="list_files",
            description=(
                "List files in this agent's cached SharePoint / OneDrive / "
                "Google Drive catalog. Reads from the schema cache — fast, "
                "no API calls, returns up to 500 files. Use this for "
                "discovery / browsing. For a file that was just uploaded or "
                "isn't in the cache, use search_files (live API). To filter "
                "by filename, pass name_pattern (glob: '*.xlsx', 'Book *')."
            ),
            category="research",
            input_schema=ListFilesInput.model_json_schema(),
            output_schema=ListFilesOutput.model_json_schema(),
            idempotent=True,
            timeout_seconds=15,
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

        data_source, err = await resolve_file_data_source(runtime_ctx, data.connection_id)
        if err:
            yield ToolEndEvent(type="tool.end", payload={
                "output": {"success": False, "connection_id": data.connection_id, "error": err},
                "observation": {"summary": err, "success": False},
            })
            return

        # Read the cached catalog. get_data_source_schema picks the right
        # path internally — UserOverlayTable for user_required, DataSourceTable
        # for shared/system_only. Both round-trip the file's metadata_json
        # (which carries file_id, mime_type, size, modified_at, web_url).
        try:
            from app.services.data_source_service import DataSourceService
            tables = await DataSourceService().get_data_source_schema(
                db=runtime_ctx.get("db"),
                data_source_id=str(data_source.id),
                organization=runtime_ctx.get("organization"),
                current_user=runtime_ctx.get("user"),
                include_inactive=True,
            )
        except Exception as e:
            err = f"Failed to read cached catalog: {e}"
            yield ToolEndEvent(type="tool.end", payload={
                "output": {"success": False, "connection_id": data.connection_id, "error": err},
                "observation": {"summary": err, "success": False},
            })
            return

        files = []
        for t in (tables or []):
            meta_json = getattr(t, "metadata_json", None) or {}
            sub = next((meta_json.get(k) for k in _FILE_METADATA_KEYS if meta_json.get(k)), {}) or {}
            name = t.name
            if data.name_pattern and not fnmatch.fnmatch(name.lower(), data.name_pattern.lower()):
                continue
            files.append({
                "id": sub.get("file_id") or t.name,
                "name": name,
                "path": name,
                "mime_type": sub.get("mime_type"),
                "size": sub.get("size"),
                "modified_at": sub.get("modified_at"),
                "web_url": sub.get("web_url"),
            })

        truncated = len(files) > _MAX_RESULTS
        if truncated:
            files = files[:_MAX_RESULTS]

        entries = [FileEntry(**f).model_dump() for f in files]

        hint = ""
        if not entries:
            hint = (
                " Catalog is empty — try search_files for files outside the "
                "cache, or run a refresh."
            )

        yield ToolEndEvent(type="tool.end", payload={
            "output": {
                "success": True,
                "connection_id": data.connection_id,
                "file_count": len(entries),
                "files": entries,
                "truncated": truncated,
            },
            "observation": {
                "summary": (
                    f"Listed {len(entries)} file(s) from cache"
                    + (f" (capped at {_MAX_RESULTS})" if truncated else "")
                    + hint
                ),
                "success": True,
            },
        })
