"""browser_extract — return the readable text of the current page.

A page longer than ``max_chars`` is saved in full as a report file (the same
path execute_mcp results take) and the agent gets a preview plus the file id,
so nothing past the inline limit is lost: read_file can page through it in this
turn or a later one.
"""
import hashlib
import logging
import re
from typing import Any, AsyncIterator, Dict, Optional, Type

from pydantic import BaseModel

from app.ai.tools.implementations._browser_policy import guard_browser_tool
from app.ai.tools.base import Tool
from app.ai.tools.metadata import ToolMetadata
from app.ai.tools.schemas import ToolEvent, ToolStartEvent, ToolEndEvent
from app.ai.tools.schemas.browser import BrowserExtractInput, BrowserOutput
from app.ai.tools.implementations._browser_common import session_manager

logger = logging.getLogger(__name__)


async def _save_full_text(text: str, url: str, title: str, runtime_ctx: Dict[str, Any]) -> Optional[Any]:
    """Save the page's full text as a report file, reusing an identical earlier save.

    Keyed on URL + content, so re-extracting an unchanged page does not write a
    second file. Returns None when saving fails: the inline preview still goes
    out, the agent just loses the overflow (the old behavior).
    """
    from app.ai.tools.implementations.execute_mcp import save_text_artifact

    tag = "browser_extract:" + hashlib.sha1(f"{url}\n{text}".encode("utf-8")).hexdigest()
    user = runtime_ctx.get("user") or runtime_ctx.get("current_user")
    report = runtime_ctx.get("report")
    # Only already-loaded files: touching an unloaded report.files would lazy-load
    # outside the async greenlet.
    loaded = (getattr(report, "__dict__", {}) or {}).get("files") or []
    for f in list(runtime_ctx.get("excel_files") or []) + list(loaded):
        if getattr(f, "source_ref", None) == tag and str(getattr(f, "user_id", "")) == str(getattr(user, "id", "")):
            return f
    stem = re.sub(r"[^\w.-]+", "_", title or "page").strip("_")[:80] or "page"
    try:
        return await save_text_artifact(runtime_ctx, f"{stem}.txt", text, source_ref=tag)
    except Exception:
        logger.warning("browser_extract: could not save the full page text", exc_info=True)
        return None


class BrowserExtractTool(Tool):
    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="browser_extract",
            description=(
                "Return the readable text content of the current browser page — use "
                "this to actually read an article/table/report after navigating, "
                "when you want the content rather than the interactive structure "
                "that browser_snapshot gives. A page longer than max_chars is "
                "saved in full to a file (text_file_id) you read with read_file."
            ),
            category="both",
            version="1.0.0",
            input_schema=BrowserExtractInput.model_json_schema(),
            output_schema=BrowserOutput.model_json_schema(),
            requires_capability="browser",
            timeout_seconds=30,
            idempotent=True,
            tags=["browser", "web", "extract", "read"],
        )

    @property
    def input_model(self) -> Type[BaseModel]:
        return BrowserExtractInput

    @property
    def output_model(self) -> Type[BaseModel]:
        return BrowserOutput

    @guard_browser_tool
    async def run_stream(self, tool_input: Dict[str, Any], runtime_ctx: Dict[str, Any]) -> AsyncIterator[ToolEvent]:
        data = BrowserExtractInput(**tool_input)
        yield ToolStartEvent(type="tool.start", payload={"title": data.title or "Reading page text"})

        s = session_manager.get(data.session_id, runtime_ctx)
        if s is None or s.page is None:
            yield ToolEndEvent(type="tool.end", payload={
                "output": BrowserOutput(success=False, error_message="No active browser session; call browser_navigate first.", error_code="no_session").model_dump(),
                "observation": {"summary": "No active browser session.", "success": False},
            })
            return

        if s.preview:
            from app.ai.tools.implementations._artifact_browser import run_artifact_operation
            try:
                result = await run_artifact_operation(s, "extract", data, runtime_ctx)
                yield ToolEndEvent(type="tool.end", payload=result)
            except Exception as e:
                from app.ai.tools.implementations._artifact_browser import artifact_operation_failure
                yield ToolEndEvent(type="tool.end", payload=await artifact_operation_failure(s, e, runtime_ctx))
            return

        try:
            text = await s.page.locator("body").inner_text(timeout=5000)
        except Exception:
            text = ""
        cur_url = s.page.url
        title = await s.page.title()
        total_chars = len(text)
        truncated = total_chars > data.max_chars
        saved = await _save_full_text(text, cur_url, title, runtime_ctx) if truncated else None
        file_id = str(saved.id) if saved is not None else None
        file_name = getattr(saved, "filename", None)
        if truncated:
            text = text[:data.max_chars] + "\n… (truncated)"

        summary = f"Extracted {total_chars} chars from {cur_url}"
        if file_id:
            summary += (
                f"; the first {data.max_chars} are inline and the FULL text is saved as {file_name},"
                f" file_id={file_id}. Read the rest with read_file(file_id='{file_id}', offset={data.max_chars})"
                " and page with next_cursor — do not re-extract or answer from the preview alone."
            )
        elif truncated:
            summary += f"; only the first {data.max_chars} are shown (the full text could not be saved)."

        out = BrowserOutput(success=True, session_id=s.session_id, url=cur_url, title=title,
                            text=text, truncated=truncated, total_chars=total_chars,
                            text_file_id=file_id, text_file_name=file_name)
        observation = {"summary": summary, "success": True,
                       "url": cur_url, "title": title, "text": text, "truncated": truncated,
                       "total_chars": total_chars}
        if file_id:
            observation.update(text_file_id=file_id, text_file_name=file_name)
        yield ToolEndEvent(type="tool.end", payload={"output": out.model_dump(), "observation": observation})
