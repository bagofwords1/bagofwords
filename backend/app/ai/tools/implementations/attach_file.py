"""attach_file agent tool — materialize files from a connection into the report.

Pulls one or more files out of a Files & Directories (or other file-source)
connection and attaches them to the current report as **durable** files — the
same kind of File a user upload produces. Unlike read_file (which materializes a
file ephemerally for the current turn's analysis), attached files persist on the
report: they show in the report's file list, are downloadable, and can be passed
to inspect_data / create_data / read_excel_as_csv.

Reads the ORIGINAL bytes where the client exposes them (a real .pdf / .xlsx),
so the attached file isn't a reparsed/serialized copy.
"""
from __future__ import annotations

import logging
import os
import uuid
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple, Type

import aiofiles
from pydantic import BaseModel
from sqlalchemy import select

from app.ai.tools.base import Tool
from app.ai.tools.metadata import ToolMetadata
from app.ai.tools.schemas import ToolEndEvent, ToolEvent, ToolStartEvent
from app.ai.tools.schemas.file_tools import AttachedFile, AttachFileInput, AttachFileOutput
from app.data_sources.clients.base import Capability

from ._file_tool_common import resolve_file_client

logger = logging.getLogger(__name__)


class AttachFileTool(Tool):
    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="attach_file",
            description=(
                "Attach one or more files from a Files & Directories (or "
                "SharePoint / OneDrive / Drive) connection to the current report "
                "as durable files — they show in the report, are downloadable, "
                "and can be analyzed with inspect_data / create_data. Pass "
                "file_ids from list_files or search_files. Use this to 'collect' "
                "or 'attach' files (e.g. gather related contracts into the "
                "report). For reading a single file's contents inline, use "
                "read_file instead."
            ),
            category="action",
            input_schema=AttachFileInput.model_json_schema(),
            output_schema=AttachFileOutput.model_json_schema(),
            idempotent=False,
            timeout_seconds=120,
            tags=["files", "network_dir", "attach", "materialize", "collect"],
            requires_capability="read_file",
        )

    @property
    def input_model(self) -> Type[BaseModel]:
        return AttachFileInput

    @property
    def output_model(self) -> Type[BaseModel]:
        return AttachFileOutput

    async def run_stream(
        self, tool_input: Dict[str, Any], runtime_ctx: Dict[str, Any]
    ) -> AsyncIterator[ToolEvent]:
        data = AttachFileInput(**tool_input)
        yield ToolStartEvent(type="tool.start", payload={
            "title": f"Attaching {len(data.file_ids)} file(s)",
            "connection_id": data.connection_id,
        })

        client, err = await resolve_file_client(
            runtime_ctx, data.connection_id, Capability.READ_FILE
        )
        if err:
            yield ToolEndEvent(type="tool.end", payload={
                "output": {"success": False, "connection_id": data.connection_id, "error": err},
                "observation": {"summary": err, "success": False},
            })
            return

        results: List[AttachedFile] = []
        for fid in data.file_ids:
            try:
                content, name, mime = await self._raw_bytes(client, fid)
                session_file_id = await self._persist_durable(
                    runtime_ctx, filename=name, content=content, mime=mime,
                )
                if session_file_id:
                    results.append(AttachedFile(
                        file_id=fid, name=name, session_file_id=session_file_id, size=len(content),
                    ))
                else:
                    results.append(AttachedFile(file_id=fid, name=name, error="attach failed"))
            except Exception as e:
                results.append(AttachedFile(file_id=fid, error=str(e)))

        ok = [r for r in results if r.session_file_id]
        summary = f"Attached {len(ok)}/{len(data.file_ids)} file(s) to the report"
        failed = [r for r in results if not r.session_file_id]
        if failed:
            summary += f" ({len(failed)} failed)"

        yield ToolEndEvent(type="tool.end", payload={
            "output": {
                "success": bool(ok),
                "connection_id": data.connection_id,
                "attached_count": len(ok),
                "files": [r.model_dump() for r in results],
                **({"error": "; ".join(f"{r.file_id}: {r.error}" for r in failed)} if not ok else {}),
            },
            "observation": {"summary": summary, "success": bool(ok)},
        })

    async def _raw_bytes(self, client, file_id: str) -> Tuple[bytes, str, Optional[str]]:
        """Prefer the client's raw-bytes reader (original file); otherwise
        serialize whatever read_file returns so it still attaches."""
        if hasattr(client, "read_raw_bytes"):
            import asyncio
            return await asyncio.to_thread(client.read_raw_bytes, file_id)
        # Fallback: reparse via read_file and serialize.
        import io
        import json
        import pandas as pd
        payload = await client.aread_file(file_id)
        base = str(file_id).split("/")[-1] or "file"
        if isinstance(payload, pd.DataFrame):
            buf = io.StringIO(); payload.to_csv(buf, index=False)
            return buf.getvalue().encode("utf-8"), f"{base}.csv", "text/csv"
        if isinstance(payload, (dict, list)):
            return json.dumps(payload, default=str).encode("utf-8"), f"{base}.json", "application/json"
        if isinstance(payload, (bytes, bytearray)):
            return bytes(payload), base, None
        return str(payload).encode("utf-8"), f"{base}.txt", "text/plain"

    async def _persist_durable(
        self, runtime_ctx: Dict[str, Any], *, filename: str, content: bytes, mime: Optional[str],
    ) -> Optional[str]:
        """Write bytes to the uploads store, create a durable File row, link it
        to the report, and expose it to same-turn analysis tools. Mirrors
        file_service.upload_file but for connector-sourced bytes, with no
        file-type gate (an explicit attach persists any type)."""
        db = runtime_ctx.get("db")
        report = runtime_ctx.get("report")
        user = runtime_ctx.get("user")
        organization = runtime_ctx.get("organization")
        if not (db and report and user and organization and content):
            return None

        from app.models.file import File
        from app.models.report import Report

        os.makedirs("uploads/files", exist_ok=True)
        safe = (filename or "file").replace("/", "_")
        path = f"uploads/files/{uuid.uuid4()}_{safe}"
        async with aiofiles.open(path, "wb") as fh:
            await fh.write(content)

        db_file = File(
            filename=safe,
            content_type=mime or "application/octet-stream",
            path=path,
            user_id=str(user.id),
            organization_id=str(organization.id),
            source_kind="upload",  # durable — attaches to report.files
        )
        db.add(db_file)
        await db.commit()
        await db.refresh(db_file)

        # Durable link to the report.
        report_row = (await db.execute(select(Report).where(Report.id == str(report.id)))).scalar_one_or_none()
        if report_row is not None:
            report_row.files.append(db_file)
            await db.commit()

        # Same-turn visibility for inspect_data / create_data.
        try:
            ef = runtime_ctx.get("excel_files")
            if isinstance(ef, list) and all(getattr(x, "id", None) != db_file.id for x in ef):
                ef.append(db_file)
        except Exception as e:
            logger.warning("attach_file: excel_files refresh failed: %s", e)

        try:
            from app.services.file_preview import generate_file_preview
            db_file.preview = generate_file_preview(db_file)
            db.add(db_file)
            await db.commit()
        except Exception:
            pass

        return str(db_file.id)
