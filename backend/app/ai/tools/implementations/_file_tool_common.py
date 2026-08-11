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
from app.data_sources.clients._file_source_common import storage_safe_name
from app.data_sources.clients._office_convert import (
    CONVERTIBLE_EXTS,
    office_to_pdf_bytes,
)

logger = logging.getLogger(__name__)


# Connection types whose payloads the file-tool layer can resolve and read.
# This is the allow-list every file/mail/note tool resolves a connection_id
# against — a type missing here can still be LISTED (list_* resolves the client
# by capability) but never read or searched, which surfaces as the misleading
# "no file source is attached to this agent".
FILE_SOURCE_TYPES = {
    "sharepoint", "onedrive", "google_drive", "outlook_mail", "gmail_mail",
    "network_dir", "s3", "onenote",
}


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


_AUTH_ERROR_MARKERS = (
    "401", "unauthorized", "invalid authentication", "invalid_grant",
    "expired or revoked", "insufficient authentication", "credential",
    "access token", "oauth",
)


def friendly_tool_error(operation: str, connection_name: str, exc: Exception) -> str:
    """Convert provider exceptions into product-native observations.

    Auth failures become a call-to-action the model can relay ("user must
    Connect this source") instead of a raw OAuth boilerplate dump rendered
    red in the chat; everything else keeps the raw detail for debugging."""
    text = str(exc)
    low = text.lower()
    if any(m in low for m in _AUTH_ERROR_MARKERS):
        name = connection_name or "this source"
        return (
            f"'{name}' needs the user to sign in — its access token is missing or "
            "expired. Do NOT retry and do NOT show raw provider errors: tell the "
            f"user to Connect '{name}' from the agent selector, then continue with "
            "the remaining sources."
        )
    return f"{operation} failed: {text}"


def mark_agent_used(runtime_ctx: Dict[str, Any], ds: Any) -> None:
    """Record that a tool actually operated on this agent this run.

    Feeds focus-on-use (agent_v2._persist_focus_on_use): the run's focus
    follows the agents that were genuinely used, so the model never has to
    remember a set_report_agents step after the fact.
    """
    try:
        used = runtime_ctx.get("used_agent_ids")
        if isinstance(used, set) and ds is not None:
            used.add(str(ds.id))
    except Exception:
        pass


def note_enumeration(runtime_ctx: Dict[str, Any], connection_key: str, count: int) -> Optional[str]:
    """Per-run repeat-enumeration guard for list/search file tools.

    Returns a hint string when this connection was ALREADY enumerated this
    run (regardless of how the query was phrased — rephrasing bypasses
    exact-query dedupe), else records the enumeration and returns None.
    Result-time feedback lands exactly when the model is about to repeat
    itself, unlike description-time advice.
    """
    try:
        seen = runtime_ctx.setdefault("_file_enum_seen", {})
        key = str(connection_key or "")
        prev = seen.get(key)
        seen[key] = max(int(count or 0), int(prev or 0))
        if prev is not None:
            return (
                f"NOTE: this connection was already enumerated this run "
                f"({prev} file(s) seen earlier). Consult your note/inventory instead of "
                "re-listing — rephrasing the same discovery is still re-running it."
            )
    except Exception:
        pass
    return None


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
    sid = str(connection_id or "").strip()
    sid_l = sid.lower()
    # Accept a connection id, a data_source id, OR either's NAME — the model
    # frequently addresses a source by the label it sees (e.g. the connection
    # name "Employees SharePoint") rather than the internal UUID.
    file_choices = []  # (name, id) for the error message
    for ds in (report.data_sources or []):
        for conn in (ds.connections or []):
            if conn.type in FILE_SOURCE_TYPES:
                file_choices.append(((getattr(conn, "name", "") or "").strip(), conn.id))
        if str(ds.id) == sid:
            mark_agent_used(runtime_ctx, ds)
            return ds, None
        if sid_l and (getattr(ds, "name", "") or "").strip().lower() == sid_l:
            mark_agent_used(runtime_ctx, ds)
            return ds, None
        for conn in (ds.connections or []):
            if conn.type not in FILE_SOURCE_TYPES:
                continue
            if str(conn.id) == sid:
                mark_agent_used(runtime_ctx, ds)
                return ds, None
            if sid_l and (getattr(conn, "name", "") or "").strip().lower() == sid_l:
                mark_agent_used(runtime_ctx, ds)
                return ds, None
    choices = ", ".join(f"'{n}' (id: {i})" for n, i in file_choices) or "(none attached)"
    return None, (
        f"Invalid file-source selection: '{connection_id}' does not match any file "
        f"source attached to this agent. This is NOT a disconnection — the attached "
        f"source(s) are still connected. Retry with one of: {choices}."
    )


def attached_file_connections(runtime_ctx: Dict[str, Any]) -> list:
    """``[(data_source, connection), ...]`` for every ACTIVE file-source
    connection on the current report's ACTIVE data sources.

    This is the allow-list every file tool resolves against: a disabled agent
    or connection is never a valid target, and nothing outside the report the
    caller is running in can appear here. Empty when there's no report scope.
    """
    report = runtime_ctx.get("report")
    if not report:
        return []
    out: list = []
    for ds in (report.data_sources or []):
        if not getattr(ds, "is_active", True):
            continue
        for conn in (ds.connections or []):
            if conn.type in FILE_SOURCE_TYPES and getattr(conn, "is_active", True):
                out.append((ds, conn))
    return out


def describe_file_connections(attached: list) -> str:
    """`'Name' (id: …), 'Other' (id: …)` — the retry hint every file tool shows
    when a selection didn't resolve, so the model can name a real source
    instead of guessing (or re-listing in a loop)."""
    return ", ".join(
        f"'{(getattr(conn, 'name', '') or '').strip()}' (id: {conn.id})"
        for _, conn in attached
    ) or "(none attached)"


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

    # The agent's attached file-source connections form the allow-list AND the
    # resolution target. We only ever resolve to something on this list, so the
    # forgiving fallbacks below can never reach a connection the current report
    # isn't already built on.
    attached = attached_file_connections(runtime_ctx)  # (ds, conn)

    sid = str(connection_id or "").strip()
    sid_l = sid.lower()

    resolved_conn = None
    resolved_ds = None

    if report:
        # Resolve forgivingly — the model frequently passes the agent's name or
        # its data_source id instead of the connection id. Order: exact
        # connection id → data_source id → data_source NAME → connection NAME →
        # (if the agent has exactly one file connection) that connection
        # regardless of the guess.
        for ds, conn in attached:
            if str(conn.id) == sid:
                resolved_ds, resolved_conn = ds, conn
                break
        if resolved_conn is None:
            for ds, conn in attached:
                if str(ds.id) == sid:
                    resolved_ds, resolved_conn = ds, conn
                    break
        if resolved_conn is None and sid_l:
            for ds, conn in attached:
                if (getattr(ds, "name", "") or "").strip().lower() == sid_l:
                    resolved_ds, resolved_conn = ds, conn
                    break
        # Match the CONNECTION's own name too — the model often passes the label
        # it sees (e.g. "Employees SharePoint"), which is the connection name,
        # not the data_source name. Without this, a multi-connection agent
        # rejects a valid-but-name-addressed selection, and the model tends to
        # misreport that rejection to the user as "SharePoint is disconnected".
        if resolved_conn is None and sid_l:
            for ds, conn in attached:
                if (getattr(conn, "name", "") or "").strip().lower() == sid_l:
                    resolved_ds, resolved_conn = ds, conn
                    break
        if resolved_conn is None and len(attached) == 1:
            resolved_ds, resolved_conn = attached[0]
            logger.info(
                "resolve_file_client: '%s' matched no id/name; using the agent's "
                "only file connection %s (%s).",
                connection_id, resolved_conn.id, resolved_conn.name,
            )

        if resolved_conn is None:
            # A bad SELECTION, not a connectivity problem. Be explicit so the
            # model retries with a valid identifier instead of telling the user
            # the source is disconnected (these connections are attached and
            # remain connected — auth is checked separately, below).
            choices = describe_file_connections(attached)
            return None, (
                f"Invalid file-source selection: '{connection_id}' does not match any "
                f"file source attached to this agent. This is NOT a disconnection — "
                f"the attached source(s) are still connected. Retry with one of: {choices}."
            )

        # Access guard: never operate on a data source the current user can't
        # access — not even via the forgiving fallbacks above. Skipped only when
        # there's no user in context (standalone/system callers). Fail-open on a
        # resolver error (matches the existing trust in report.data_sources); a
        # definitive "no" blocks.
        if current_user is not None and resolved_ds is not None:
            try:
                from app.core.permission_resolver import user_can_access_data_source
                allowed = await user_can_access_data_source(
                    db, str(current_user.id), str(organization.id), resolved_ds
                )
            except Exception:
                allowed = True
            if not allowed:
                return None, (
                    f"You do not have access to the data source "
                    f"'{getattr(resolved_ds, 'name', '?')}'."
                )
    else:
        # No report scope — direct DB lookup (standalone tool calls / tests).
        from app.models.connection import Connection
        result = await db.execute(
            select(Connection).where(
                Connection.id == sid,
                Connection.organization_id == str(organization.id),
                Connection.type.in_(list(FILE_SOURCE_TYPES)),
                Connection.is_active == True,  # noqa: E712
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

    if resolved_ds is not None:
        mark_agent_used(runtime_ctx, resolved_ds)
    return client, None


def resolve_session_file(runtime_ctx: Dict[str, Any], file_id: str):
    """Resolve a file id against the report's OWN file space (uploads,
    attach_file results). The report.files list is the allow-list — a session
    id from another report resolves to None here, exactly like an unattached
    connection in resolve_file_client. Org membership is double-checked so a
    stale association can't cross tenants."""
    report = runtime_ctx.get("report")
    organization = runtime_ctx.get("organization")
    sid = str(file_id or "").strip()
    if not (report and sid):
        return None
    # Report uploads + files inherited live from the report's project (the
    # agent loop stages the latter in runtime_ctx as "project_files").
    candidates = list(getattr(report, "files", None) or [])
    candidates += list(runtime_ctx.get("project_files") or [])
    for f in candidates:
        if str(getattr(f, "id", "")) != sid:
            continue
        f_org = getattr(f, "organization_id", None)
        if organization is not None and f_org is not None and str(f_org) != str(organization.id):
            logger.warning("resolve_session_file: org mismatch for file %s", sid)
            return None
        return f
    return None


class SessionFileClient:
    """File-client shim over ONE session file on local disk.

    Delegates parsing/windowed/page_range reads to NetworkDirClient rooted at
    the file's directory, so session files get the exact same read semantics
    (newline-snapped windows, PDF page extraction, tabular parsing) as
    connector files — one pipeline, two resolution sources. The stored disk
    name keeps the original extension (uuid_name.ext), so type dispatch works.
    """

    def __init__(self, session_file):
        import os as _os
        self._file = session_file
        self._dir = _os.path.dirname(session_file.path) or "."
        self._name = _os.path.basename(session_file.path)

    def _inner(self):
        from app.data_sources.clients.network_dir_client import NetworkDirClient
        return NetworkDirClient(root_path=self._dir)

    async def aread_file(self, _file_id: str, **kwargs) -> Any:
        import asyncio as _asyncio
        return await _asyncio.to_thread(self._inner().read_file, self._name, **kwargs)

    def read_raw_bytes(self, _file_id: str):
        return self._inner().read_raw_bytes(self._name)

    @property
    def display_name(self) -> str:
        return getattr(self._file, "filename", None) or self._name


def render_pdf_pages_images(pdf_bytes: bytes, first: int, last: int, *, max_pages: int = 8, dpi: int = 150):
    """Rasterize an inclusive 1-based page range of a PDF to vision-sized
    images — the companion to extract_pdf_pages_text for scanned/image-only
    documents. Returns (images, pages_total) where images is
    [(bytes, mime)] — PNG for pages that compress well, JPEG for scans (see
    image_utils: a lossless render of a scanned page can exceed provider
    per-image byte caps and 400 the whole request). Capped at max_pages.
    Raises on an unreadable PDF."""
    import pypdfium2 as pdfium
    from app.ai.llm.image_utils import encode_pil_for_vision

    pdf = pdfium.PdfDocument(bytes(pdf_bytes))
    try:
        total = len(pdf)
        first = max(1, int(first))
        last = min(total, int(last))
        out = []
        for i in range(first - 1, last):
            if len(out) >= max_pages:
                break
            page = pdf[i]
            try:
                bitmap = page.render(scale=dpi / 72.0)
                pil = bitmap.to_pil()
                out.append(encode_pil_for_vision(pil))
            finally:
                page.close()
        return out, total
    finally:
        pdf.close()


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
            # The file's REAL row count, not the size of the truncated preview.
            # These were the same value before, so a 1,500-row file read with
            # max_rows=1000 reported "row_count: 1000" and models answered
            # "the file has 1,000 rows" — `truncated: true` sat right beside it
            # and was not enough to prevent that. `rows_shown` now carries the
            # preview size, and the two only differ when truncation happened.
            "row_count": int(len(payload)),
            "rows_shown": int(len(df)),
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
    # Office documents. Attachable so a read that could NOT be turned into text
    # still lands in the conversation as the original file — before this, such
    # a read produced neither text, nor images, nor a session file, leaving the
    # model with an opaque byte count and no next move.
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "doc": "application/msword",
    "ppt": "application/vnd.ms-powerpoint",
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

    - Picture files (png/jpg/…) pass through, normalized to a vision-sized
      PNG/JPEG (see image_utils — provider per-image byte caps).
    - PDFs are rasterized page-by-page with pypdfium2 (PDFium — the same engine
      Chromium uses for PDFs — as a self-contained wheel, so no system poppler
      and no headless-browser launch).
    - Office documents (docx/pptx/…) are converted to PDF with LibreOffice
      first, then rasterized by the same path. Their layout doesn't exist until
      a layout engine has run, so without this a text-free Word file — one
      wrapping scanned images, say — had no vision fallback at all.

    ``file_id`` is used only to pick the format, so pass the file's real NAME
    when the id is opaque (Graph item ids carry no extension).

    Returns ``(images, total_pages)`` where ``images`` is a list of
    ``(bytes, mime)`` (PNG or JPEG), capped at ``max_pages``. Best-effort: returns
    ``([], 0)`` when the payload isn't a renderable binary or the renderer is
    unavailable, so the caller simply keeps the original (binary) result.

    Blocking — the PDF rasterizer is CPU-bound and the Office path spawns a
    subprocess. Call it from a worker thread.
    """
    if not isinstance(payload, (bytes, bytearray)):
        return [], 0
    data = bytes(payload)
    ext = file_id.rsplit(".", 1)[-1].lower() if "." in (file_id or "") else ""
    import io
    try:
        if ext in CONVERTIBLE_EXTS:
            converted = office_to_pdf_bytes(data, file_id)
            if not converted:
                return [], 0
            data, ext = converted, "pdf"
        if ext in _RENDERABLE_IMAGE_EXTS:
            from PIL import Image
            from app.ai.llm.image_utils import normalize_image_bytes
            # Prove the bytes decode before normalizing — a corrupt picture
            # must fall through to the ([], 0) no-render path (normalize is
            # fail-open and would otherwise pass the bytes along untouched).
            Image.open(io.BytesIO(data)).load()
            return [normalize_image_bytes(data)], 1
        if ext == "pdf":
            import pypdfium2 as pdfium
            from app.ai.llm.image_utils import encode_pil_for_vision
            pdf = pdfium.PdfDocument(data)
            try:
                total = len(pdf)
                out = []
                for i in range(min(total, max_pages)):
                    page = pdf[i]
                    pil = page.render(scale=dpi / 72.0).to_pil()
                    out.append(encode_pil_for_vision(pil))
                return out, total
            finally:
                pdf.close()
    except Exception as e:  # corrupt/locked file, unsupported image
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


async def _find_cached_connector_file(
    db, *, report, connection_id: Optional[str], source_ref: Optional[str],
    source_kind: str,
):
    """The connector file already materialized for this (report, connection, ref).

    Returns None for uploads, for calls that don't identify the remote file, and
    on any lookup failure — every one of those falls back to minting a fresh
    row, i.e. the old behaviour. Scoped through report_file_association so one
    report never serves another report's cached copy.
    """
    if source_kind != "connector" or not connection_id or not source_ref:
        return None
    try:
        from app.models.file import File
        from app.models.report_file_association import report_file_association

        result = await db.execute(
            select(File)
            .join(report_file_association, File.id == report_file_association.c.file_id)
            .where(
                report_file_association.c.report_id == str(report.id),
                File.source_kind == "connector",
                File.source_connection_id == str(connection_id),
                File.source_ref == str(source_ref),
                File.deleted_at.is_(None),
            )
            .order_by(File.created_at.desc())
        )
        return result.scalars().first()
    except Exception as e:
        logger.warning("attach_drive_file_to_session: cache lookup failed: %s", e)
        return None


async def attach_drive_file_to_session(
    runtime_ctx: Dict[str, Any],
    *,
    filename: str,
    content_bytes: bytes,
    mime_type: Optional[str] = None,
    source_kind: str = "connector",
    connection_id: Optional[str] = None,
    source_ref: Optional[str] = None,
) -> Optional[str]:
    """Materialize connector bytes as a File the code sandbox can read.

    Mirrors what `file_service.upload_file` does for user uploads — once the
    file lands in the same File table that inspect_data / read_excel_as_csv /
    create_data already read from, the agent can analyse connector files via
    the existing tool stack without any per-source code path. It has to be a
    local file: generated code runs sandboxed with no network, no credentials
    and no `open()`, so `excel_files[N].path` is its only way in.

    A connector file is therefore a CACHE of the remote file. Given
    ``connection_id`` + ``source_ref`` it refreshes the existing row in place;
    the previous behaviour — mint a new File row and a new copy on disk on
    every single read, never linked to the report — left the handle dead the
    moment the turn ended and leaked a file per read. Uploads are unaffected.

    Returns the File row id, or None if the file wasn't attached (no report
    context, oversize, or persistence failed — non-fatal, caller still returns
    inline content).
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

    # Every string below lands in a UTF-8 Postgres column. A name that still
    # carries surrogateescape'd bytes (legacy-encoded share, connector we
    # haven't taught to recover) makes asyncpg reject the INSERT mid-flush and
    # poisons the session for the rest of the turn — so scrub here, before the
    # name is used to build the on-disk path, and disk and DB agree.
    filename = storage_safe_name(filename or "")
    if source_ref:
        source_ref = storage_safe_name(source_ref)

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

        # Cache hit? Same report, same connection, same remote ref → refresh the
        # bytes on the existing row rather than minting another one. Keeping the
        # id stable is the point: it is what the model was handed in an earlier
        # turn's tool result and what it will pass back to create_data.
        cached = await _find_cached_connector_file(
            db, report=report, connection_id=connection_id, source_ref=source_ref,
            source_kind=source_kind,
        )

        if cached is not None:
            path = cached.path
            async with aiofiles.open(path, "wb") as fh:
                await fh.write(content_bytes)
            cached.content_type = resolved_mime
            cached.filename = filename
            db_file = cached
            db.add(db_file)
            await db.commit()
            await db.refresh(db_file)
            logger.info(
                "attach_drive_file_to_session: refreshed %s in place (session file %s)",
                filename, db_file.id,
            )
        else:
            # File ids from nested sources carry path separators (e.g.
            # "docs/scan.png"); they must not leak into the on-disk path or the
            # open fails on a missing subdir. Flatten for storage; keep
            # `filename` (the display name) intact.
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
                source_connection_id=connection_id,
                source_ref=source_ref,
            )
            db.add(db_file)
            await db.commit()
            await db.refresh(db_file)

            # Link to the report — for connector files too, now that they are a
            # refreshed cache entry rather than a new copy per read. Without the
            # link, `excel_files` (rebuilt from report.files at agent init) loses
            # the row at the turn boundary and the session_file_id the model is
            # still carrying resolves to nothing. Staleness is handled by
            # refreshing above, not by orphaning the row.
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
        # A failure during flush (bad encoding, constraint violation) leaves the
        # session in a state where EVERY later statement raises "This Session's
        # transaction has been rolled back" — the attach is best-effort, but
        # without this the whole agent turn goes down with it.
        try:
            await db.rollback()
        except Exception:
            pass
        return None
