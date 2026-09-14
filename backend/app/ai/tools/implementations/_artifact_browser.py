"""Internal preview behavior shared by the five existing browser tools."""
from __future__ import annotations

import base64

from app.ai.tools.implementations._browser_common import build_snapshot, mask_secrets_style, save_bytes, session_manager
from app.services.artifact_preview_service import ArtifactPreviewService, PreviewUnavailableError


async def navigate_artifact(data, ctx):
    if data.session_id:
        raise ValueError("Open an artifact without reusing a URL session")
    preview = ArtifactPreviewService(ctx)
    try:
        await preview.open(data.artifact_id)
        # One internal target per execution; opening the repaired version
        # releases its predecessor without affecting connector sessions.
        await session_manager.close_preview(ctx)
        s = await session_manager.open(preview.report_id, [preview.url], False, runtime_ctx=ctx,
                                       preview=preview, viewport=data.viewport or {"width": 960, "height": 900})
        async def receive(source, detail):
            if source.get("frame") != s.page.main_frame or not isinstance(detail, dict):
                return
            if detail.get("artifact_id") != preview.artifact["id"]:
                return
            kind = detail.get("kind")
            if kind in {"params_commit", "params_status", "data_sent", "data_received", "error"}:
                preview.record(kind, **{k: v for k, v in detail.items() if k not in {"kind", "artifact_id", "action_id", "cursor"}})
        await s.page.expose_binding("__bowPreviewEvidence", receive)
        await s.page.add_init_script("window.addEventListener('bow:artifact-evidence', e => window.__bowPreviewEvidence(e.detail));")
        s.page.on("pageerror", lambda e: preview.record("error", source="browser", message=str(e)[:1500], stack=getattr(e, "stack", "")[:2500]))
        s.page.on("console", lambda msg: preview.record(
            "warning" if msg.text.startswith(("Warning:", "[vite] failed to connect to websocket")) else "error",
            source="console", message=msg.text[:1500]) if msg.type == "error" else None)
        s.page.on("popup", lambda p: p.close())
        await s.page.goto(preview.url, wait_until="domcontentloaded", timeout=30000)
        handle = await s.page.wait_for_selector("iframe[data-artifact-frame]", state="attached", timeout=30000)
        s.frame = await handle.content_frame()
        await s.frame.wait_for_selector("#root > *", timeout=20000)
        readiness = await preview.wait_ready()
        result = await run_artifact_operation(s, "snapshot", data, ctx)
        result["output"]["readiness"] = readiness
        result["output"]["datasets"] = preview.datasets
        result["output"]["parameters"] = parameter_manifest(preview)
        result["observation"].update(result["output"])
        return result
    except Exception as exc:
        if "s" not in locals():
            await preview.close()
            raise
        if isinstance(exc, (PermissionError, PreviewUnavailableError)):
            result = await artifact_operation_failure(s, exc, ctx)
            await session_manager._close(s.session_id)
            return result
        # Startup errors are evidence too: return them before disposing the
        # incomplete session, rather than losing them behind a timeout.
        preview.record("error", source="navigation", message=str(exc)[:1500])
        output = {"success": False, "error_code": "preview_unavailable",
                  "error_message": "Artifact preview could not become ready",
                  "artifact": preview.identity(), "evidence": preview.evidence(update_status="failed"),
                  "evidence_id": preview.evidence_id, "next_cursor": preview.cursor,
                  "readiness": "failed", "verification_group_id": ctx.get("verification_group_id")}
        await session_manager._close(s.session_id)
        return {"output": output, "observation": {**output, "summary": output["error_message"]}}



async def run_artifact_operation(s, operation, data, ctx):
    from app.ai.tools.schemas.browser import BrowserOutput
    preview = s.preview
    # The public browser tools enforce live org policy before operations and
    # before yielding evidence. The broker also rechecks the exact API target.
    await preview._get(f"/api/artifacts/{preview.artifact['id']}")
    async with s.action_lock:
        action_id = getattr(data, "evidence_for_action_id", None)
        status = None
        images = None
        screenshot_id = None
        text = None
        if operation == "act":
            if data.expect_query_update is not None:
                preview.validate_expectation(data.expect_query_update)
            action_id = preview.begin_action()
            locator = s.frame.locator(f"aria-ref={data.ref}")
            action = data.action.strip().lower()
            if action == "click":
                await locator.click(timeout=10000)
            elif action == "type":
                await locator.fill(data.text or "", timeout=10000)
            elif action == "press":
                await locator.press(data.text or "Enter", timeout=10000)
            elif action == "select":
                options = await locator.evaluate("el => Array.from(el.options || []).map(e => ({value:e.value,label:e.label}))")
                value = data.text or ""
                if not any(o["value"] == value for o in options):
                    matches = [o["value"] for o in options if o["label"] == value]
                    if len(matches) != 1:
                        raise ValueError("Select an exact option label or value from the snapshot")
                    value = matches[0]
                await locator.select_option(value, timeout=10000)
            elif action == "hover":
                await locator.hover(timeout=10000)
            elif action == "scroll":
                await locator.scroll_into_view_if_needed(timeout=10000)
            status = await preview.wait(action_id, data.expect_query_update)
        elif operation == "snapshot" and not action_id:
            status = await preview.wait(timeout=15, since=getattr(data, "since_cursor", None))
        elif action_id:
            if action_id not in preview.actions:
                raise ValueError("Unknown action in this preview")
            status = await preview.wait(action_id)
        if operation == "vision":
            await mask_secrets_style(s.frame)
            png = await s.page.locator("iframe[data-artifact-frame]").screenshot(type="png")
            f = await save_bytes(ctx, png, f"artifact-verification-{preview.artifact['version']}.png", "image/png")
            screenshot_id = str(f.id) if f else None
            if getattr(ctx.get("model"), "supports_vision", False):
                images = [{"data": base64.b64encode(png).decode(), "media_type": "image/png", "source_type": "base64"}]
        if operation == "extract":
            text = (await s.frame.locator("body").inner_text())[:data.max_chars]
        root = None
        dialogs = s.frame.locator('[role="dialog"]:visible, [role="alertdialog"]:visible')
        if await dialogs.count():
            root = dialogs.last
        snapshot, truncated = await build_snapshot(s.frame, root=root, max_chars=getattr(data, "max_chars", 8000), full=getattr(data, "full", False))
        evidence = preview.evidence(getattr(data, "since_cursor", None), status)
        group_id = ctx.get("verification_group_id") or f"verification:{preview.execution_id}:{preview.report_id}"
        output = BrowserOutput(
            success=True, session_id=s.session_id, url=preview.url, title=data.title,
            artifact=preview.identity(), action_id=action_id, snapshot=snapshot, text=text,
            truncated=truncated, screenshot_file_id=screenshot_id,
            evidence=evidence, evidence_id=preview.evidence_id, next_cursor=preview.cursor,
            verification_group_id=group_id,
        ).model_dump(exclude_none=True)
        observation = {**output, "summary": data.title or "Checked artifact state"}
        if images:
            observation["images"] = images
        return {"output": output, "observation": observation}


def parameter_manifest(preview):
    parameters = {}
    for query in preview.queries:
        for parameter in query.get("parameters", []) or []:
            key = parameter["name"]
            if key not in parameters:
                parameters[key] = dict(parameter, query_ids=[])
            parameters[key]["query_ids"].append(query["id"])
    return list(parameters.values())


async def artifact_operation_failure(session, exc, ctx):
    """Return recoverable context without leaking evidence after access denial."""
    message = str(exc)[:1500]
    output = {"success": False, "error_message": message,
              "error_code": "preview_restricted" if isinstance(exc, PermissionError) else
                            "preview_unavailable" if isinstance(exc, PreviewUnavailableError) else "artifact_action_failed"}
    if not isinstance(exc, (PermissionError, PreviewUnavailableError)):
        preview = session.preview
        output.update(session_id=session.session_id, artifact=preview.identity(),
                      action_id=preview.action_id, evidence=preview.evidence(),
                      evidence_id=preview.evidence_id, next_cursor=preview.cursor,
                      parameters=parameter_manifest(preview),
                      verification_group_id=ctx.get("verification_group_id"))
        try:
            async with session.action_lock:
                dialogs = session.frame.locator('[role="dialog"]:visible, [role="alertdialog"]:visible')
                root = dialogs.last if await dialogs.count() else None
                output["snapshot"], output["truncated"] = await build_snapshot(session.frame, root=root)
        except Exception:
            pass  # Closed/crashed pages still retain the permitted failure evidence.
    return {"output": output, "observation": {**output, "summary": message}}
