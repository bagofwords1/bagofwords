"""browser_navigate — open (or reuse) the report's browser session at a URL."""
from typing import Any, AsyncIterator, Dict, Optional, Type

from pydantic import BaseModel

from app.ai.tools.base import Tool
from app.ai.tools.metadata import ToolMetadata
from app.ai.tools.schemas import ToolEvent, ToolStartEvent, ToolProgressEvent, ToolEndEvent
from app.ai.tools.schemas.browser import BrowserNavigateInput, BrowserOutput
from app.ai.tools.implementations._browser_common import (
    NAV_TIMEOUT_MS,
    build_snapshot,
    detect_block,
    get_browser_connection,
    session_manager,
    url_matches_patterns,
)


class BrowserNavigateTool(Tool):
    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="browser_navigate",
            description=(
                "Open a web page in a real browser and return an accessibility "
                "snapshot of it. Use this to start a browsing task or to move to a "
                "new URL. The URL must be within the browser connection's allowlist. "
                "Returns a session_id — pass it to browser_snapshot / browser_act / "
                "browser_extract / browser_vision to keep working in the same page. "
                "Downloaded files become report files you can then read with "
                "inspect_data / read_excel_as_csv."
            ),
            category="both",
            version="1.0.0",
            input_schema=BrowserNavigateInput.model_json_schema(),
            output_schema=BrowserOutput.model_json_schema(),
            requires_capability="browser",
            timeout_seconds=NAV_TIMEOUT_MS // 1000 + 20,
            tags=["browser", "web", "navigate"],
        )

    @property
    def input_model(self) -> Type[BaseModel]:
        return BrowserNavigateInput

    @property
    def output_model(self) -> Type[BaseModel]:
        return BrowserOutput

    def _fail(self, msg: str, code: str, **extra) -> ToolEndEvent:
        return ToolEndEvent(type="tool.end", payload={
            "output": BrowserOutput(success=False, error_message=msg, error_code=code, **extra).model_dump(),
            "observation": {"summary": msg, "success": False},
        })

    async def run_stream(self, tool_input: Dict[str, Any], runtime_ctx: Dict[str, Any]) -> AsyncIterator[ToolEvent]:
        data = BrowserNavigateInput(**tool_input)
        yield ToolStartEvent(type="tool.start", payload={"title": data.title or f"Opening {data.url}", "url": data.url})

        conn = get_browser_connection(runtime_ctx)
        if conn is None:
            yield self._fail("No browser connection is attached to this report.", "no_connection")
            return
        patterns, allow_downloads = conn

        if not url_matches_patterns(data.url, patterns):
            allowed = "; ".join(patterns[:15]) if patterns else "(none configured)"
            yield self._fail(
                f"{data.url} is outside this browser connection's allowed URLs. "
                f"You may only open URLs matching one of these patterns: {allowed}. "
                f"Retry browser_navigate with a URL that matches, or tell the user the "
                f"page they want is not in the allowlist.",
                "not_allowed", url=data.url, blocked_reason="allowlist",
            )
            return

        yield ToolProgressEvent(type="tool.progress", payload={"stage": "launching"})
        try:
            s = await session_manager.open(str(runtime_ctx["report"].id), patterns, allow_downloads)
            await session_manager.track_downloads(s, runtime_ctx)
        except Exception as e:
            yield self._fail(f"Could not start the browser: {e}", "launch_failed")
            return

        yield ToolProgressEvent(type="tool.progress", payload={"stage": "navigating"})
        try:
            s.pending_downloads = []
            resp = await s.page.goto(data.url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
            status = resp.status if resp else None
        except Exception as e:
            yield self._fail(f"Navigation failed: {e}", "nav_failed", session_id=s.session_id, url=data.url)
            return

        snap, truncated = await build_snapshot(s.page)
        blocked = await detect_block(s.page)
        cur_url = s.page.url
        title = await s.page.title()

        summary = f"Opened {cur_url}"
        if status:
            summary += f" ({status})"
        if blocked:
            summary += f" — blocked: {blocked}"

        out = BrowserOutput(
            success=True, session_id=s.session_id, url=cur_url, title=title,
            snapshot=snap, truncated=truncated, blocked_reason=blocked,
            downloads=s.pending_downloads or None,
        )
        yield ToolEndEvent(type="tool.end", payload={
            "output": out.model_dump(),
            "observation": {
                "summary": summary, "success": True, "url": cur_url, "title": title,
                "snapshot": snap, "blocked_reason": blocked,
                "downloads": s.pending_downloads or None,
                "session_id": s.session_id,
            },
        })
