"""BOW's own preview of an app, driven directly (no model): open the saved
version in the internal viewer, click a control by its visible name, and print
what the app did through bow.* -- the same evidence the agent's browser tools
get (evidence.sdk_calls / evidence.errors) -- and the preview rows kept in the
app's activity log.

  TESTING=true TEST_DATABASE_URL=... BOW_ENCRYPTION_KEY=... \\
  PLAYWRIGHT_BROWSERS_PATH=... uv run python ../tools/agent/embed/preview_sdk_check.py \\
      <report_id> <artifact_version_id> <user_id> "<button name>" ["<text to type>"]
Run from backend/ with the same environment as the running backend.
"""
import asyncio
import json
import re
import sys


async def main(report_id, version_id, user_id, button, text=None):
    import main as _app  # noqa: F401  (registers every model; the preview dispatches into this app)
    from app.ai.tools.implementations.browser_act import BrowserActTool
    from app.ai.tools.implementations.browser_navigate import BrowserNavigateTool
    from app.ai.tools.implementations._browser_common import session_manager
    from app.dependencies import async_session_maker
    from app.models.organization import Organization
    from app.models.report import Report
    from app.models.user import User

    if "--debug" in sys.argv:
        from app.services.artifact_preview_service import ArtifactPreviewService
        original = ArtifactPreviewService._api

        async def traced(self, route, path, query):
            print("api:", route.request.method, path)
            return await original(self, route, path, query)
        ArtifactPreviewService._api = traced

    async with async_session_maker() as db:
        report = await db.get(Report, report_id)
        ctx = {"report": report, "user": await db.get(User, user_id),
               "organization": await db.get(Organization, str(report.organization_id))}

    async def run(tool, args):
        out = None
        async for event in tool.run_stream(args, ctx):
            if event.type == "tool.end":
                out = event.payload["output"]
        return out

    opened = await run(BrowserNavigateTool(), {"artifact_id": version_id})
    session = opened.get("session_id")
    show = lambda o: {k: (o.get("evidence") or {}).get(k) for k in ("sdk_calls", "errors")} | {"success": o.get("success"), "error": o.get("error_message")}
    print("navigate:", json.dumps(show(opened), ensure_ascii=False, default=str)[:1500])
    snapshot = opened.get("snapshot") or ""
    live = session_manager._sessions.get(session)
    if live is not None and live.frame is not None:
        policy = await live.frame.evaluate("() => (window.ARTIFACT_DATA || {})._fixture_conversation_policy || null")
        print("policy in preview:", json.dumps(policy, ensure_ascii=False)[:400])
        if "--debug" in sys.argv:
            print("data keys:", await live.frame.evaluate("() => Object.keys(window.ARTIFACT_DATA || {})"))
    if text:
        box = re.search(r'textbox[^\n]*\[ref=([a-z0-9]+)\]', snapshot)
        if box:
            typed = await run(BrowserActTool(), {"session_id": session, "ref": box.group(1), "action": "type", "text": text})
            snapshot = typed.get("snapshot") or snapshot
    ref = re.search(r'button "[^"\n]*' + re.escape(button) + r'[^"\n]*"[^\n]*\[ref=([a-z0-9]+)\]', snapshot)
    if not ref:
        print("no button", button, "in snapshot:", json.dumps(snapshot[:3000], ensure_ascii=False))
        return 1
    clicked = await run(BrowserActTool(), {"session_id": session, "ref": ref.group(1), "action": "click"})
    print("click:", json.dumps(show(clicked), ensure_ascii=False, default=str)[:2500])
    await session_manager._close(session)
    return 0 if clicked.get("evidence") is not None else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main(*[a for a in sys.argv[1:] if a != "--debug"])))
