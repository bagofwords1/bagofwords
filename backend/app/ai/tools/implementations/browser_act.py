"""browser_act — interact with an element from the latest snapshot (by ref).

Actions: click, type, press, hover, select, scroll. Elements are addressed by
the ref from the most recent snapshot; a ref that no longer resolves returns a
typed `stale_ref` error so the model re-snapshots instead of blindly retrying.

Phase 1 note: this is the one *write* tool. It only operates inside a page the
allowlist already permits. A per-agent "confirm before interacting" policy is a
planned follow-up (via the ConnectionTool overlay), not wired here yet.
"""
from typing import Any, AsyncIterator, Dict, Type

from pydantic import BaseModel

from app.ai.tools.base import Tool
from app.ai.tools.metadata import ToolMetadata
from app.ai.tools.schemas import ToolEvent, ToolStartEvent, ToolProgressEvent, ToolEndEvent
from app.ai.tools.schemas.browser import BrowserActInput, BrowserOutput
from app.ai.tools.implementations._browser_common import (
    ACTION_TIMEOUT_MS,
    build_snapshot,
    describe_available_secrets,
    detect_block,
    get_browser_connection,
    redact_secrets,
    resolve_secret_placeholders,
    session_manager,
)

_ACTIONS = {"click", "type", "press", "hover", "select", "scroll"}


class BrowserActTool(Tool):
    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="browser_act",
            description=(
                "Interact with an element on the current browser page. `ref` is an "
                "element ref from the most recent snapshot (e.g. 'e12'). `action` is "
                "one of click, type, press, hover, select, scroll. For 'type' put the "
                "text in `text`; for 'press' put the key (e.g. 'Enter') in `text`; for "
                "'select' put the option label/value in `text`. To fill a field with a "
                "secret parameter without seeing its value, use a {{secret:NAME}} "
                "placeholder as the text (e.g. action='type', text='{{secret:API_TOKEN}}') "
                "— substitution happens server-side. Returns a fresh "
                "snapshot. If a ref is stale, re-run browser_snapshot and use the new ref."
            ),
            category="both",
            version="1.0.0",
            input_schema=BrowserActInput.model_json_schema(),
            output_schema=BrowserOutput.model_json_schema(),
            requires_capability="browser",
            timeout_seconds=ACTION_TIMEOUT_MS // 1000 + 20,
            tags=["browser", "web", "act", "click", "type"],
        )

    @property
    def input_model(self) -> Type[BaseModel]:
        return BrowserActInput

    @property
    def output_model(self) -> Type[BaseModel]:
        return BrowserOutput

    def _fail(self, msg: str, code: str, **extra) -> ToolEndEvent:
        return ToolEndEvent(type="tool.end", payload={
            "output": BrowserOutput(success=False, error_message=msg, error_code=code, **extra).model_dump(),
            "observation": {"summary": msg, "success": False, "error_code": code},
        })

    async def run_stream(self, tool_input: Dict[str, Any], runtime_ctx: Dict[str, Any]) -> AsyncIterator[ToolEvent]:
        data = BrowserActInput(**tool_input)
        action = (data.action or "").strip().lower()
        yield ToolStartEvent(type="tool.start", payload={
            "title": data.title or f"{action or 'act'} on {data.ref}", "action": action, "ref": data.ref,
        })

        if action not in _ACTIONS:
            yield self._fail(f"Unknown action '{data.action}'. Use one of: {', '.join(sorted(_ACTIONS))}.", "bad_action")
            return

        # The session is addressed by the caller's identity, not by the model-
        # provided session_id — sessions may hold injected credentials.
        ctx = await get_browser_connection(runtime_ctx)
        s = session_manager.get(ctx.session_key) if ctx is not None else None
        if s is None or s.page is None:
            yield self._fail("No active browser session; call browser_navigate first.", "no_session")
            return

        # Resolve {{secret:KEY}} placeholders in the text server-side.
        text, used_keys, missing_keys = resolve_secret_placeholders(data.text or "", s.secrets)
        if missing_keys:
            yield self._fail(
                f"Unknown secret parameter(s): {', '.join(missing_keys)}. "
                f"On this connection, {describe_available_secrets(ctx)}.",
                "missing_secret", session_id=s.session_id,
            )
            return

        page = s.page
        s.pending_downloads = []
        locator = page.locator(f"aria-ref={data.ref}")
        yield ToolProgressEvent(type="tool.progress", payload={"stage": action})

        try:
            if action == "click":
                await locator.click(timeout=ACTION_TIMEOUT_MS)
            elif action == "type":
                await locator.fill(text, timeout=ACTION_TIMEOUT_MS)
            elif action == "press":
                await locator.press(text or "Enter", timeout=ACTION_TIMEOUT_MS)
            elif action == "hover":
                await locator.hover(timeout=ACTION_TIMEOUT_MS)
            elif action == "select":
                await locator.select_option(text, timeout=ACTION_TIMEOUT_MS)
            elif action == "scroll":
                await locator.scroll_into_view_if_needed(timeout=ACTION_TIMEOUT_MS)
            if used_keys:
                s.secret_injected = True
        except Exception as e:
            emsg = redact_secrets(str(e), s.secrets)
            # A ref that no longer resolves is the dominant failure mode.
            if "aria-ref" in emsg or "resolve" in emsg.lower() or "not found" in emsg.lower() or "Timeout" in emsg:
                yield self._fail(
                    f"Ref '{data.ref}' did not resolve — the page changed. Re-run browser_snapshot and use a fresh ref.",
                    "stale_ref", session_id=s.session_id, url=page.url,
                )
                return
            yield self._fail(f"Action failed: {emsg}", "action_failed", session_id=s.session_id, url=page.url)
            return

        # Let the page settle after an interaction.
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=ACTION_TIMEOUT_MS)
        except Exception:
            pass

        snap, truncated = await build_snapshot(page)
        snap = redact_secrets(snap, s.secrets)
        blocked = await detect_block(page)
        cur_url = redact_secrets(page.url, s.secrets)
        title = redact_secrets(await page.title(), s.secrets)
        out = BrowserOutput(
            success=True, session_id=s.session_id, url=cur_url, title=title,
            snapshot=snap, truncated=truncated, blocked_reason=blocked,
            downloads=s.pending_downloads or None,
        )
        yield ToolEndEvent(type="tool.end", payload={
            "output": out.model_dump(),
            "observation": {
                "summary": f"{action} on {data.ref} → {cur_url}", "success": True,
                "url": cur_url, "title": title, "snapshot": snap, "blocked_reason": blocked,
                "downloads": s.pending_downloads or None,
            },
        })
