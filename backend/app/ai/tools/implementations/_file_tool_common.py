"""Shared helpers for file-source agent tools.

Resolves a data_source_id from runtime_ctx to a constructed DataSourceClient
(with per-user OAuth applied), verifies the requested capability, and renders
read_file output into the LLM-friendly shape expected by the tool's schema.
"""
from __future__ import annotations

import io
import json
import logging
import os
import uuid
from typing import Any, Dict, Optional, Tuple

import aiofiles
import pandas as pd
from sqlalchemy import select

from app.data_sources.clients.base import Capability, DataSourceClient

logger = logging.getLogger(__name__)


FILE_SOURCE_TYPES = {"sharepoint", "onedrive", "google_drive"}


async def resolve_file_client(
    runtime_ctx: Dict[str, Any],
    connection_id: str,
    required_capability: Capability,
) -> Tuple[Optional[DataSourceClient], Optional[str]]:
    """Resolve a connection ID to a constructed file client.

    Returns (client, error_message). On error, client is None.
    Validates: db/org context present, connection belongs to org and is attached
    to the current agent (report.data_sources[*].connections — same path
    execute_mcp uses for tool-provider connections), connection type is a file
    source, and the client declares the required capability.
    """
    db = runtime_ctx.get("db")
    organization = runtime_ctx.get("organization")
    report = runtime_ctx.get("report")
    current_user = runtime_ctx.get("user")

    if not db or not organization:
        return None, "Missing database session or organization context."

    # Build the allowed-connection set from the agent's attached data sources.
    # SharePoint reaches us via DataSource (data source connection); OneDrive
    # and Google Drive are tool-provider connections attached to the same agent
    # via the same m2m table — both routes land here.
    allowed_ids = set()
    if report:
        for ds in (report.data_sources or []):
            for conn in (ds.connections or []):
                if conn.type in FILE_SOURCE_TYPES:
                    allowed_ids.add(str(conn.id))
        if allowed_ids and str(connection_id) not in allowed_ids:
            return None, f"Connection '{connection_id}' is not attached to this agent."

    from app.models.connection import Connection

    result = await db.execute(
        select(Connection).where(
            Connection.id == str(connection_id),
            Connection.organization_id == str(organization.id),
            Connection.type.in_(list(FILE_SOURCE_TYPES)),
        )
    )
    connection = result.scalar_one_or_none()
    if not connection:
        return None, f"Connection '{connection_id}' not found or not a file source."

    from app.services.connection_service import ConnectionService

    service = ConnectionService()
    try:
        client = await service.construct_client(db, connection, current_user)
    except Exception as e:
        return None, f"Failed to construct client: {e}"

    if required_capability not in getattr(client, "capabilities", set()):
        return None, (
            f"Connection '{connection.name}' does not support {required_capability.value}."
        )

    return client, None


def render_file_payload(name: str, payload: Any, max_rows: int, max_chars: int) -> Dict[str, Any]:
    """Turn whatever read_file returned into the ReadFileOutput shape."""
    out: Dict[str, Any] = {"file_name": name}

    if isinstance(payload, pd.DataFrame):
        truncated = len(payload) > max_rows
        df = payload.head(max_rows) if truncated else payload
        buf = io.StringIO()
        df.to_csv(buf, index=False)
        out.update({
            "content_type": "tabular",
            "csv": buf.getvalue(),
            "row_count": int(len(df)),
            "col_count": int(len(df.columns)),
            "truncated": truncated,
        })
        return out

    if isinstance(payload, str):
        truncated = len(payload) > max_chars
        out.update({
            "content_type": "text",
            "text": payload[:max_chars],
            "truncated": truncated,
        })
        return out

    if isinstance(payload, (dict, list)):
        text = json.dumps(payload, default=str, ensure_ascii=False)
        truncated = len(text) > max_chars
        out.update({
            "content_type": "json",
            "text": text[:max_chars],
            "truncated": truncated,
        })
        return out

    if isinstance(payload, (bytes, bytearray)):
        out.update({
            "content_type": "binary",
            "byte_count": len(payload),
            "truncated": False,
        })
        return out

    out.update({"content_type": "unknown", "text": str(payload)[:max_chars]})
    return out


# Mime types we treat as "worth attaching as a session file" — i.e. things
# inspect_data / read_excel_as_csv / create_data already know how to analyse.
# Binaries we don't recognize aren't attached (the agent gets just the byte
# count) to avoid clutter and accidental persistence of large unknown files.
_ATTACHABLE_BY_EXT = {
    "csv": "text/csv",
    "tsv": "text/tab-separated-values",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "xls": "application/vnd.ms-excel",
    "json": "application/json",
    "txt": "text/plain",
    "md": "text/markdown",
    "pdf": "application/pdf",
}

# Hard cap on auto-attach size. Larger files still return content inline but
# don't get persisted — the agent should reach for a more specific reader.
_ATTACH_MAX_BYTES = 50 * 1024 * 1024  # 50 MB


async def attach_drive_file_to_session(
    runtime_ctx: Dict[str, Any],
    *,
    filename: str,
    content_bytes: bytes,
    mime_type: Optional[str] = None,
) -> Optional[str]:
    """Persist Drive file bytes as a session File and link to the current report.

    Mirrors what `file_service.upload_file` does for user uploads — once the
    file lands in the same File table that inspect_data / read_excel_as_csv /
    create_data already read from, the agent can analyse Drive files via the
    existing tool stack without any per-source code path.

    Returns the new File row id, or None if the file wasn't attached (no
    report context, oversize, or persistence failed — non-fatal, caller still
    returns inline content).
    """
    db = runtime_ctx.get("db")
    report = runtime_ctx.get("report")
    user = runtime_ctx.get("user")
    organization = runtime_ctx.get("organization")
    if not (db and report and user and organization):
        return None
    if not content_bytes:
        return None
    if len(content_bytes) > _ATTACH_MAX_BYTES:
        logger.info(
            "attach_drive_file_to_session: %s skipped (%.1f MB > cap)",
            filename, len(content_bytes) / (1024 * 1024),
        )
        return None

    ext = filename.rsplit(".", 1)[-1].lower() if "." in (filename or "") else ""
    if ext not in _ATTACHABLE_BY_EXT:
        # Unknown / binary — don't litter the conversation with opaque blobs.
        return None
    resolved_mime = mime_type or _ATTACHABLE_BY_EXT[ext]

    try:
        from app.models.file import File
        from app.models.report import Report

        os.makedirs("uploads/files", exist_ok=True)
        unique_filename = f"{uuid.uuid4()}_{filename}"
        path = f"uploads/files/{unique_filename}"
        async with aiofiles.open(path, "wb") as fh:
            await fh.write(content_bytes)

        db_file = File(
            filename=filename,
            content_type=resolved_mime,
            path=path,
            user_id=str(user.id),
            organization_id=str(organization.id),
        )
        db.add(db_file)
        await db.commit()
        await db.refresh(db_file)

        # Attach to the current report so it shows up in the same place
        # uploaded files do.
        report_q = await db.execute(select(Report).where(Report.id == str(report.id)))
        report_row = report_q.scalar_one_or_none()
        if report_row is not None:
            report_row.files.append(db_file)
            await db.commit()

        # Best-effort raw preview, same as upload path.
        try:
            from app.services.file_preview import generate_file_preview
            db_file.preview = generate_file_preview(db_file)
            db.add(db_file)
            await db.commit()
        except Exception as e:
            logger.warning("attach_drive_file_to_session: preview failed for %s: %s", filename, e)

        logger.info("attach_drive_file_to_session: attached %s as session file %s", filename, db_file.id)
        return str(db_file.id)
    except Exception as e:
        logger.warning("attach_drive_file_to_session: persistence failed for %s: %s", filename, e)
        return None
