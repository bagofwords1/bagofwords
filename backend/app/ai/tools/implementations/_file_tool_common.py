"""Shared helpers for file-source agent tools.

Resolves a data_source_id from runtime_ctx to a constructed DataSourceClient
(with per-user OAuth applied), verifies the requested capability, and renders
read_file output into the LLM-friendly shape expected by the tool's schema.
"""
from __future__ import annotations

import io
import json
from typing import Any, Dict, Optional, Tuple

import pandas as pd
from sqlalchemy import select

from app.data_sources.clients.base import Capability, DataSourceClient


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
