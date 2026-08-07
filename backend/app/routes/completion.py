from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import Optional
from app.dependencies import get_db
from app.services.completion_service import CompletionService
from app.schemas.completion_v2_schema import CompletionCreate, CompletionContextEstimateSchema
from app.schemas.sse_schema import SSEEvent, format_sse_event
from app.streaming.completion_stream import CompletionEventQueue
from app.streaming.completion_event_bus import websocket_manager
from app.models.user import User
from app.core.auth import current_user
from fastapi import BackgroundTasks
from sqlalchemy.ext.asyncio import AsyncSession
from app.dependencies import get_async_db
import json
import asyncio
from fastapi import Request
from fastapi.responses import StreamingResponse
import time
from app.core.permissions_decorator import requires_permission
from app.models.organization import Organization
from app.dependencies import get_current_organization
from app.models.report import Report

router = APIRouter(tags=["completions"])

completion_service = CompletionService()

# A report's conversation is owner-only. Every report-scoped route here passes
# `model=Report, owner_only=True` so the decorator resolves the report inside the
# caller's org and enforces ownership: reads (`view_*`) additionally admit full
# admins and project collaborators via the decorator's read-only bypasses, while
# writes stay strictly with the owner. Without `owner_only` the decorator skips
# its whole object gate and any org member holding the role could read or post
# into another user's conversation. Sharing a dashboard grants the artifact
# surface (/r/{id}) only — never the transcript.
@router.post("/api/reports/{report_id}/completions/estimate", response_model=CompletionContextEstimateSchema)
@requires_permission('create_reports', model=Report, owner_only=True)
async def estimate_completion_tokens(
    report_id: str,
    completion: CompletionCreate,
    current_user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_async_db)
):
    return await completion_service.estimate_completion_tokens(
        db,
        report_id,
        completion,
        current_user,
        organization,
    )

@router.post("/api/reports/{report_id}/context/compact")
@requires_permission('create_reports', model=Report, owner_only=True)
async def compact_report_context(
    report_id: str,
    current_user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_async_db)
):
    """On-demand context compaction: fold turns older than the recent window
    into the report's rolling summary. Idle-only — 409 while an agent
    execution is streaming on this report (auto-compaction covers end-of-turn
    pressure). force=True skips the token threshold, not the recent-tail rule."""
    from sqlalchemy import select
    from app.services.context_compaction_service import context_compaction_service, ContextCompactionService

    report_res = await db.execute(select(Report).filter(Report.id == report_id))
    report = report_res.scalar_one_or_none()
    if not report or str(report.organization_id) != str(organization.id):
        raise HTTPException(status_code=404, detail="Report not found")

    if await ContextCompactionService.is_report_busy(db, report_id):
        raise HTTPException(status_code=409, detail="An agent run is in progress on this report; try again when it finishes.")

    small_model = await completion_service.llm_service.get_default_model(db, organization, current_user, is_small=True)
    if not small_model:
        small_model = await completion_service.llm_service.get_default_model(db, organization, current_user)
    if not small_model:
        raise HTTPException(status_code=400, detail="No LLM model configured for this organization.")

    result = await context_compaction_service.compact(
        db, report, organization, small_model, force=True,
    )
    if result.get("status") == "error":
        raise HTTPException(status_code=500, detail=result.get("message", "Compaction failed"))

    # The estimate cache may hold a pre-compaction context figure; drop it so
    # the usage popover refreshes with the compacted state immediately.
    try:
        completion_service._estimate_cache.clear()
    except Exception:
        pass

    result["can_compact"] = (await ContextCompactionService.get_ui_state(db, report_id))["can_compact"]
    return result


@router.post("/api/reports/{report_id}/completions")
@requires_permission('create_reports', model=Report, owner_only=True)
async def create_completion(
    report_id: str,
    completion: CompletionCreate,
    request: Request,
    current_user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_async_db)
):
    """Unified completion endpoint.

    - Streams if: body `stream: true`, or `Accept: text/event-stream`, or `?stream=true`
    - Otherwise returns JSON response
    """
    # Queue mode: persist the prompt as a queued row instead of starting a
    # second concurrent run; the dispatcher starts it when the current run
    # finishes. Never streams.
    if getattr(completion, "queue", False):
        return await completion_service.create_queued_completion(
            db, report_id, completion, current_user, organization
        )

    accept_header = request.headers.get("accept", "")
    body_stream_flag = getattr(completion, "stream", None)
    query_stream_flag = request.query_params.get("stream", "false").lower() == "true"
    wants_stream = (
        (body_stream_flag is True)
        or ("text/event-stream" in accept_header.lower())
        or query_stream_flag
    )
    if wants_stream:
        return await completion_service.create_completion_stream(
            db,
            report_id,
            completion,
            current_user,
            organization,
        )

    # Default to no background execution unless explicitly overridden via `?background=true`
    background = request.query_params.get("background", "false").lower() == "true"
    return await completion_service.create_completion(
        db,
        report_id,
        completion,
        current_user,
        organization,
        background=background,
    )

@router.get("/api/reports/{report_id}/completions/{completion_id}/stream")
@requires_permission('view_reports', model=Report, owner_only=True)
async def watch_completion_stream(
    report_id: str,
    completion_id: str,
    current_user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_async_db)
):
    """Re-attachable SSE stream for an existing completion.

    Lets a client that lost its kickoff stream (page refresh, network drop,
    second tab) resume live progress. Idempotent and side-effect free, so it
    is safe to retry with backoff.
    """
    return await completion_service.watch_completion_stream(
        db, report_id, completion_id, current_user, organization
    )


@router.get("/api/reports/{report_id}/completions.legacy")
@requires_permission('view_reports', model=Report, owner_only=True)
async def get_completions(report_id: str, current_user: User = Depends(current_user), organization: Organization = Depends(get_current_organization), db: AsyncSession = Depends(get_async_db)):
    return await completion_service.get_completions(db, report_id, organization, current_user)

# The old unauthenticated report WebSocket endpoint is gone: clients get live
# updates from the DB-backed activity stream (GET /reports/activity/stream)
# and the per-completion SSE watch stream, both of which are authenticated
# and correct across uvicorn workers.

@requires_permission('manage_settings')
@router.get("/api/completions/{completion_id}/plans")
async def get_completion_plans(completion_id: str, current_user: User = Depends(current_user), organization: Organization = Depends(get_current_organization), db: AsyncSession = Depends(get_async_db)):
    return await completion_service.get_completion_plans(db, current_user, organization, completion_id)


@router.get("/api/reports/{report_id}/completions")
@requires_permission('view_reports', model=Report, owner_only=True)
async def get_completions_v2(
    report_id: str,
    limit: int = 10,
    before: str | None = None,
    current_user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_async_db)
):
    """New UI-focused completions response with ordered blocks and artifacts.

    - limit: last N completions (user+system), default 10
    - before: ISO datetime cursor to fetch items strictly before it
    """
    return await completion_service.get_completions_v2(db, report_id, organization, current_user, limit=limit, before=before)

@requires_permission('create_reports')
@router.post("/api/completions/{completion_id}/sigkill")
async def update_completion_sigkill(completion_id: str, current_user: User = Depends(current_user), organization: Organization = Depends(get_current_organization), db: AsyncSession = Depends(get_async_db)):
    return await completion_service.update_completion_sigkill(db, completion_id, current_user, organization)


@router.delete("/api/completions/{completion_id}/queued")
@requires_permission('create_reports')
async def delete_queued_completion(
    completion_id: str,
    current_user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_async_db),
):
    """Remove a prompt from the report's queue (only while still queued)."""
    return await completion_service.delete_queued_completion(db, completion_id, current_user, organization)


@router.post("/api/completions/{completion_id}/steer")
@requires_permission('create_reports')
async def steer_completion(
    completion_id: str,
    body: dict,
    current_user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_async_db),
):
    """Inject a user message into the running completion (``completion_id`` is
    the in-progress system completion). Body: ``{content}`` to steer with new
    text, or ``{queued_completion_id}`` to promote a queued prompt into the
    live run. Falls back to enqueueing when the run already finished."""
    return await completion_service.steer_completion(db, completion_id, body, current_user, organization)


@requires_permission('create_reports')
@router.post("/api/completions/{completion_id}/tool-results/{tool_call_id}")
async def submit_tool_result(
    completion_id: str,
    tool_call_id: str,
    body: dict,
    current_user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_async_db),
):
    """Accept an Office.js execution result from the Excel taskpane and resolve
    the waiting tool future. Used by the write_officejs_code tool."""
    return await completion_service.submit_tool_result(db, completion_id, tool_call_id, body, current_user, organization)


@requires_permission('create_reports')
@router.post("/api/completions/{completion_id}/tool_executions/{tool_execution_id}/clarify_response")
async def submit_clarify_response(
    completion_id: str,
    tool_execution_id: str,
    body: dict,
    current_user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_async_db),
):
    """Persist the user's selections from the clarify tool form so the UI can
    rehydrate them on reload (and across devices)."""
    return await completion_service.submit_clarify_response(
        db, completion_id, tool_execution_id, body, current_user, organization
    )


@router.post("/api/completions/{completion_id}/mcp_tool_confirmations/{confirmation_id}")
@requires_permission('create_reports')
async def respond_to_mcp_tool_confirmation(
    completion_id: str,
    confirmation_id: str,
    body: dict,
    current_user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_async_db),
):
    """Resolve a pending MCP tool approval ('ask' policy) for a running
    completion. Only the user who started the run may respond. With
    ``remember: true`` the decision is persisted as that user's per-tool
    policy preference so future runs skip the prompt.

    The decision is written to the ``tool_confirmations`` row, which the waiting
    run polls. That is what makes this work at all under multiple uvicorn
    workers: this request rarely lands on the worker streaming the completion,
    and that worker's in-memory future is invisible from here."""
    from app.ai.tools.confirmation import get_confirmation_meta, resolve_confirmation
    from app.models.tool_confirmation import ToolConfirmation
    from app.services.tool_confirmation_service import (
        KIND_MCP_TOOL_POLICY, ToolConfirmationService,
    )
    from app.services.tool_policy_service import (
        ToolPolicyService, TOOL_POLICY_ALLOW, TOOL_POLICY_DENY,
    )

    approved = bool(body.get("approved"))
    remember = bool(body.get("remember"))

    # Kinds answerable via this endpoint: MCP tool policy asks and builtin-tool
    # confirmations (e.g. set_report_agents expanding a manual agent selection).
    # Everything below the kind check is kind-agnostic; the `remember` branch
    # already no-ops when there is no connection_tool_id.
    from app.ai.tools.confirmation import KIND_BUILTIN_TOOL
    _ANSWERABLE_KINDS = {KIND_MCP_TOOL_POLICY, KIND_BUILTIN_TOOL}

    confirmations = ToolConfirmationService()
    row = await confirmations.get(db, confirmation_id)
    if row is not None:
        if row.kind not in _ANSWERABLE_KINDS:
            raise HTTPException(status_code=404, detail="Confirmation not found or expired")
        if not confirmations.may_respond(
            row, completion_id=completion_id, user_id=str(current_user.id)
        ):
            if row.user_id and str(current_user.id) != str(row.user_id):
                raise HTTPException(
                    status_code=403, detail="Only the user who started this run can respond"
                )
            raise HTTPException(
                status_code=404, detail="Confirmation not found for this completion"
            )
        if row.status == ToolConfirmation.STATUS_EXPIRED:
            raise HTTPException(status_code=410, detail="Confirmation expired")
        meta = {"connection_tool_id": row.connection_tool_id}
        if not row.is_pending:
            # Already answered (double click, or a retry after a dropped
            # response): report the decision on record instead of failing.
            return {
                "status": "ok",
                "approved": row.approved,
                "remembered": bool(row.remember),
                "already_resolved": True,
            }
    else:
        # No row: a confirmation that predates this table (in-flight across a
        # deploy) can still be answered on its own worker.
        meta = get_confirmation_meta(confirmation_id)
        if meta is None or meta.get("kind") not in _ANSWERABLE_KINDS:
            raise HTTPException(status_code=404, detail="Confirmation not found or expired")
        if completion_id not in (meta.get("completion_ids") or []):
            raise HTTPException(status_code=404, detail="Confirmation not found for this completion")
        if meta.get("user_id") and str(current_user.id) != str(meta["user_id"]):
            raise HTTPException(status_code=403, detail="Only the user who started this run can respond")

    if remember and meta.get("connection_tool_id"):
        # Brief retry: on SQLite a concurrent agent write can hold the single
        # writer lock for a moment ("database is locked").
        last_err = None
        for attempt in range(3):
            try:
                await ToolPolicyService().set_user_preference(
                    db, str(current_user.id), str(meta["connection_tool_id"]),
                    TOOL_POLICY_ALLOW if approved else TOOL_POLICY_DENY,
                )
                await db.commit()
                last_err = None
                break
            except Exception as e:
                last_err = e
                await db.rollback()
                await asyncio.sleep(0.4 * (attempt + 1))
        if last_err is not None:
            raise HTTPException(status_code=500, detail="Failed to save preference")

    # Durable decision first — the run polls this row, wherever it is running.
    if row is not None:
        row = await confirmations.resolve(
            db,
            confirmation_id=confirmation_id,
            approved=approved,
            remember=remember,
            user_id=str(current_user.id),
        )
        if row is None:
            raise HTTPException(status_code=404, detail="Confirmation not found or expired")
        approved, remember = row.approved, bool(row.remember)

    # Same-worker fast path: wakes the run now instead of on its next poll.
    # A False here just means the run is on another worker, which is the norm.
    resolved_locally = resolve_confirmation(
        confirmation_id,
        {
            "approved": approved,
            "remember": remember,
            "resolved_by_user_id": str(current_user.id),
            "resolved_by_name": getattr(current_user, "name", None) or None,
        },
    )
    if row is None and not resolved_locally:
        raise HTTPException(status_code=404, detail="Confirmation not found or expired")
    return {"status": "ok", "approved": approved, "remembered": remember}


@requires_permission('create_reports')
@router.post("/api/completions/{completion_id}/tool_executions/{tool_execution_id}/cancel_wait")
async def cancel_wait(
    completion_id: str,
    tool_execution_id: str,
    current_user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_async_db),
):
    """Cancel a pending wait: remove its one-shot resume job so the agent never
    wakes, and mark the tool execution cancelled. Idempotent."""
    return await completion_service.cancel_wait(
        db, completion_id, tool_execution_id, current_user, organization
    )