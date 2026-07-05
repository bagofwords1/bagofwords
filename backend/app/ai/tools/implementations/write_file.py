"""write_file agent tool — create / copy a file into a writable file source.

Backs the "put all related files in one place" use case: the agent can write
text it generated, or copy a session file it found (via read_file's
`session_file_id`, or an uploaded file) into a writable network directory.

Only connections that expose the WRITE_FILE capability accept this — a
read-only file source is rejected at resolution time.
"""
from __future__ import annotations

import logging
from typing import Any, AsyncIterator, Dict, Optional, Type

import aiofiles
from pydantic import BaseModel
from sqlalchemy import select

from app.ai.tools.base import Tool
from app.ai.tools.metadata import ToolMetadata
from app.ai.tools.schemas import ToolEndEvent, ToolEvent, ToolStartEvent
from app.ai.tools.schemas.file_tools import FileEntry, WriteFileInput, WriteFileOutput
from app.data_sources.clients.base import Capability

from ._file_tool_common import resolve_file_client

logger = logging.getLogger(__name__)


class WriteFileTool(Tool):
    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="write_file",
            description=(
                "Write or copy a file INTO a writable file connection (e.g. a "
                "network directory). Two modes: pass `content` to write text you "
                "generated (CSV, markdown, notes), OR pass `source_file_id` (a "
                "session_file_id from read_file, or an uploaded file id) to copy "
                "an existing file into the directory — this is how you 'put' "
                "related files together (cp / put). Use `folder` + `filename` to "
                "place it, `overwrite` to replace an existing file. Only works on "
                "connections that have writes enabled."
            ),
            category="action",
            input_schema=WriteFileInput.model_json_schema(),
            output_schema=WriteFileOutput.model_json_schema(),
            idempotent=False,
            timeout_seconds=60,
            tags=["files", "network_dir", "write", "copy", "put"],
            requires_capability="write_file",
        )

    @property
    def input_model(self) -> Type[BaseModel]:
        return WriteFileInput

    @property
    def output_model(self) -> Type[BaseModel]:
        return WriteFileOutput

    async def run_stream(
        self, tool_input: Dict[str, Any], runtime_ctx: Dict[str, Any]
    ) -> AsyncIterator[ToolEvent]:
        data = WriteFileInput(**tool_input)
        yield ToolStartEvent(type="tool.start", payload={
            "title": f"Writing file {data.filename}",
            "connection_id": data.connection_id,
        })

        def _fail(msg: str) -> ToolEndEvent:
            return ToolEndEvent(type="tool.end", payload={
                "output": {"success": False, "connection_id": data.connection_id, "error": msg},
                "observation": {"summary": msg, "success": False},
            })

        # Exactly one content source.
        if bool(data.content) == bool(data.source_file_id):
            yield _fail("Provide exactly one of `content` or `source_file_id`.")
            return

        client, err = await resolve_file_client(
            runtime_ctx, data.connection_id, Capability.WRITE_FILE
        )
        if err:
            yield _fail(err)
            return

        # Resolve the bytes/text to write.
        payload: Any
        if data.content is not None:
            payload = data.content
        else:
            payload, err = await self._load_session_file_bytes(runtime_ctx, data.source_file_id)
            if err:
                yield _fail(err)
                return

        try:
            written = await client.awrite_file(
                data.filename, payload, folder_id=data.folder, overwrite=data.overwrite
            )
        except Exception as e:
            yield _fail(f"write_file failed: {e}")
            return

        entry = FileEntry(
            id=written.get("id"),
            name=written.get("name"),
            path=written.get("path") if isinstance(written.get("path"), str) else None,
            mime_type=written.get("mime_type"),
            size=written.get("size"),
            modified_at=written.get("modified_at"),
            web_url=written.get("web_url"),
        ).model_dump()

        yield ToolEndEvent(type="tool.end", payload={
            "output": {"success": True, "connection_id": data.connection_id, "file": entry},
            "observation": {
                "summary": f"Wrote {entry['name']} ({entry.get('size')} bytes) to {data.connection_id}",
                "success": True,
            },
        })

    async def _load_session_file_bytes(
        self, runtime_ctx: Dict[str, Any], source_file_id: str
    ) -> tuple[Optional[bytes], Optional[str]]:
        """Load raw bytes for an existing session File (upload or connector
        attachment) so it can be copied into the destination connection."""
        db = runtime_ctx.get("db")
        organization = runtime_ctx.get("organization")
        if not db or not organization:
            return None, "Missing database/organization context."
        from app.models.file import File

        result = await db.execute(
            select(File).where(
                File.id == str(source_file_id),
                File.organization_id == str(organization.id),
            )
        )
        f = result.scalar_one_or_none()
        if not f:
            return None, f"source_file_id '{source_file_id}' not found."
        try:
            async with aiofiles.open(f.path, "rb") as fh:
                return await fh.read(), None
        except Exception as e:
            return None, f"Could not read source file: {e}"
