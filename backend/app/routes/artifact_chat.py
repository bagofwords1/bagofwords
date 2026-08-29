"""Chat endpoints for the shared artifact page /r/{id}.

Deliberately OUTSIDE the owner-only /api/reports/{id}/completions surface:
sharing a dashboard never grants the owner's transcript. These routes gate on
the same artifact visibility as the rest of /r/{id}, require a signed-in org
member, and operate on the viewer's own hidden chat report
(report_type='artifact_chat') — see ArtifactChatService.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload, lazyload

from app.core.auth import current_user, current_user_optional
from app.dependencies import get_async_db
from app.models.data_source import DataSource
from app.models.organization import Organization
from app.models.report import Report
from app.models.user import User
from app.schemas.completion_v2_schema import CompletionCreate
from app.services.artifact_chat_service import artifact_chat_service
from app.services.completion_service import CompletionService

logger = logging.getLogger(__name__)

router = APIRouter(tags=["artifact-chat"])

completion_service = CompletionService()


async def _load_source_report(db: AsyncSession, report_id: str) -> Report:
    report = (await db.execute(
        select(Report)
        .options(
            lazyload("*"),
            selectinload(Report.data_sources).options(
                lazyload("*"),
                selectinload(DataSource.connections).options(lazyload("*")),
            ),
        )
        .where(
            Report.id == report_id,
            Report.report_type == 'regular',
            Report.deleted_at.is_(None),
        )
    )).unique().scalar_one_or_none()
    if not report:
        raise HTTPException(status_code=404, detail="Report not found")
    return report


async def _load_org(db: AsyncSession, report: Report) -> Organization:
    org = await db.get(Organization, str(report.organization_id))
    if not org:
        raise HTTPException(status_code=404, detail="Report not found")
    return org


@router.get("/r/{report_id}/chat")
async def get_chat_status(
    report_id: str,
    db: AsyncSession = Depends(get_async_db),
    user: User | None = Depends(current_user_optional),
):
    """Availability + scope for the chat bubble. Never creates anything.

    Anonymous callers on a visible report get {available: false,
    reason: 'auth_required'} instead of a 401 so the page can render a
    sign-in prompt inside the bubble.
    """
    report = await _load_source_report(db, report_id)
    org = await _load_org(db, report)

    try:
        await artifact_chat_service.ensure_chat_access(db, report, user)
    except HTTPException as e:
        # Visibility check runs first inside ensure_chat_access, so a 401
        # here means "sign in and try again", never "hidden report" (those
        # 404). A 403 for a signed-in caller is either the toggle being off
        # or a non-member — both render as an unavailable bubble.
        if user is None and e.status_code == 401:
            return {"enabled": True, "available": False, "reason": "auth_required"}
        if e.status_code == 403 and user is not None:
            reason = "disabled" if "not enabled" in str(e.detail) else "not_member"
            return {"enabled": reason != "disabled", "available": False, "reason": reason}
        raise

    agent_ids = await artifact_chat_service.effective_agent_ids(db, report, org, user)
    agents_by_id = {str(ds.id): ds for ds in (report.data_sources or [])}
    agent_names = []
    for aid in agent_ids:
        ds = agents_by_id.get(aid)
        if ds is None:
            ds = await db.get(DataSource, aid)
        if ds is not None:
            agent_names.append({"id": aid, "name": ds.name})

    chat_report = (await db.execute(
        select(Report.id).where(
            Report.report_type == 'artifact_chat',
            Report.forked_from_id == str(report.id),
            Report.user_id == str(user.id),
            Report.deleted_at.is_(None),
        ).limit(1)
    )).scalar_one_or_none()

    return {
        "enabled": True,
        "available": True,
        "reason": None,
        "scope": "agents" if agent_ids else "data_only",
        "agents": agent_names,
        "chat_report_id": str(chat_report) if chat_report else None,
    }


@router.post("/r/{report_id}/chat/completions")
async def create_chat_completion(
    report_id: str,
    completion: CompletionCreate,
    db: AsyncSession = Depends(get_async_db),
    user: User = Depends(current_user),
):
    """Send a message into the viewer's own chat thread. Always streams SSE.

    Access, scope and roster are re-resolved on every message, so unsharing,
    disabling chat, or narrowing the agent allowlist applies immediately.
    """
    report = await _load_source_report(db, report_id)
    org = await _load_org(db, report)
    await artifact_chat_service.ensure_chat_access(db, report, user)

    agent_ids = await artifact_chat_service.effective_agent_ids(db, report, org, user)
    chat_report = await artifact_chat_service.resolve_chat_report(db, report, user, agent_ids)

    # Server-side prompt hardening: viewer chat is plain text into the chat
    # report — platform identity and context are ours, and widget/step
    # targeting or queueing make no sense here.
    if completion.prompt is None or not (completion.prompt.content or "").strip():
        raise HTTPException(status_code=400, detail="Message content is required")
    completion.prompt.widget_id = None
    completion.prompt.step_id = None
    completion.prompt.mode = 'chat'
    completion.prompt.platform = 'artifact_chat'
    completion.prompt.platform_context = await artifact_chat_service.build_platform_context(
        db, report, agent_ids
    )
    try:
        completion.queue = False
    except Exception:
        pass

    return await completion_service.create_completion_stream(
        db, str(chat_report.id), completion, user, org,
    )


@router.get("/r/{report_id}/chat/completions")
async def get_chat_completions(
    report_id: str,
    limit: int = 20,
    before: str | None = None,
    db: AsyncSession = Depends(get_async_db),
    user: User = Depends(current_user),
):
    """The viewer's own thread (v2 blocks). Empty before the first message."""
    report = await _load_source_report(db, report_id)
    org = await _load_org(db, report)
    await artifact_chat_service.ensure_chat_access(db, report, user)

    chat_report_id = (await db.execute(
        select(Report.id).where(
            Report.report_type == 'artifact_chat',
            Report.forked_from_id == str(report.id),
            Report.user_id == str(user.id),
            Report.deleted_at.is_(None),
        ).limit(1)
    )).scalar_one_or_none()
    if not chat_report_id:
        return {"report_id": None, "completions": [], "total_completions": 0}

    return await completion_service.get_completions_v2(
        db, str(chat_report_id), org, user, limit=limit, before=before
    )


@router.get("/r/{report_id}/chat/completions/{completion_id}/stream")
async def watch_chat_completion(
    report_id: str,
    completion_id: str,
    db: AsyncSession = Depends(get_async_db),
    user: User = Depends(current_user),
):
    """Re-attachable SSE watch for a completion in the viewer's own thread."""
    report = await _load_source_report(db, report_id)
    org = await _load_org(db, report)
    await artifact_chat_service.ensure_chat_access(db, report, user)

    chat_report_id = (await db.execute(
        select(Report.id).where(
            Report.report_type == 'artifact_chat',
            Report.forked_from_id == str(report.id),
            Report.user_id == str(user.id),
            Report.deleted_at.is_(None),
        ).limit(1)
    )).scalar_one_or_none()
    if not chat_report_id:
        raise HTTPException(status_code=404, detail="No chat thread")

    return await completion_service.watch_completion_stream(
        db, str(chat_report_id), completion_id, user, org
    )
