"""browser_vision — screenshot the current page (secrets masked) → report file + vision."""
import base64
from typing import Any, AsyncIterator, Dict, Type

from pydantic import BaseModel

from app.ai.tools.base import Tool
from app.ai.tools.metadata import ToolMetadata
from app.ai.tools.schemas import ToolEvent, ToolStartEvent, ToolProgressEvent, ToolEndEvent
from app.ai.tools.schemas.browser import BrowserVisionInput, BrowserOutput
from app.ai.tools.implementations._browser_common import mask_secrets_style, save_bytes, session_manager


class BrowserVisionTool(Tool):
    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="browser_vision",
            description=(
                "Take a screenshot of the current browser page. Use this when the "
                "visual layout matters (a chart, a captcha, 'why does this look "
                "wrong') — for reading text prefer browser_extract, and for "
                "interacting prefer browser_snapshot, both of which are cheaper. The "
                "screenshot is saved as a report file (a file_id you can embed in a "
                "dashboard via create_artifact) and shown to you as an image. "
                "Secret-shaped fields are masked."
            ),
            category="both",
            version="1.0.0",
            input_schema=BrowserVisionInput.model_json_schema(),
            output_schema=BrowserOutput.model_json_schema(),
            requires_capability="browser",
            timeout_seconds=30,
            tags=["browser", "web", "screenshot", "vision"],
        )

    @property
    def input_model(self) -> Type[BaseModel]:
        return BrowserVisionInput

    @property
    def output_model(self) -> Type[BaseModel]:
        return BrowserOutput

    async def run_stream(self, tool_input: Dict[str, Any], runtime_ctx: Dict[str, Any]) -> AsyncIterator[ToolEvent]:
        data = BrowserVisionInput(**tool_input)
        yield ToolStartEvent(type="tool.start", payload={"title": data.title or "Taking a screenshot"})

        s = session_manager.get(data.session_id)
        if s is None or s.page is None:
            yield ToolEndEvent(type="tool.end", payload={
                "output": BrowserOutput(success=False, error_message="No active browser session; call browser_navigate first.", error_code="no_session").model_dump(),
                "observation": {"summary": "No active browser session.", "success": False},
            })
            return

        yield ToolProgressEvent(type="tool.progress", payload={"stage": "capturing"})
        await mask_secrets_style(s.page)
        try:
            png = await s.page.screenshot(full_page=data.full_page, type="png")
        except Exception as e:
            yield ToolEndEvent(type="tool.end", payload={
                "output": BrowserOutput(success=False, session_id=s.session_id, error_message=f"Screenshot failed: {e}", error_code="screenshot_failed").model_dump(),
                "observation": {"summary": f"Screenshot failed: {e}", "success": False},
            })
            return

        cur_url = s.page.url
        title = await s.page.title()
        yield ToolProgressEvent(type="tool.progress", payload={"stage": "saving"})
        db_file = await save_bytes(runtime_ctx, png, "browser-screenshot.png", "image/png")
        file_id = str(db_file.id) if db_file is not None else None

        out = BrowserOutput(success=True, session_id=s.session_id, url=cur_url, title=title,
                            screenshot_file_id=file_id)
        b64 = base64.b64encode(png).decode("ascii")
        yield ToolEndEvent(type="tool.end", payload={
            "output": out.model_dump(),
            "observation": {
                "summary": f"Screenshot of {cur_url}", "success": True,
                "url": cur_url, "title": title, "screenshot_file_id": file_id,
                # Fed to vision-capable models as an image block; stripped from the
                # serialized observation by the agent after extraction.
                "images": [{"data": b64, "media_type": "image/png", "source_type": "base64"}],
            },
        })
