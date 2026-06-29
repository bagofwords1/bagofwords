"""Resolve durable connector file references (A3) into per-user session files.

`ensure_materialized` fetches a referenced file under the *current user* via the
connection's client and lands it as an ephemeral `connector` session File (same
materialization as the agent-driven path). Bytes are never cached on the
reference — every run re-fetches, so the copy is always fresh and per-user.
"""
from __future__ import annotations

import io
import json as _json
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


def _serialize(payload: Any, name: str, ref_mime: Optional[str]):
    """Turn whatever the client returned into (filename, bytes, mime)."""
    try:
        import pandas as pd
    except Exception:
        pd = None

    if pd is not None and isinstance(payload, pd.DataFrame):
        buf = io.StringIO()
        payload.to_csv(buf, index=False)
        return (name if name.endswith(".csv") else f"{name}.csv"), buf.getvalue().encode("utf-8"), "text/csv"
    if isinstance(payload, (dict, list)):
        return (name if name.endswith(".json") else f"{name}.json"), \
            _json.dumps(payload, default=str, ensure_ascii=False).encode("utf-8"), "application/json"
    if isinstance(payload, str):
        return (name if name.endswith(".txt") else f"{name}.txt"), payload.encode("utf-8"), "text/plain"
    if isinstance(payload, (bytes, bytearray)):
        return name, bytes(payload), (ref_mime or "application/octet-stream")
    return None, None, None


async def ensure_materialized(db, ref, user, report, organization):
    """Fetch the referenced file (per-user) and materialize a session File.
    Returns the File, or None on failure (best-effort)."""
    from app.models.connection import Connection
    from app.models.file import File
    from app.services.connection_service import ConnectionService
    from app.ai.tools.implementations._file_tool_common import attach_drive_file_to_session

    conn = await db.get(Connection, ref.connection_id)
    if conn is None:
        return None
    client = await ConnectionService().construct_client(db, conn, user)
    payload = await client.aread_file(ref.external_file_id)

    name = ref.name or str(ref.external_file_id)
    filename, content, mime = _serialize(payload, name, ref.mime)
    if not content:
        return None

    rctx = {
        "db": db, "report": report, "user": user, "current_user": user,
        "organization": organization, "excel_files": None,
    }
    sid = await attach_drive_file_to_session(
        rctx, filename=filename, content_bytes=content, mime_type=mime, source_kind="connector",
    )
    return await db.get(File, sid) if sid else None
