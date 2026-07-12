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


FILE_SOURCE_TYPES = {"sharepoint", "onedrive", "google_drive", "outlook_mail", "network_dir", "s3"}


async def audit_file_access_denied(
    runtime_ctx: Dict[str, Any], connection_id: str, file_id: str, reason: str
) -> None:
    """Record an audit-trail entry when a file tool is denied access to a path
    outside the connection's include-globs. Best-effort — never raises, never
    blocks the tool response. Viewing is license-gated at the API; logging is
    always on so the trail is complete."""
    try:
        db = runtime_ctx.get("db")
        organization = runtime_ctx.get("organization")
        user = runtime_ctx.get("user")
        if db is None or organization is None:
            return
        from app.ee.audit.service import audit_service
        await audit_service.log(
            db=db,
            organization_id=str(organization.id),
            action="file.access_denied",
            user_id=str(user.id) if user is not None else None,
            resource_type="connection",
            resource_id=str(connection_id),
            details={"file_id": file_id, "reason": reason,
                     "report_id": str(getattr(runtime_ctx.get("report"), "id", "")) or None},
            commit=True,
        )
        logger.warning("file.access_denied conn=%s file=%s: %s", connection_id, file_id, reason)
    except Exception:
        logger.debug("audit_file_access_denied failed", exc_info=True)


async def resolve_file_data_source(
    runtime_ctx: Dict[str, Any],
    connection_id: str,
) -> Tuple[Optional[Any], Optional[str]]:
    """Same accept-either-id semantics as resolve_file_client, but returns
    the DataSource that owns the file-source connection. Used by list_files
    which reads the cached catalog rather than calling the upstream client.

    Returns (data_source, error). On error, data_source is None.
    """
    report = runtime_ctx.get("report")
    if not report:
        return None, "No report context — list_files needs an active agent."
    sid = str(connection_id)
    for ds in (report.data_sources or []):
        if str(ds.id) == sid:
            return ds, None
        for conn in (ds.connections or []):
            if str(conn.id) == sid and conn.type in FILE_SOURCE_TYPES:
                return ds, None
    return None, f"'{connection_id}' is not a file source attached to this agent."


async def resolve_file_client(
    runtime_ctx: Dict[str, Any],
    connection_id: str,
    required_capability: Capability,
) -> Tuple[Optional[DataSourceClient], Optional[str]]:
    """Resolve an ID to a constructed file client.

    The `connection_id` arg accepts either:
      - a Connection ID (preferred), OR
      - a DataSource (agent) ID — we then pick its first attached file-source
        connection. The LLM frequently confuses these because the agent
        surface refers to itself by data_source_id; resolving both makes the
        tool robust to that.

    Returns (client, error_message). On error, client is None.
    Validates: db/org context present, resolved connection belongs to the
    current agent, type is a file source, client declares the capability.
    """
    db = runtime_ctx.get("db")
    organization = runtime_ctx.get("organization")
    report = runtime_ctx.get("report")
    current_user = runtime_ctx.get("user")

    if not db or not organization:
        return None, "Missing database session or organization context."

    # Build the agent's attached file-source connections from the report's
    # data sources. Used both as an allow-list (security) and as the
    # fallback when the LLM passes a data_source_id instead of a connection_id.
    attached_conns: list = []
    if report:
        for ds in (report.data_sources or []):
            for conn in (ds.connections or []):
                if conn.type in FILE_SOURCE_TYPES:
                    attached_conns.append((ds, conn))

    attached_conn_ids = {str(conn.id) for _, conn in attached_conns}
    attached_ds_ids = {str(ds.id) for ds, _ in attached_conns}
    sid = str(connection_id)

    resolved_conn = None

    if sid in attached_conn_ids:
        # Direct Connection ID match — happy path.
        for _, conn in attached_conns:
            if str(conn.id) == sid:
                resolved_conn = conn
                break
    elif sid in attached_ds_ids:
        # LLM passed the DataSource (agent) ID. Pick the first file-source
        # connection on that data source.
        for ds, conn in attached_conns:
            if str(ds.id) == sid:
                resolved_conn = conn
                break

    if resolved_conn is None:
        if report and attached_conns:
            return None, (
                f"'{connection_id}' is not a file source attached to this agent. "
                f"Attached file connections: {sorted(attached_conn_ids)}."
            )
        # No report scope — fall through to direct DB lookup (used by
        # standalone tool calls / tests).
        from app.models.connection import Connection
        result = await db.execute(
            select(Connection).where(
                Connection.id == sid,
                Connection.organization_id == str(organization.id),
                Connection.type.in_(list(FILE_SOURCE_TYPES)),
            )
        )
        resolved_conn = result.scalar_one_or_none()
        if not resolved_conn:
            return None, f"Connection '{connection_id}' not found or not a file source."

    from app.services.connection_service import ConnectionService

    service = ConnectionService()
    try:
        client = await service.construct_client(db, resolved_conn, current_user)
    except Exception as e:
        return None, f"Failed to construct client: {e}"

    if required_capability not in getattr(client, "capabilities", set()):
        return None, (
            f"Connection '{resolved_conn.name}' does not support {required_capability.value}."
        )

    # Stash the resolved connection so tools can inspect its auth policy (e.g.
    # list_files listing live-per-user for per-user OAuth sources) without a
    # second lookup.
    try:
        client._bow_connection = resolved_conn
    except Exception:
        pass

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
    # Rendered page images / picture files — materialized so they get a file id
    # (referenceable in later turns and visible in the UI).
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
}

# Reverse map: MIME → extension. Used when materializing an MCP blob/resource
# whose filename has no usable extension (e.g. a resource URI). Lets us still
# pick an extension the analysis tools (read_excel_as_csv/inspect_data) accept.
_EXT_BY_MIME = {v: k for k, v in _ATTACHABLE_BY_EXT.items()}
_EXT_BY_MIME.update({
    "application/vnd.ms-excel.sheet.macroenabled.12": "xlsx",
    "text/plain; charset=utf-8": "txt",
    "application/csv": "csv",
})


def ext_for_mime(mime: Optional[str]) -> Optional[str]:
    """Best-effort extension for a MIME type, or None if not attachable."""
    if not mime:
        return None
    return _EXT_BY_MIME.get(mime.strip().lower())


# Picture files we can hand to a vision model as-is (normalized to PNG).
_RENDERABLE_IMAGE_EXTS = {"png", "jpg", "jpeg", "gif", "webp", "bmp", "tiff", "tif"}


def render_file_images(file_id: str, payload, *, max_pages: int = 8, dpi: int = 150):
    """Turn a *binary* file payload into page images for a vision model.

    - Picture files (png/jpg/…) pass through, normalized to PNG.
    - PDFs are rasterized page-by-page via pdf2image (needs poppler at runtime).

    Returns ``(images, total_pages)`` where ``images`` is a list of
    ``(png_bytes, "image/png")``, capped at ``max_pages``. Best-effort: returns
    ``([], 0)`` when the payload isn't a renderable binary or the renderer is
    unavailable, so the caller simply keeps the original (binary) result.
    """
    if not isinstance(payload, (bytes, bytearray)):
        return [], 0
    data = bytes(payload)
    ext = file_id.rsplit(".", 1)[-1].lower() if "." in (file_id or "") else ""
    import io
    try:
        if ext in _RENDERABLE_IMAGE_EXTS:
            from PIL import Image
            im = Image.open(io.BytesIO(data))
            im.load()
            if im.mode not in ("RGB", "RGBA", "L"):
                im = im.convert("RGB")
            buf = io.BytesIO()
            im.save(buf, format="PNG")
            return [(buf.getvalue(), "image/png")], 1
        if ext == "pdf":
            from pdf2image import convert_from_bytes
            pages = convert_from_bytes(data, dpi=dpi)
            total = len(pages)
            out = []
            for pg in pages[:max_pages]:
                buf = io.BytesIO()
                pg.save(buf, format="PNG")
                out.append((buf.getvalue(), "image/png"))
            return out, total
    except Exception as e:  # missing poppler, corrupt file, unsupported image
        logger.info("render_file_images: could not render %s: %s", file_id, e)
    return [], 0


def allow_llm_see_data(runtime_ctx: Dict[str, Any]) -> bool:
    """Whether the org lets raw data/content reach the model. Defaults to True
    when settings are unavailable, matching the other tool call sites."""
    try:
        org = runtime_ctx.get("organization")
        settings = getattr(org, "settings", None)
        if settings is None:
            return True
        try:
            return bool(settings.get_config("allow_llm_see_data").value)
        except Exception:
            pass
        cfg = getattr(settings, "config", None)
        if isinstance(cfg, dict):
            return bool(cfg.get("allow_llm_see_data", {}).get("value", True))
    except Exception:
        pass
    return True

# Hard cap on auto-attach size. Larger files still return content inline but
# don't get persisted — the agent should reach for a more specific reader.
_ATTACH_MAX_BYTES = 50 * 1024 * 1024  # 50 MB


async def attach_drive_file_to_session(
    runtime_ctx: Dict[str, Any],
    *,
    filename: str,
    content_bytes: bytes,
    mime_type: Optional[str] = None,
    source_kind: str = "connector",
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
        # Filename has no attachable extension (common for MCP resources keyed by
        # URI) — try to derive one from the MIME type before giving up.
        mime_ext = ext_for_mime(mime_type)
        if mime_ext:
            ext = mime_ext
            filename = f"{filename}.{ext}" if filename else f"resource.{ext}"
        else:
            # Unknown / binary — don't litter the conversation with opaque blobs.
            return None
    resolved_mime = mime_type or _ATTACHABLE_BY_EXT[ext]

    try:
        from app.models.file import File
        from app.models.report import Report

        os.makedirs("uploads/files", exist_ok=True)
        # File ids from nested sources carry path separators (e.g.
        # "docs/scan.png"); they must not leak into the on-disk path or the open
        # fails on a missing subdir. Flatten for storage; keep `filename` (the
        # display name) intact.
        safe_name = filename.replace("/", "_").replace("\\", "_")
        unique_filename = f"{uuid.uuid4()}_{safe_name}"
        path = f"uploads/files/{unique_filename}"
        async with aiofiles.open(path, "wb") as fh:
            await fh.write(content_bytes)

        db_file = File(
            filename=filename,
            content_type=resolved_mime,
            path=path,
            user_id=str(user.id),
            organization_id=str(organization.id),
            source_kind=source_kind,
        )
        db.add(db_file)
        await db.commit()
        await db.refresh(db_file)

        # Durable report link ONLY for uploads. Connector files are ephemeral:
        # they're materialized per turn for analysis and must NOT persist into
        # report.files (next turn would reuse a stale copy). They reach the
        # current turn's tools purely via the excel_files append below; freshness
        # comes from the agent re-downloading when it needs the data again.
        if source_kind != "connector":
            report_q = await db.execute(select(Report).where(Report.id == str(report.id)))
            report_row = report_q.scalar_one_or_none()
            if report_row is not None:
                report_row.files.append(db_file)
                await db.commit()

        # Same-turn visibility: excel_files is the init-time snapshot of
        # report.files and isn't refreshed mid-run, so a file materialized now
        # would be invisible to inspect_data / create_data called later THIS
        # turn. Append it to the live list (same object as agent.analysis_files).
        try:
            ef = runtime_ctx.get("excel_files")
            if isinstance(ef, list) and all(getattr(x, "id", None) != db_file.id for x in ef):
                ef.append(db_file)
        except Exception as e:
            logger.warning("attach_drive_file_to_session: excel_files refresh failed: %s", e)

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
