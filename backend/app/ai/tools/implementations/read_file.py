"""read_file agent tool — read a file from a file-based data source."""
from __future__ import annotations

from typing import Any, AsyncIterator, Dict, Type

from pydantic import BaseModel

from app.ai.tools.base import Tool
from app.ai.tools.metadata import ToolMetadata
from app.ai.tools.schemas import ToolEndEvent, ToolEvent, ToolStartEvent
from app.ai.tools.schemas.file_tools import ReadFileInput, ReadFileOutput
from app.data_sources.clients.base import Capability

from app.data_sources.clients._file_source_common import GlobScopeError

from . import _file_cache
from ._file_tool_common import (
    SessionFileClient,
    allow_llm_see_data,
    attach_drive_file_to_session,
    audit_file_access_denied,
    render_file_images,
    render_file_payload,
    render_pdf_pages_images,
    resolve_file_client,
    resolve_session_file,
)

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


def _name_from_path_id(file_id: str) -> str:
    """Basename of a path-shaped file id ('logs/app/web.log' → 'web.log');
    '' for opaque provider ids (no slash and no dot in the leaf)."""
    fid = str(file_id or "")
    leaf = fid.rsplit("/", 1)[-1]
    if ("/" in fid or "." in leaf) and leaf:
        return leaf
    return ""


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


class ReadFileTool(Tool):
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
                "identical read. For big files, page with offset/length (text) or "
                "page_range (PDFs/documents). Binary files return their size only."
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
            "title": f"Reading file {data.file_id}",
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
                err = (
                    f"'{data.file_id}' is not a file attached to this conversation. "
                    "Pass a session file id from <files>, or a connection_id + "
                    "file id from list_files/search_files for a file source."
                )
                yield self._fail_read(data, err)
                return
            client = SessionFileClient(session_file)
        else:
            client, err = await resolve_file_client(
                runtime_ctx, data.connection_id, Capability.READ_FILE
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
                    err = f"read_file (windowed) failed: {e}"
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
                    yield self._fail_read(data, f"read_file (page_range) failed: {e}")
                return
            if not isinstance(paged, dict) or not paged.get("__doc_pages__"):
                yield self._fail_read(
                    data, "This connection does not support page_range reads."
                )
                return
            shown = f"{paged.get('first')}-{paged.get('last')}"

            # Scanned/image-only pages: no usable text came back — rasterize
            # the REQUESTED pages for a vision model instead of returning an
            # empty read (the page-level analogue of the whole-file vision
            # fallback below).
            from app.data_sources.clients._document_text import doc_text_is_usable
            model = runtime_ctx.get("model")
            if (
                not doc_text_is_usable(paged.get("text"))
                and model and getattr(model, "supports_vision", False)
                and allow_llm_see_data(runtime_ctx)
            ):
                import asyncio as _asyncio
                import base64 as _b64
                try:
                    raw = await _asyncio.to_thread(client.read_raw_bytes, data.file_id)
                    raw_bytes = raw[0] if isinstance(raw, tuple) else raw
                    imgs, total = render_pdf_pages_images(raw_bytes, rng[0], rng[1])
                except Exception as e:
                    imgs, total = [], paged.get("pages_total")
                if imgs:
                    output = {
                        "success": True,
                        "connection_id": data.connection_id,
                        "file_id": data.file_id,
                        "content_type": "images",
                        "image_count": len(imgs),
                        "pages_total": total,
                        "pages_shown": shown,
                    }
                    blocks = [
                        {"data": _b64.b64encode(png).decode("utf-8"),
                         "media_type": mtype, "source_type": "base64"}
                        for png, mtype in imgs
                    ]
                    yield ToolEndEvent(type="tool.end", payload={
                        "output": output,
                        "observation": {
                            "summary": (
                                f"Read pages {shown} of {total} from {data.file_id} "
                                "as image(s) for vision (no extractable text)"
                            ),
                            "success": True,
                            "images": blocks,
                        },
                    })
                    return

            output = {
                "success": True,
                "connection_id": data.connection_id,
                "file_id": data.file_id,
                "content_type": "text",
                "text": paged.get("text") or "",
                "pages_total": paged.get("pages_total"),
                "pages_shown": shown,
            }
            name = (
                getattr(client, "display_name", None) if session_file is not None
                else _name_from_path_id(data.file_id)
            )
            if name:
                output["file_name"] = name
            summary = (
                f"Read pages {shown} of {paged.get('pages_total')} from {data.file_id}"
            )
            observation = {"summary": summary, "success": True}
            if allow_llm_see_data(runtime_ctx):
                details = _content_details(output, max_chars=_OBS_DETAILS_MAX_CHARS)
                if details:
                    observation["details"] = details
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

        if version:
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
                session_file_id = await self._persist_rendered_session(runtime_ctx, data.file_id, rendered)
                output, observation = await self._finalize(
                    data, runtime_ctx, rendered=rendered, session_file_id=session_file_id,
                    image_pngs=cached.get("image_bytes") or [], pages_total=cached.get("pages_total"),
                    cached=True,
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
                err = f"read_file failed: {e}"
            yield self._fail_read(data, err)
            return

        rendered = render_file_payload(
            name=None, payload=payload, max_rows=data.max_rows, max_chars=data.max_chars
        )

        # Persist the file as a session attachment so the existing analysis
        # stack (inspect_data, read_excel_as_csv, create_data) can pick it up.
        # A session file is ALREADY in the space — echo its own id instead of
        # spawning a duplicate File row on every read.
        if session_file is not None:
            session_file_id = str(session_file.id)
        else:
            session_file_id = await _persist_session_file(
                runtime_ctx, file_id=data.file_id, payload=payload,
            )

        # Vision fallback: a file that couldn't be turned into text (scanned /
        # image-based / CID-font PDF, or a picture) comes back as binary — render
        # its pages so a vision model can read it instead of an opaque blob.
        # Extension dispatch uses the DISPLAY name (a session file's id is a
        # bare UUID that would never match _RENDERABLE_IMAGE_EXTS).
        image_pngs, pages_total = [], None
        if rendered.get("content_type") == "binary":
            model = runtime_ctx.get("model")
            if model and getattr(model, "supports_vision", False) and allow_llm_see_data(runtime_ctx):
                render_name = (
                    getattr(client, "display_name", None)
                    if session_file is not None else data.file_id
                )
                try:
                    rendered_imgs, pages_total = render_file_images(render_name, payload)
                    image_pngs = [png for png, _mtype in rendered_imgs]
                except Exception:
                    image_pngs, pages_total = [], None

        output, observation = await self._finalize(
            data, runtime_ctx, rendered=rendered, session_file_id=session_file_id,
            image_pngs=image_pngs, pages_total=pages_total, cached=False,
            source_name=(getattr(client, "display_name", None) if session_file is not None else None),
            attach_images=(session_file is None),
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
                if k not in ("success", "connection_id", "file_id", "session_file_id", "image_file_ids")
            }
            _file_cache.write(
                data.connection_id, data.file_id, version,
                rendered=cache_rendered, image_pngs=image_pngs, pages_total=pages_total,
            )

        yield ToolEndEvent(type="tool.end", payload={"output": output, "observation": observation})

    def _fail_read(self, data, err: str) -> ToolEndEvent:
        return ToolEndEvent(type="tool.end", payload={
            "output": {"success": False, "connection_id": data.connection_id,
                       "file_id": data.file_id, "error": err},
            "observation": {"summary": err, "success": False},
        })

    async def _finalize(self, data, runtime_ctx, *, rendered, session_file_id,
                        image_pngs, pages_total, cached, source_name=None,
                        attach_images=True):
        """Assemble the tool output + observation from a rendered payload and any
        page images. Shared by the fresh-read and cache-hit paths so both emit an
        identical shape. Materializes page images as session files (unless the
        source is itself a session file — attach_images=False) and, when the
        model supports vision, attaches them as observation image blocks."""
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
        if session_file_id:
            output["session_file_id"] = session_file_id

        observation_images = None
        if image_pngs:
            import base64
            model = runtime_ctx.get("model")
            supports_vision = bool(model and getattr(model, "supports_vision", False))
            output["content_type"] = "images"
            output.pop("byte_count", None)
            output["image_count"] = len(image_pngs)
            output["pages_total"] = pages_total
            file_ids, blocks = [], []
            for i, png in enumerate(image_pngs):
                if attach_images:
                    fid = await attach_drive_file_to_session(
                        runtime_ctx, filename=f"{data.file_id}.p{i + 1}.png",
                        content_bytes=png, mime_type="image/png",
                    )
                    if fid:
                        file_ids.append(fid)
                if supports_vision:
                    blocks.append({"data": base64.b64encode(png).decode("utf-8"),
                                   "media_type": "image/png", "source_type": "base64"})
            if not attach_images and session_file_id:
                # The source image is already a session file — point back at it
                # instead of duplicating the bytes on every look.
                file_ids = [session_file_id]
            if file_ids:
                output["image_file_ids"] = file_ids
            if blocks:
                observation_images = blocks

        ct = output.get("content_type", "?")
        bits = [f"Read {data.file_id}", ct]
        if ct == "tabular":
            bits.append(f"{output.get('row_count')} rows × {output.get('col_count')} cols")
        elif ct == "images":
            bits.append(f"{output.get('image_count')} of {pages_total} page(s) as image(s) for vision")
        if output.get("truncated"):
            bits.append("(truncated)")
        if cached:
            bits.append("cached")
        observation = {"summary": " — ".join(bits), "success": True}
        # The content itself, bounded — without this the model receives only
        # the summary line above and re-reads the file forever.
        if ct in ("text", "json", "tabular") and allow_llm_see_data(runtime_ctx):
            details = _content_details(output, max_chars=_OBS_DETAILS_MAX_CHARS)
            if details:
                observation["details"] = details
        if observation_images:
            observation["images"] = observation_images
        return output, observation

    async def _persist_rendered_session(self, runtime_ctx, file_id, rendered):
        """Re-materialize a session file from a cached rendered payload (text/csv/
        json) so inspect_data / create_data still work on a cache hit. Images and
        binary carry no attachable text — return None."""
        ct = (rendered or {}).get("content_type")
        if ct == "tabular" and rendered.get("csv") is not None:
            return await attach_drive_file_to_session(
                runtime_ctx, filename=f"{file_id}.csv",
                content_bytes=rendered["csv"].encode("utf-8"), mime_type="text/csv")
        if ct == "text" and rendered.get("text") is not None:
            return await attach_drive_file_to_session(
                runtime_ctx, filename=f"{file_id}.txt",
                content_bytes=rendered["text"].encode("utf-8"), mime_type="text/plain")
        if ct == "json" and rendered.get("text") is not None:
            return await attach_drive_file_to_session(
                runtime_ctx, filename=f"{file_id}.json",
                content_bytes=rendered["text"].encode("utf-8"), mime_type="application/json")
        return None


async def _persist_session_file(
    runtime_ctx: Dict[str, Any], *, file_id: str, payload: Any,
) -> Optional[str]:
    """Serialize the parsed payload back to bytes + attach to current report.

    Tabular  → CSV bytes, filename `<file_id>.csv`
    Text/JSON → utf-8 bytes, filename `<file_id>.txt` or `.json`
    Binary   → raw bytes, filename `<file_id>.bin`

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
        name = f"{file_id}.csv"
        mime = "text/csv"
    elif isinstance(payload, (dict, list)):
        content = json.dumps(payload, default=str, ensure_ascii=False).encode("utf-8")
        name = f"{file_id}.json"
        mime = "application/json"
    elif isinstance(payload, str):
        content = payload.encode("utf-8")
        name = f"{file_id}.txt"
        mime = "text/plain"
    elif isinstance(payload, (bytes, bytearray)):
        content = bytes(payload)
        name = f"{file_id}.bin"  # _ATTACHABLE_BY_EXT skips .bin → won't persist
    else:
        return None

    return await attach_drive_file_to_session(
        runtime_ctx, filename=name, content_bytes=content, mime_type=mime,
    )
