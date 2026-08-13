"""browser_extract — return the readable text of the current page."""
from typing import Any, AsyncIterator, Dict, Type

from pydantic import BaseModel

from app.ai.tools.base import Tool
from app.ai.tools.metadata import ToolMetadata
from app.ai.tools.schemas import ToolEvent, ToolStartEvent, ToolEndEvent
from app.ai.tools.schemas.browser import BrowserExtractInput, BrowserOutput
from app.ai.tools.implementations._browser_common import (
    get_browser_connection,
    redact_secrets,
    session_manager,
)


class BrowserExtractTool(Tool):
    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="browser_extract",
            description=(
                "Return the readable text content of the current browser page — use "
                "this to actually read an article/table/report after navigating, "
                "when you want the content rather than the interactive structure "
                "that browser_snapshot gives."
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

    async def run_stream(self, tool_input: Dict[str, Any], runtime_ctx: Dict[str, Any]) -> AsyncIterator[ToolEvent]:
        data = BrowserExtractInput(**tool_input)
        yield ToolStartEvent(type="tool.start", payload={"title": data.title or "Reading page text"})

        ctx = await get_browser_connection(runtime_ctx)
        s = session_manager.get(ctx.session_key) if ctx is not None else None
        if s is None or s.page is None:
            yield ToolEndEvent(type="tool.end", payload={
                "output": BrowserOutput(success=False, error_message="No active browser session; call browser_navigate first.", error_code="no_session").model_dump(),
                "observation": {"summary": "No active browser session.", "success": False},
            })
            return

        try:
            text = await s.page.locator("body").inner_text(timeout=5000)
        except Exception:
            text = ""
        truncated = False
        if len(text) > data.max_chars:
            text = text[:data.max_chars] + "\n… (truncated)"
            truncated = True
        text = redact_secrets(text, s.secrets)
        cur_url = redact_secrets(s.page.url, s.secrets)
        title = redact_secrets(await s.page.title(), s.secrets)
        out = BrowserOutput(success=True, session_id=s.session_id, url=cur_url, title=title,
                            text=text, truncated=truncated)
        yield ToolEndEvent(type="tool.end", payload={
            "output": out.model_dump(),
            "observation": {"summary": f"Extracted {len(text)} chars from {cur_url}", "success": True,
                            "url": cur_url, "title": title, "text": text, "truncated": truncated},
        })
