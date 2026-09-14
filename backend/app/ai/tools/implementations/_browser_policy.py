"""Enforce browser authorization at entry and at the evidence boundary."""
from functools import wraps
import logging

from app.ai.tools.schemas import ToolEndEvent
from app.ai.tools.schemas.browser import BrowserOutput
from app.ai.tools.implementations._browser_common import get_browser_connection, session_manager, url_matches_patterns
from app.services.artifact_verification_policy import require_browser_access

logger = logging.getLogger(__name__)


class _SessionScopeError(PermissionError):
    pass


def guard_browser_tool(run_stream):
    @wraps(run_stream)
    async def guarded(self, tool_input, runtime_ctx):
        # Validate before interpreting IDs, and never mutate the agent's shared ctx.
        data = self.input_model(**tool_input)
        ctx = dict(runtime_ctx)
        artifact_id = getattr(data, "artifact_id", None)
        session_id = getattr(data, "session_id", None)

        async def check(*, returning=False):
            nonlocal artifact_id
            try:
                session = session_manager.get(session_id, ctx, strict=True) if session_id else None
            except PermissionError as exc:
                raise _SessionScopeError from exc
            if session and session.preview:
                if getattr(data, "url", None):
                    raise _SessionScopeError
                artifact_id = session.preview.artifact["id"]
            policy = await require_browser_access(ctx, artifact_id)
            if not artifact_id:
                ctx["report"] = policy.report
                if session:
                    session.patterns, session.allow_downloads = get_browser_connection(ctx)
                    # URL navigation can recover from a blocked/failed page;
                    # reading that page or returning its evidence cannot.
                    reads_current_page = returning or self.metadata.name != "browser_navigate"
                    if (reads_current_page and session.page and session.page.url != "about:blank"
                            and not url_matches_patterns(session.page.url, session.patterns)):
                        raise PermissionError("The current page is no longer allowed by the browser connection")

        try:
            await check()
            async for event in run_stream(self, tool_input, ctx):
                if event.type == "tool.end":
                    # Re-resolve the new session too, including navigation failures.
                    session_id = (event.payload.get("output") or {}).get("session_id") or session_id
                    await check(returning=True)
                yield event
        except Exception as exc:
            if not isinstance(exc, PermissionError):
                logger.warning("Browser operation or access check failed", exc_info=True)
            if not isinstance(exc, _SessionScopeError):
                await session_manager.close_preview(ctx)
                if session_id:
                    session = session_manager.get(session_id, ctx)
                    if session:
                        await session_manager._close(session_id)
            denied = isinstance(exc, PermissionError)
            message = ("Browser access is restricted by the current organization policy or report access."
                       if denied else "Browser access could not be verified. Retry verification.")
            code = "preview_restricted" if denied else "preview_unavailable"
            if isinstance(exc, _SessionScopeError):
                message = "This browser session cannot be used in the current scope."
                code = "session_scope_mismatch"
            output = BrowserOutput(success=False, error_message=message,
                                   error_code=code).model_dump()
            yield ToolEndEvent(type="tool.end", payload={"output": output,
                "observation": {"success": False, "summary": message}})
    return guarded
