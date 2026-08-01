"""browser_snapshot — re-read the current page as an accessibility tree with refs."""
from typing import Any, AsyncIterator, Dict, Type

from pydantic import BaseModel

from app.ai.tools.base import Tool
from app.ai.tools.metadata import ToolMetadata
from app.ai.tools.schemas import ToolEvent, ToolStartEvent, ToolEndEvent
from app.ai.tools.schemas.browser import BrowserSnapshotInput, BrowserOutput
from app.ai.tools.implementations._browser_common import build_snapshot, detect_block, session_manager


class BrowserSnapshotTool(Tool):
    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="browser_snapshot",
            description=(
                "Re-read the current browser page as an accessibility tree where "
                "every interactive element has a ref (e.g. [ref=e12]). Call this "
                "after a browser_act that changed the page — refs from an older "
                "snapshot are invalid once the page navigates or its DOM changes."
            ),
            category="both",
            version="1.0.0",
            input_schema=BrowserSnapshotInput.model_json_schema(),
            output_schema=BrowserOutput.model_json_schema(),
            requires_capability="browser",
            timeout_seconds=30,
            idempotent=True,
            tags=["browser", "web", "snapshot"],
        )

    @property
    def input_model(self) -> Type[BaseModel]:
        return BrowserSnapshotInput

    @property
    def output_model(self) -> Type[BaseModel]:
        return BrowserOutput

    async def run_stream(self, tool_input: Dict[str, Any], runtime_ctx: Dict[str, Any]) -> AsyncIterator[ToolEvent]:
        data = BrowserSnapshotInput(**tool_input)
        yield ToolStartEvent(type="tool.start", payload={"title": data.title or "Reading the page"})

        s = session_manager.get(data.session_id)
        if s is None or s.page is None:
            yield ToolEndEvent(type="tool.end", payload={
                "output": BrowserOutput(success=False, error_message="No active browser session; call browser_navigate first.", error_code="no_session").model_dump(),
                "observation": {"summary": "No active browser session.", "success": False},
            })
            return

        snap, truncated = await build_snapshot(s.page, full=data.full, max_chars=data.max_chars)
        blocked = await detect_block(s.page)
        cur_url = s.page.url
        title = await s.page.title()
        out = BrowserOutput(success=True, session_id=s.session_id, url=cur_url, title=title,
                            snapshot=snap, truncated=truncated, blocked_reason=blocked)
        yield ToolEndEvent(type="tool.end", payload={
            "output": out.model_dump(),
            "observation": {"summary": f"Snapshot of {cur_url}", "success": True,
                            "url": cur_url, "title": title, "snapshot": snap, "blocked_reason": blocked},
        })
