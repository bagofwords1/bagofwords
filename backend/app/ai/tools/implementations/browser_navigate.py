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
    describe_available_secrets,
    detect_block,
    get_browser_connection,
    redact_secrets,
    resolve_secret_placeholders,
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
                "inspect_data / read_excel_as_csv. If the connection defines secret "
                "parameters, reference them as {{secret:NAME}} inside the URL (e.g. "
                "…?token={{secret:API_TOKEN}}) — the real value is substituted "
                "server-side and never shown to you."
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

        ctx = await get_browser_connection(runtime_ctx)
        if ctx is None:
            yield self._fail("No browser connection is attached to this report.", "no_connection")
            return

        # Substitute {{secret:KEY}} placeholders server-side; the model only
        # ever sees the placeholder form (data.url) in errors and outputs.
        target_url, used_keys, missing_keys = resolve_secret_placeholders(data.url, ctx.secrets)
        if missing_keys:
            yield self._fail(
                f"Unknown secret parameter(s) in URL: {', '.join(missing_keys)}. "
                f"On this connection, {describe_available_secrets(ctx)}.",
                "missing_secret", url=data.url,
            )
            return

        if not url_matches_patterns(target_url, ctx.patterns):
            allowed = "; ".join(ctx.patterns[:15]) if ctx.patterns else "(none configured)"
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
            s = await session_manager.open(ctx)
            await session_manager.track_downloads(s, runtime_ctx)
        except Exception as e:
            yield self._fail(f"Could not start the browser: {e}", "launch_failed")
            return

        yield ToolProgressEvent(type="tool.progress", payload={"stage": "navigating"})
        try:
            s.pending_downloads = []
            resp = await s.page.goto(target_url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
            status = resp.status if resp else None
        except Exception as e:
            yield self._fail(
                f"Navigation failed: {redact_secrets(str(e), s.secrets)}",
                "nav_failed", session_id=s.session_id, url=data.url,
            )
            return
        if used_keys:
            s.secret_injected = True

        snap, truncated = await build_snapshot(s.page)
        snap = redact_secrets(snap, s.secrets)
        blocked = await detect_block(s.page)
        cur_url = redact_secrets(s.page.url, s.secrets)
        title = redact_secrets(await s.page.title(), s.secrets)

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
        observation = {
            "summary": summary, "success": True, "url": cur_url, "title": title,
            "snapshot": snap, "blocked_reason": blocked,
            "downloads": s.pending_downloads or None,
            "session_id": s.session_id,
        }
        # Tell the model which secret NAMES it may reference (values never appear).
        if ctx.secrets or ctx.user_secret_keys:
            observation["secret_parameters"] = describe_available_secrets(ctx)
        yield ToolEndEvent(type="tool.end", payload={
            "output": out.model_dump(),
            "observation": observation,
        })
