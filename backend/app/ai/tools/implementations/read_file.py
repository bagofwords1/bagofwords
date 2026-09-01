"""read_file agent tool — read a file from a file-based data source."""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, AsyncIterator, Dict, Optional, Type

from pydantic import BaseModel

from app.ai.tools.base import Tool
from app.ai.tools.metadata import ToolMetadata
from app.ai.tools.schemas import ToolEndEvent, ToolEvent, ToolStartEvent
from app.ai.tools.schemas.file_tools import ReadFileInput, ReadFileOutput
from app.data_sources.clients.base import Capability

from app.data_sources.clients._document_text import (
    DOC_EXTS,
    doc_text_is_usable,
    doc_text_looks_garbled,
)
from app.data_sources.clients._file_source_common import GlobScopeError, payload_name

from . import _file_cache
from ._file_tool_common import (
    SessionFileClient,
    allow_llm_see_data,
    attach_drive_file_to_session,
    attached_file_connections,
    audit_file_access_denied,
    describe_file_connections,
    is_picture_name,
    persist_source_document,
    render_file_images,
    render_file_payload,
    render_pdf_pages_images,
    resolve_file_client,
    resolve_session_file,
)

logger = logging.getLogger(__name__)

# Session file ids are uuid4 (File.id). Connector ids never are: Graph items are
# opaque base32 tokens ('01LZCX…'), network_dir/S3 ids are paths. The shape is
# what lets a failed session lookup tell "stale/foreign attachment" (report it)
# apart from "the model pasted a list_files id" (route it to the connection).
_SESSION_FILE_ID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE
)


def _looks_like_session_file_id(file_id: str) -> bool:
    return bool(_SESSION_FILE_ID_RE.match(str(file_id or "").strip()))


# The planner consumes the OBSERVATION, not the tool output — so the content a
# read is supposed to deliver to the model must be rendered into
# observation.details (bounded), or the model sees only a summary line and
# rationally concludes the read returned nothing (then re-reads in a loop).
# Past observations are compacted to "N chars" by ObservationContextBuilder,
# so only the latest read pays this context cost in full.
_OBS_DETAILS_MAX_CHARS = 4000
# Windowed reads exist for sequential consumption — give them a bigger budget
# and tell the model how to page losslessly (pass length <= this budget).
_OBS_WINDOW_DETAILS_MAX_CHARS = 8000


def _display_path(file_id: str, session_client=None) -> str:
    """Human-readable location for the expanded UI: the upload filename for
    session files, the source-relative path for path-addressed connectors
    (network_dir/S3 ids ARE paths). '' for opaque provider ids (Graph)."""
    if session_client is not None:
        return getattr(session_client, "display_name", "") or ""
    fid = str(file_id or "")
    leaf = fid.rsplit("/", 1)[-1]
    if "/" in fid or "." in leaf:
        return fid
    return ""


def _doc_ext(name: str) -> str:
    """Lowercase extension of a path-or-name string ('' when none)."""
    leaf = str(name or "").rsplit("/", 1)[-1]
    return leaf.rsplit(".", 1)[-1].lower() if "." in leaf else ""


def _name_from_path_id(file_id: str) -> str:
    """Basename of a path-shaped file id ('logs/app/web.log' → 'web.log');
    '' for opaque provider ids (no slash and no dot in the leaf)."""
    fid = str(file_id or "")
    leaf = fid.rsplit("/", 1)[-1]
    if ("/" in fid or "." in leaf) and leaf:
        return leaf
    return ""


def _materialized_name(file_id: str, ext: str) -> str:
    """Display name for a connector file materialized to local disk.

    This used to be f"{file_id}.{ext}", which for a path-addressed connector
    (network_dir, S3 — their ids ARE paths) produced "sales.csv.csv". That was
    invisible while connector files were orphaned; now that they are
    report-linked the name reaches the model's file context, and the model
    reads it back as a file id: "read_file failed: File not found:
    sales.csv.csv". Use the source's own leaf name, adding the extension only
    when it is genuinely missing (opaque provider ids have none).
    """
    leaf = str(file_id or "").rsplit("/", 1)[-1] or f"file.{ext}"
    return leaf if leaf.lower().endswith(f".{ext}") else f"{leaf}.{ext}"


def _parse_page_range(value: str) -> "Optional[tuple]":
    """'3' → (3, 3); '10-15' → (10, 15). None for anything malformed."""
    try:
        raw = str(value).strip()
        if "-" in raw:
            a, b = raw.split("-", 1)
            first, last = int(a.strip()), int(b.strip())
        else:
            first = last = int(raw)
        if first < 1 or last < first:
            return None
        return (first, last)
    except (ValueError, TypeError):
        return None


def _content_details(output: Dict[str, Any], *, max_chars: int) -> str:
    """Bounded, model-facing excerpt of a read's content with an honest
    trailer: what's shown, where the full content lives, how to get the rest."""
    body = output.get("text") if output.get("text") is not None else output.get("csv")
    if not isinstance(body, str) or not body:
        return ""
    shown = body[:max_chars]
    if len(body) <= max_chars and not output.get("truncated"):
        return shown
    bits = [f"showing first {len(shown):,} of {len(body):,} chars retrieved"]
    if output.get("truncated"):
        bits.append("the retrieved content itself was truncated at max_chars/max_rows")
    sfid = output.get("session_file_id")
    if sfid:
        bits.append(f"full file is attached as session_file_id={sfid} (use inspect_data to analyze it)")
    bits.append("page the rest with windowed reads (offset/length) — do NOT re-run the same read")
    return shown + "\n[" + "; ".join(bits) + "]"


def _build_preview(output: Dict[str, Any], *, preview_file_id: Optional[str],
                   first_image_mime: Optional[str] = None,
                   source_is_image: bool = False) -> Dict[str, Any]:
    """The UI contract for this read (see ReadFilePreview).

    A DOCUMENT always beats its page renders: when a scanned PDF is rasterized
    so a vision model can read it, the user is still shown the real PDF opened
    at the page in question, not the PNG the model saw. `preview_file_id` is
    only ever passed when it holds the ORIGINAL bytes.

    Every branch returns the SAME keys — the frontend reads this as a fixed
    shape, so an absent id must be an explicit null, not a missing field.
    """
    preview: Dict[str, Any] = {
        "kind": "none",
        "file_id": None,
        "mime": None,
        "target_page": None,
        "pages_total": output.get("pages_total"),
        "image_file_ids": None,
        "truncated": False,
        # True when the images are page RENDERS of a document, not the source
        # picture itself. The card auto-shows real pictures (they ARE the
        # content) but keeps derived galleries — a lossy rasterized stand-in —
        # collapsed behind a click.
        "derived": False,
    }

    # page_range reports "10-15"; the viewer opens at the first of them.
    target_page = None
    shown = output.get("pages_shown")
    if shown:
        try:
            target_page = int(str(shown).split("-", 1)[0])
        except (TypeError, ValueError):
            target_page = None

    if preview_file_id and _doc_ext(output.get("file_name") or "") == "pdf":
        preview.update({
            "kind": "pdf",
            "file_id": preview_file_id,
            "mime": "application/pdf",
            "target_page": target_page or 1,
        })
        return preview

    image_ids = output.get("image_file_ids") or []
    if image_ids:
        pages_total = preview["pages_total"]
        preview.update({
            "kind": "image",
            "file_id": image_ids[0],
            "mime": first_image_mime or "image/png",
            "image_file_ids": image_ids,
            # Renders are capped (render_file_images max_pages), so a gallery
            # showing fewer pages than the document has must say so.
            "truncated": bool(pages_total and len(image_ids) < pages_total),
            # A picture source stays "the picture" even when the id points at
            # a normalized re-encode; only document rasters count as derived.
            "derived": not source_is_image,
        })
        return preview

    ct = output.get("content_type")
    if ct == "tabular":
        preview.update({"kind": "table", "truncated": bool(output.get("truncated"))})
    elif ct in ("text", "json"):
        preview.update({"kind": "text", "truncated": bool(output.get("truncated"))})
    return preview


class ReadFileTool(Tool):
    # Capability the resolved connection must expose. Overridden by ReadEmailTool
    # so the same read/materialize path backs a mailbox (READ_EMAIL) as well.
    _required_capability = Capability.READ_FILE
    _start_noun = "file"
    _operation_name = "read_file"

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="read_file",
            description=(
                "Read a file from a file-based connection (Files & Directories, S3, "
                "SharePoint, OneDrive, Google Drive) AND attach it to the current "
                "conversation as a session file. USE THIS — not inspect_data — "
                "whenever you need to analyze a file that came from list_files or "
                "search_files. Tabular files (CSV, Excel, Google Sheets) are "
                "returned as CSV plus a `session_file_id` you can pass to "
                "inspect_data / create_data / read_excel_as_csv exactly like an "
                "uploaded file. Text/JSON content (and a CSV head) is shown to you "
                "directly in the result, up to a bounded excerpt — trust it; the "
                "same read will return the same content, so never re-issue an "
                "identical read. The inline rows are for INSPECTION — checking "
                "shape, column names, and example values. Do NOT compute over "
                "them by hand: any count, sum, average, group-by or 'how many X' "
                "goes through create_data with this result's `session_file_id`, "
                "even when the file looks small enough to tally by eye. Counting "
                "rows in your head is where wrong answers come from. "
                "For big files, page with offset/length (text) or "
                "page_range (PDFs/documents). Binary files return their size only. "
                "EXCEPTION — garbled text: if a document's extracted text comes "
                "back as unreadable symbol soup / mojibake (broken font encoding), "
                "do NOT try to interpret it; re-issue the same read with "
                "as_images=true to see the actual pages as images."
            ),
            category="research",
            input_schema=ReadFileInput.model_json_schema(),
            output_schema=ReadFileOutput.model_json_schema(),
            idempotent=True,
            timeout_seconds=60,
            tags=["files", "sharepoint", "onedrive", "drive", "read"],
            requires_capability="read_file",
        )

    @property
    def input_model(self) -> Type[BaseModel]:
        return ReadFileInput

    @property
    def output_model(self) -> Type[BaseModel]:
        return ReadFileOutput

    async def run_stream(
        self, tool_input: Dict[str, Any], runtime_ctx: Dict[str, Any]
    ) -> AsyncIterator[ToolEvent]:
        data = ReadFileInput(**tool_input)
        yield ToolStartEvent(type="tool.start", payload={
            "title": f"Reading {self._start_noun} {data.file_id}",
            "connection_id": data.connection_id,
        })

        # Resolution ladder: the report's OWN file space first (uploads /
        # attach_file results — only when no connection was named; explicit
        # beats implicit), then the attached file connections. One verb, two
        # sources, identical read semantics.
        session_file = None
        if not (data.connection_id or "").strip():
            session_file = resolve_session_file(runtime_ctx, data.file_id)
            if session_file is None:
                # Not in the report's own space. The overwhelmingly common cause
                # is a file-source id carried straight from list_files/search_files
                # with connection_id left empty — so when the agent has exactly one
                # file connection, read from it instead of failing. Access is NOT
                # assumed: resolve_file_client re-checks that this user can reach
                # the data source (and that it declares the capability) exactly as
                # it does for an explicitly named connection.
                fallback = self._implicit_connection(runtime_ctx, data.file_id)
                if fallback is None:
                    yield self._fail_read(data, self._unresolved_error(runtime_ctx, data.file_id))
                    return
                client, err = await resolve_file_client(
                    runtime_ctx, str(fallback.id), self._required_capability
                )
                if err:
                    yield self._fail_read(data, err)
                    return
                # Own the choice: the cache key, the output and the UI all read
                # connection_id, and a blank one would misreport a connector read
                # as a conversation attachment.
                data.connection_id = str(fallback.id)
                logger.info(
                    "%s: '%s' matched no session file; reading it from the agent's "
                    "only file connection %s (%s).",
                    self._operation_name, data.file_id, fallback.id, fallback.name,
                )
            else:
                client = SessionFileClient(session_file)
        else:
            client, err = await resolve_file_client(
                runtime_ctx, data.connection_id, self._required_capability
            )
            if err:
                yield ToolEndEvent(type="tool.end", payload={
                    "output": {
                        "success": False,
                        "connection_id": data.connection_id,
                        "file_id": data.file_id,
                        "error": err,
                    },
                    "observation": {"summary": err, "success": False},
                })
                return

        # Windowed (ranged) read: pass offset/length through, return the raw
        # byte window + cursor WITHOUT parsing or attaching. For streaming
        # through large objects (logs, ndjson, big CSVs) on object-store sources.
        if data.offset is not None:
            try:
                window = await client.aread_file(
                    data.file_id, offset=data.offset, length=data.length
                )
            except Exception as e:
                if isinstance(e, GlobScopeError):
                    await audit_file_access_denied(runtime_ctx, data.connection_id, data.file_id, str(e))
                    err = str(e)
                else:
                    from app.ai.tools.implementations._file_tool_common import friendly_tool_error as _fte
                    _cn = getattr(getattr(client, "_bow_connection", None), "name", "") or ""
                    err = _fte(f"{self._operation_name} (windowed)", _cn, e)
                yield ToolEndEvent(type="tool.end", payload={
                    "output": {
                        "success": False,
                        "connection_id": data.connection_id,
                        "file_id": data.file_id,
                        "error": err,
                    },
                    "observation": {"summary": err, "success": False},
                })
                return
            if not isinstance(window, dict) or "content" not in window:
                err = "This connection does not support windowed (offset/length) reads."
                yield ToolEndEvent(type="tool.end", payload={
                    "output": {
                        "success": False,
                        "connection_id": data.connection_id,
                        "file_id": data.file_id,
                        "error": err,
                    },
                    "observation": {"summary": err, "success": False},
                })
                return
            enc = window.get("encoding", "text")
            output = {
                "success": True,
                "connection_id": data.connection_id,
                "file_id": data.file_id,
                "windowed": True,
                "path": _display_path(data.file_id, client if session_file is not None else None) or None,
                "content_type": "text" if enc == "text" else "binary",
                "text": window.get("content"),
                "encoding": enc,
                "next_cursor": None if window.get("eof") else window.get("next_cursor"),
                "total_size": window.get("total_size"),
                "eof": window.get("eof"),
                "byte_count": window.get("length"),
            }
            pos = f"{window.get('offset')}–{window.get('next_cursor')}"
            total = window.get("total_size")
            summary = (
                f"Read window {pos} of {total} bytes from {data.file_id}"
                + (" (eof)" if window.get("eof") else "")
            )
            observation: Dict[str, Any] = {"summary": summary, "success": True}
            # Ship the window text itself — paging through a file is useless
            # to the model if only the cursor arithmetic arrives.
            if enc == "text" and allow_llm_see_data(runtime_ctx):
                body = window.get("content") or ""
                shown = body[:_OBS_WINDOW_DETAILS_MAX_CHARS]
                details = shown
                if len(body) > len(shown):
                    details += (
                        f"\n[window content clipped to {len(shown):,} of {len(body):,} chars — "
                        f"request length<={_OBS_WINDOW_DETAILS_MAX_CHARS} to page without loss]"
                    )
                elif not window.get("eof"):
                    details += f"\n[continue from offset={window.get('next_cursor')}]"
                observation["details"] = details
            yield ToolEndEvent(type="tool.end", payload={
                "output": output,
                "observation": observation,
            })
            return

        # Page-range (document) read: extract ONLY the requested PDF pages.
        # Like the windowed path: no parsing beyond the range, no attach, no
        # cache — this is the lazy-read mode for large documents.
        if data.page_range is not None:
            rng = _parse_page_range(data.page_range)
            if rng is None:
                yield self._fail_read(
                    data, f"Invalid page_range {data.page_range!r} — use '3' or '10-15' (1-based)."
                )
                return
            try:
                paged = await client.aread_file(data.file_id, page_range=rng)
            except Exception as e:
                if isinstance(e, GlobScopeError):
                    await audit_file_access_denied(runtime_ctx, data.connection_id, data.file_id, str(e))
                    yield self._fail_read(data, str(e))
                else:
                    yield self._fail_read(
                        data, f"{self._operation_name} (page_range) failed: {e}"
                    )
                return
            if not isinstance(paged, dict) or not paged.get("__doc_pages__"):
                yield self._fail_read(
                    data, "This connection does not support page_range reads."
                )
                return
            shown = f"{paged.get('first')}-{paged.get('last')}"
            # Bytes and name the client carried over from the fetch it already
            # made (see the __doc_pages__ contract). Absent for network_dir,
            # which extracts straight from disk — there the fallback re-read is
            # local and free.
            paged_raw = paged.get("raw")
            paged_name = paged.get("name") or None

            # Scanned/image-only pages (no usable text), glyph-soup extractions
            # (text came back but it's garbled — subset font with a broken
            # ToUnicode map), or an explicit as_images request — rasterize the
            # REQUESTED pages for a vision model instead of returning an
            # empty/garbage read (the page-level analogue of the whole-file
            # vision fallback below).
            model = runtime_ctx.get("model")
            if (
                (
                    data.as_images
                    or not doc_text_is_usable(paged.get("text"))
                    or doc_text_looks_garbled(paged.get("text"))
                )
                and model and getattr(model, "supports_vision", False)
                and allow_llm_see_data(runtime_ctx)
            ):
                import asyncio as _asyncio

                raw_bytes = paged_raw
                try:
                    if raw_bytes is None:
                        raw = await _asyncio.to_thread(client.read_raw_bytes, data.file_id)
                        raw_bytes = raw[0] if isinstance(raw, tuple) else raw
                    imgs, total = render_pdf_pages_images(raw_bytes, rng[0], rng[1])
                except Exception as e:
                    imgs, total = [], paged.get("pages_total")
                if imgs:
                    reason = (
                        "as_images requested" if data.as_images
                        else "garbled text layer" if doc_text_is_usable(paged.get("text"))
                        else "no extractable text"
                    )
                    # Keep the ORIGINAL document so the UI can open it at the
                    # page that was read. The page PNGs stay vision-only — they
                    # ride the observation as base64 and are NOT materialized
                    # (attach_images=False), so paging through a long PDF can't
                    # mint a File row per page.
                    source_file_id = await persist_source_document(
                        runtime_ctx, client, data.file_id,
                        connection_id=data.connection_id,
                        session_file=session_file,
                        raw_bytes=raw_bytes,
                        raw_name=paged_name,
                    )
                    # source_file_id reaches the UI via preview_file_id only.
                    # As session_file_id it would be a raw PDF advertised to
                    # the model as inspect_data/read_excel_as_csv input — a
                    # guaranteed parse failure.
                    output, observation = await self._finalize(
                        data, runtime_ctx,
                        rendered={"content_type": "binary", "pages_shown": shown,
                                  "file_name": paged_name},
                        session_file_id=None,
                        image_pngs=[png for png, _mtype in imgs],
                        pages_total=total,
                        cached=False,
                        source_name=(
                            getattr(client, "display_name", None)
                            if session_file is not None else None
                        ),
                        attach_images=False,
                        summary_note=reason,
                        preview_file_id=source_file_id,
                    )
                    yield ToolEndEvent(type="tool.end", payload={
                        "output": output, "observation": observation,
                    })
                    return

            # Same deal for a text page read: the slice goes to the model, the
            # whole document is kept so the card can open it at that page.
            # Deliberately no cache write — the cache key carries no page
            # dimension, so storing a slice under the whole-file key would
            # later be served back as the entire file.
            source_file_id = await persist_source_document(
                runtime_ctx, client, data.file_id,
                connection_id=data.connection_id,
                session_file=session_file,
                raw_bytes=paged_raw,
                raw_name=paged_name,
            )
            output, observation = await self._finalize(
                data, runtime_ctx,
                rendered={
                    "content_type": "text",
                    "text": paged.get("text") or "",
                    "pages_total": paged.get("pages_total"),
                    "pages_shown": shown,
                    "file_name": paged_name,
                },
                session_file_id=None,
                image_pngs=[],
                pages_total=paged.get("pages_total"),
                cached=False,
                source_name=(
                    getattr(client, "display_name", None)
                    if session_file is not None else None
                ),
                preview_file_id=source_file_id,
            )
            yield ToolEndEvent(type="tool.end", payload={
                "output": output, "observation": observation,
            })
            return

        # ------------------------------- content cache -------------------------
        # Skip re-download / re-extract / re-render for an UNCHANGED file. Only
        # for system-identity, glob-enforced connections (never per-user OAuth: a
        # shared cache could serve content past an ACL). file_version is a cheap,
        # scope-enforced probe (raises for an off-scope id), so a cache hit stays
        # access-checked. No cheap version → skip caching, read live.
        conn_obj = getattr(client, "_bow_connection", None)
        per_user = bool(
            conn_obj is not None
            and getattr(conn_obj, "auth_policy", None) == "user_required"
            and "oauth" in (getattr(conn_obj, "allowed_user_auth_modes", None) or [])
        )
        version = None
        # Session files are already local + immutable — the connector cache
        # buys nothing and its keying assumes a connection id.
        if not per_user and session_file is None:
            try:
                version = await client.afile_version(data.file_id)
            except GlobScopeError as e:
                await audit_file_access_denied(runtime_ctx, data.connection_id, data.file_id, str(e))
                yield self._fail_read(data, str(e))
                return
            except Exception:
                version = None

        # An explicit as_images read must bypass the cache read: the cached
        # entry may be a pre-escalation text render (possibly garbled) and
        # serving it back would defeat the model's explicit request. The fresh
        # image render then overwrites the entry below.
        if version and not data.as_images:
            cached = _file_cache.read(data.connection_id, data.file_id, version)
            # Never serve a TRUNCATED render from cache: the cached text is
            # frozen at the caps of whichever call populated it, so (a) a
            # retry asking for more (bigger max_chars/max_rows) would get the
            # same clipped content back, and (b) _persist_rendered_session
            # would re-materialize the session file from the clipped text,
            # poisoning downstream analysis (inspect_data / write_csv) with a
            # fraction of the file. Truncated renders read live instead.
            if cached and (cached.get("rendered") or {}).get("truncated"):
                cached = None
            if cached:
                rendered = cached.get("rendered") or {}
                session_file_id = await self._persist_rendered_session(
                    runtime_ctx, data.file_id, rendered,
                    connection_id=getattr(data, "connection_id", None))
                # The cached entry carries the source's real name (opaque Graph
                # ids have none of their own), which is what says whether there
                # is a document worth showing.
                cached_name = rendered.get("file_name") or _name_from_path_id(data.file_id)
                if _doc_ext(cached_name or "") == "pdf":
                    # A cache hit means file_version matched, so a copy already
                    # kept for THIS report is still faithful — reuse it with no
                    # fetch. The content cache is shared across reports, so a
                    # report seeing this file for the first time still fetches.
                    preview_file_id = await persist_source_document(
                        runtime_ctx, client, data.file_id,
                        connection_id=data.connection_id,
                        session_file=session_file,
                        reuse_existing=True,
                    )
                else:
                    preview_file_id = None
                output, observation = await self._finalize(
                    data, runtime_ctx, rendered=rendered, session_file_id=session_file_id,
                    image_pngs=cached.get("image_bytes") or [], pages_total=cached.get("pages_total"),
                    cached=True, preview_file_id=preview_file_id,
                )
                yield ToolEndEvent(type="tool.end", payload={"output": output, "observation": observation})
                return

        try:
            payload = await client.aread_file(data.file_id, sheet=data.sheet)
        except Exception as e:
            if isinstance(e, GlobScopeError):
                await audit_file_access_denied(runtime_ctx, data.connection_id, data.file_id, str(e))
                err = str(e)
            else:
                from app.ai.tools.implementations._file_tool_common import friendly_tool_error as _fte
                _cn = getattr(getattr(client, "_bow_connection", None), "name", "") or ""
                err = _fte(self._operation_name, _cn, e)
            yield self._fail_read(data, err)
            return

        # The connector handed us the source bytes alongside the extracted text
        # (DocumentText), from the one and only fetch. Captured here because the
        # garbled-text escalation below reassigns `payload`. Re-downloading a
        # 20 MB object from S3/SharePoint just to render a preview is not an
        # acceptable cost, so this is the ONLY place the bytes come from.
        source_raw = getattr(payload, "raw", None)
        source_raw_mime = getattr(payload, "mime", "") or None

        # Note pages (OneNote) are text PLUS embedded media. The images are
        # separate Graph resources, not bytes inside the payload, so — unlike
        # every other source — they cannot be recovered by re-rendering the
        # payload later. The client hands both over at once and we split them
        # here: text goes through the normal render path, images ride the
        # existing vision channel via `prerendered`.
        note_images: list = []
        note_path: Optional[str] = None
        if isinstance(payload, dict) and payload.get("__note_page__"):
            note_images = list(payload.get("images") or [])
            # A OneNote page id is an opaque Graph token, so without the path
            # the tool card reads "Read 1-69cc87…". The client already knows
            # the page's `Notebook/Section/Page` path — surface it as the
            # display name.
            note_path = payload.get("path") or None
            payload = payload.get("text") or ""

        # Dispatch on the file's real NAME. Opaque provider ids (Graph) carry no
        # extension, so without the connector-supplied name a scanned PDF from
        # SharePoint reached the renderer unidentifiable and never rendered —
        # and the output carried no file_name for the UI to key off either.
        render_name = (
            getattr(client, "display_name", None) if session_file is not None
            else payload_name(payload, data.file_id)
        )
        rendered = render_file_payload(
            name=render_name, payload=payload,
            max_rows=data.max_rows, max_chars=data.max_chars,
        )

        # Garbled-text escalation for rich documents. Extraction can "succeed"
        # while producing glyph soup (subset-font PDF with a broken/missing
        # ToUnicode map: renders fine, extracts as symbol salad) — a length
        # gate can't catch that. When the extracted text looks garbled, or the
        # model explicitly asked with as_images, re-fetch the ORIGINAL bytes
        # and reshape this read as binary so the vision path renders the pages.
        # Every DOC_EXTS format qualifies now that render_file_images routes
        # docx/pptx through LibreOffice; previously this was PDF-only, so
        # as_images on a Word file was silently a no-op.
        garble_note: Optional[str] = None
        # Images rendered during escalation, reused below rather than rendered
        # twice — a LibreOffice conversion is far too expensive to repeat.
        prerendered: Optional[tuple] = None
        if note_images:
            # Normalize to PNG through the same renderer everything else uses —
            # a page can embed jpeg/gif/bmp, and the vision blocks below are
            # declared as image/png.
            model = runtime_ctx.get("model")
            if model and getattr(model, "supports_vision", False) and allow_llm_see_data(runtime_ctx):
                pngs = []
                for raw_img, img_name in note_images:
                    try:
                        imgs, _total = await asyncio.to_thread(
                            render_file_images, img_name, raw_img
                        )
                        pngs.extend(png for png, _m in imgs)
                    except Exception:
                        continue
                if pngs:
                    prerendered = (pngs, None)
        if prerendered is None and rendered.get("content_type") == "text" and _doc_ext(render_name) in DOC_EXTS:
            garbled = doc_text_looks_garbled(rendered.get("text"))
            if data.as_images or garbled:
                model = runtime_ctx.get("model")
                vision_ok = bool(
                    model and getattr(model, "supports_vision", False)
                    and allow_llm_see_data(runtime_ctx)
                )
                # The read already fetched the original bytes once (source_raw,
                # captured above) — S3/SharePoint/Drive carry them on the
                # DocumentText payload. Re-fetching here made every as_images /
                # garble escalation download the object a second time. Only
                # path-backed sources (network_dir returns plain str) fall back
                # to read_raw_bytes, where the re-read is a local file open.
                raw_bytes = None
                if vision_ok:
                    raw_bytes = source_raw
                    if raw_bytes is None and hasattr(client, "read_raw_bytes"):
                        try:
                            raw = await asyncio.to_thread(client.read_raw_bytes, data.file_id)
                            raw_bytes = raw[0] if isinstance(raw, tuple) else raw
                        except Exception:
                            raw_bytes = None
                # Render BEFORE discarding the text: conversion can fail (no
                # soffice, missing format filter, corrupt file), and dropping
                # readable text for a render that never materializes would be a
                # strictly worse read than the one we started with.
                imgs, total = [], None
                if raw_bytes is not None:
                    try:
                        rimgs, total = await asyncio.to_thread(
                            render_file_images, render_name, raw_bytes
                        )
                        imgs = [png for png, _mtype in rimgs]
                    except Exception:
                        imgs, total = [], None
                if imgs:
                    payload = raw_bytes
                    rendered = {
                        "file_name": rendered.get("file_name") or render_name,
                        "content_type": "binary",
                        "byte_count": len(raw_bytes),
                        "truncated": False,
                    }
                    prerendered = (imgs, total)
                    garble_note = (
                        "extracted text was garbled (broken font encoding) — showing pages as images"
                        if garbled else "as images (as_images requested)"
                    )
                elif garbled:
                    # No vision / render unavailable / raw fetch failed: keep the
                    # text — digits and layout may still carry signal — but mark
                    # it so the model doesn't treat it as a faithful read.
                    rendered["garbled"] = True

        # Persist the file as a session attachment so the existing analysis
        # stack (inspect_data, read_excel_as_csv, create_data) can pick it up.
        # A session file is ALREADY in the space — echo its own id instead of
        # spawning a duplicate File row on every read.
        if session_file is not None:
            session_file_id = str(session_file.id)
        else:
            session_file_id = await _persist_session_file(
                runtime_ctx, file_id=data.file_id, payload=payload,
                connection_id=getattr(data, "connection_id", None),
            )

        # What the UI can actually open. A session file IS the upload, and a
        # binary read already persisted the real bytes — both are viewable as
        # they stand. A text-extracted document is the gap: its session file is
        # a .txt derivative, so keep the original alongside it, reusing the
        # bytes the read already fetched.
        if session_file is not None or rendered.get("content_type") == "binary":
            preview_file_id = session_file_id
        elif _doc_ext(render_name) == "pdf":
            preview_file_id = await persist_source_document(
                runtime_ctx, client, data.file_id,
                connection_id=data.connection_id,
                raw_bytes=source_raw,
                raw_name=render_name,
                raw_mime=source_raw_mime or "application/pdf",
            )
        else:
            # Office documents need a LibreOffice conversion before a browser
            # can show them; that runs on demand, not on every read.
            preview_file_id = None

        # Vision fallback: a file that couldn't be turned into text (scanned /
        # image-based / CID-font PDF, or a picture) comes back as binary — render
        # its pages so a vision model can read it instead of an opaque blob.
        # Extension dispatch uses the DISPLAY name (a session file's id is a
        # bare UUID that would never match _RENDERABLE_IMAGE_EXTS).
        image_pngs, pages_total = [], None
        if prerendered is not None:
            image_pngs, pages_total = prerendered
        elif rendered.get("content_type") == "binary":
            model = runtime_ctx.get("model")
            if model and getattr(model, "supports_vision", False) and allow_llm_see_data(runtime_ctx):
                try:
                    # Off the event loop: PDF rasterizing is CPU-bound and the
                    # Office path shells out to LibreOffice.
                    rendered_imgs, pages_total = await asyncio.to_thread(
                        render_file_images, render_name, payload
                    )
                    image_pngs = [png for png, _mtype in rendered_imgs]
                except Exception:
                    image_pngs, pages_total = [], None

        output, observation = await self._finalize(
            data, runtime_ctx, rendered=rendered, session_file_id=session_file_id,
            image_pngs=image_pngs, pages_total=pages_total, cached=False,
            source_name=(
                note_path
                or (getattr(client, "display_name", None) if session_file is not None else None)
            ),
            attach_images=(session_file is None),
            summary_note=garble_note,
            source_is_image=is_picture_name(render_name),
            # Only when session_file_id holds the ORIGINAL bytes: a session file
            # IS the upload, and a binary render persisted the real file. A
            # text/tabular connector read stored a .txt/.csv derivative instead,
            # which no viewer can open — that gap closes when connector
            # documents get their originals kept.
            preview_file_id=preview_file_id,
        )

        # Populate the cache. Skip un-rendered binary so a later vision-capable
        # read still gets its chance to render the pages, and skip TRUNCATED
        # renders — they're only valid for the caps of THIS call and would be
        # served verbatim to later calls asking for more (see the read-side
        # guard above).
        if (
            version
            and output.get("content_type") in ("text", "tabular", "json", "images")
            and not output.get("truncated")
        ):
            cache_rendered = {
                k: v for k, v in output.items()
                if k not in ("success", "connection_id", "file_id", "session_file_id",
                             "image_file_ids", "preview")
            }
            _file_cache.write(
                data.connection_id, data.file_id, version,
                rendered=cache_rendered, image_pngs=image_pngs, pages_total=pages_total,
            )

        yield ToolEndEvent(type="tool.end", payload={"output": output, "observation": observation})

    def _implicit_connection(self, runtime_ctx: Dict[str, Any], file_id: str):
        """The connection to read `file_id` from when the model named none, or
        None when the id must be reported as unresolved.

        Only ever the agent's SINGLE attached file connection: with two or more,
        guessing could read the wrong source, so the error names them instead.
        A uuid4 id is a conversation-attachment id by construction — if it isn't
        in this report's space it is stale or from another report, and probing a
        connector with it would swap a precise error for a provider 404.
        """
        if _looks_like_session_file_id(file_id):
            return None
        attached = attached_file_connections(runtime_ctx)
        return attached[0][1] if len(attached) == 1 else None

    def _unresolved_error(self, runtime_ctx: Dict[str, Any], file_id: str) -> str:
        """Error for an id that resolved to nothing — written so the model's next
        move is a corrected call, not another list_files. The old text ended at
        'not a file attached to this conversation', which reads as 'your id is
        wrong'; re-listing returns the same id, so the agent could loop."""
        attached = attached_file_connections(runtime_ctx)
        if not attached:
            return (
                f"'{file_id}' is not a file attached to this conversation, and no "
                "file source is attached to this agent. Pass a file id from the "
                "<files> block."
            )
        choices = describe_file_connections(attached)
        if _looks_like_session_file_id(file_id):
            return (
                f"'{file_id}' is not a file attached to this conversation (it may "
                "belong to another report). Pass a file id from the <files> block, "
                "or a file-source id from list_files/search_files together with "
                f"connection_id — attached source(s): {choices}."
            )
        return (
            f"'{file_id}' is not a file attached to this conversation — it looks "
            "like a file-source id. The id is probably fine: re-issue this SAME "
            f"{self._operation_name} call with connection_id set to the source it "
            f"came from. Do NOT re-run list_files. Attached source(s): {choices}."
        )

    def _fail_read(self, data, err: str) -> ToolEndEvent:
        return ToolEndEvent(type="tool.end", payload={
            "output": {"success": False, "connection_id": data.connection_id,
                       "file_id": data.file_id, "error": err},
            "observation": {"summary": err, "success": False},
        })

    async def _finalize(self, data, runtime_ctx, *, rendered, session_file_id,
                        image_pngs, pages_total, cached, source_name=None,
                        attach_images=True, summary_note=None,
                        source_is_image=False, preview_file_id=None):
        """Assemble the tool output + observation from a rendered payload and any
        page images. Shared by the fresh-read and cache-hit paths so both emit an
        identical shape. Materializes page images as session files (unless the
        source is itself a session file — attach_images=False) and, when the
        model supports vision, attaches them as observation image blocks.

        ``source_is_image`` says the SOURCE is a picture, not merely that it was
        rendered to one — see the image_file_ids fallback below.
        ``preview_file_id`` is the File id holding the ORIGINAL document, when
        one was kept; it is what lets the UI show a .pdf rather than a render."""
        output = {"success": True, "connection_id": data.connection_id, "file_id": data.file_id}
        output.update({k: v for k, v in (rendered or {}).items() if k != "_pages"})
        if output.get("file_name") is None:
            # Session files carry their upload name; path-shaped connector ids
            # (network_dir / s3) carry a human name — surface either so the UI
            # header isn't a truncated id. Opaque provider ids (Graph) stay
            # unset; the model-authored title covers those.
            derived = source_name or _name_from_path_id(data.file_id)
            if derived:
                output["file_name"] = derived
            else:
                output.pop("file_name", None)
        if not output.get("path"):
            p = source_name or _display_path(data.file_id)
            if p:
                output["path"] = p
        if session_file_id:
            output["session_file_id"] = session_file_id

        observation_images = None
        first_image_mime = None
        if image_pngs:
            import base64
            model = runtime_ctx.get("model")
            supports_vision = bool(model and getattr(model, "supports_vision", False))
            # A note page is text AND images: the images are embedded IN the
            # body, they don't replace it. Every other source that supplies
            # images has already been reshaped to `binary` (a page render
            # REPLACES the text), so keeping content_type=text here only
            # affects note-shaped reads — and without it the page body would be
            # dropped from the observation entirely, since only text/json/
            # tabular reads emit their content below.
            text_plus_images = output.get("content_type") == "text"
            if not text_plus_images:
                output["content_type"] = "images"
                output.pop("byte_count", None)
                output["pages_total"] = pages_total
            output["image_count"] = len(image_pngs)
            from app.ai.llm.image_utils import sniff_image_mime
            file_ids, blocks = [], []
            for i, png in enumerate(image_pngs):
                # Renders are PNG or JPEG (image_utils re-encodes heavy pages);
                # cached entries are bare bytes, so the mime comes from the
                # bytes themselves — a wrong declared media_type is a provider
                # 400.
                mime = sniff_image_mime(png)
                if first_image_mime is None:
                    first_image_mime = mime
                ext = "jpg" if mime == "image/jpeg" else "png"
                if attach_images:
                    fid = await attach_drive_file_to_session(
                        runtime_ctx, filename=f"{data.file_id}.p{i + 1}.{ext}",
                        content_bytes=png, mime_type=mime,
                    )
                    if fid:
                        file_ids.append(fid)
                if supports_vision:
                    blocks.append({"data": base64.b64encode(png).decode("utf-8"),
                                   "media_type": mime, "source_type": "base64"})
            if not attach_images and source_is_image and session_file_id:
                # The source image is already a session file — point back at it
                # instead of duplicating the bytes on every look. Gated on the
                # source actually BEING a picture: for a rendered document this
                # id is the .pdf/.docx itself, and handing it back as an image
                # id gives the UI a broken <img> and the model a wrong type.
                file_ids = [session_file_id]
            if file_ids:
                output["image_file_ids"] = file_ids
            if blocks:
                observation_images = blocks

        output["preview"] = _build_preview(
            output, preview_file_id=preview_file_id,
            first_image_mime=first_image_mime,
            source_is_image=source_is_image,
        )

        ct = output.get("content_type", "?")
        # A page-scoped read names its slice: "Read pages 10-15 of 120" is what
        # makes paging progress legible to the model across turns.
        shown_pages = output.get("pages_shown")
        bits = [
            f"Read pages {shown_pages} of {output.get('pages_total')} from {data.file_id}"
            if shown_pages else f"Read {data.file_id}",
            ct,
        ]
        if ct == "tabular":
            bits.append(f"{output.get('row_count')} rows × {output.get('col_count')} cols")
        elif ct == "images":
            bits.append(f"{output.get('image_count')} of {pages_total} page(s) as image(s) for vision")
        elif output.get("image_count"):
            bits.append(f"+{output['image_count']} embedded image(s) for vision")
        if output.get("truncated"):
            bits.append("(truncated)")
        if summary_note:
            bits.append(summary_note)
        if cached:
            bits.append("cached")
        observation = {"summary": " — ".join(bits), "success": True}
        # The content itself, bounded — without this the model receives only
        # the summary line above and re-reads the file forever.
        if ct in ("text", "json", "tabular") and allow_llm_see_data(runtime_ctx):
            details = _content_details(output, max_chars=_OBS_DETAILS_MAX_CHARS)
            if output.get("garbled"):
                details = (
                    "[warning: this document's text layer is garbled (broken "
                    "font encoding) — labels are unreadable; digits/layout may "
                    "carry partial signal. Do not treat this as a faithful "
                    "read. Re-read with as_images=true on a vision-capable "
                    "model for the real content.]\n" + (details or "")
                )
            if details:
                observation["details"] = details
        if observation_images:
            observation["images"] = observation_images
        return output, observation

    async def _persist_rendered_session(self, runtime_ctx, file_id, rendered,
                                        connection_id=None):
        """Re-materialize a session file from a cached rendered payload (text/csv/
        json) so inspect_data / create_data still work on a cache hit. Images and
        binary carry no attachable text — return None."""
        ct = (rendered or {}).get("content_type")
        if ct == "tabular" and rendered.get("csv") is not None:
            return await attach_drive_file_to_session(
                runtime_ctx, filename=_materialized_name(file_id, "csv"),
                content_bytes=rendered["csv"].encode("utf-8"), mime_type="text/csv",
                connection_id=connection_id, source_ref=file_id)
        if ct == "text" and rendered.get("text") is not None:
            return await attach_drive_file_to_session(
                runtime_ctx, filename=_materialized_name(file_id, "txt"),
                content_bytes=rendered["text"].encode("utf-8"), mime_type="text/plain",
                connection_id=connection_id, source_ref=file_id)
        if ct == "json" and rendered.get("text") is not None:
            return await attach_drive_file_to_session(
                runtime_ctx, filename=_materialized_name(file_id, "json"),
                content_bytes=rendered["text"].encode("utf-8"), mime_type="application/json",
                connection_id=connection_id, source_ref=file_id)
        return None


async def _persist_session_file(
    runtime_ctx: Dict[str, Any], *, file_id: str, payload: Any,
    connection_id: Optional[str] = None,
) -> Optional[str]:
    """Serialize the parsed payload back to bytes + attach to current report.

    Tabular  → CSV bytes, filename `<file_id>.csv`
    Text/JSON → utf-8 bytes, filename `<file_id>.txt` or `.json`
    Binary   → raw bytes under the source file's own name when the connector
               supplied one (`Document.docx`), else `<file_id>.bin`

    The name matters: `.bin` isn't in `_ATTACHABLE_BY_EXT`, so an unnamed binary
    is dropped. That's how a docx whose text extraction came up empty ended up
    with no text, no images AND no session file — nothing for the model to act
    on. Keeping the real name lands the original file in the conversation.

    Returns the resulting session File id, or None if attach was skipped.
    """
    import io
    import json
    import pandas as pd

    name: str
    content: bytes
    mime: Optional[str] = None

    if isinstance(payload, pd.DataFrame):
        buf = io.StringIO()
        payload.to_csv(buf, index=False)
        content = buf.getvalue().encode("utf-8")
        name = _materialized_name(file_id, "csv")
        mime = "text/csv"
    elif isinstance(payload, (dict, list)):
        content = json.dumps(payload, default=str, ensure_ascii=False).encode("utf-8")
        name = _materialized_name(file_id, "json")
        mime = "application/json"
    elif isinstance(payload, str):
        content = payload.encode("utf-8")
        name = _materialized_name(file_id, "txt")
        mime = "text/plain"
    elif isinstance(payload, (bytes, bytearray)):
        content = bytes(payload)
        leaf = payload_name(payload).rsplit("/", 1)[-1]
        # Unknown extension still falls through to .bin (and is skipped) rather
        # than littering the conversation with opaque blobs.
        name = leaf if "." in leaf else f"{file_id}.bin"
        mime = getattr(payload, "mime", "") or None
    else:
        return None

    return await attach_drive_file_to_session(
        runtime_ctx, filename=name, content_bytes=content, mime_type=mime,
        connection_id=connection_id, source_ref=file_id,
    )
