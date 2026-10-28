"""Embedded chat: the surface the BOW-served SDK and iframe talk to.

Everything here is reachable from a customer's application, so the rules are
narrower than the main app:

* ``/api/embed/sdk/v1/bow-embed.js`` and ``/api/embed/{id}/bootstrap`` are
  public and carry appearance only — no agents, members or policy.
* ``/api/embed/{id}/session`` exchanges a trusted host assertion for an embed
  session (EmbedService). The preview variant lets a signed-in admin open the
  chat as THEMSELVES from the settings page; it never impersonates anyone.
* Every other route takes an embed session (``Authorization: Bearer
  bow_embed_…``) via ``current_embed_session`` and checks that the session's
  embed is the one in the path, so a token from one embed is useless on
  another. Embed sessions are not accepted anywhere else in the API.
* Chat routes operate only on the caller's own hidden thread
  (report_type='embed_chat', embed_id, user_id); the embed's runtime policy is
  re-applied on every message, so saved changes and revocations take effect
  on the next action, not on redeploy.
"""
from __future__ import annotations

import hashlib
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import current_user
from app.dependencies import get_async_db, get_current_organization
from app.core.permissions_decorator import requires_permission
from app.errors import AppError, ErrorCode
from app.models.completion import Completion
from app.models.organization import Organization
from app.models.user import User
from app.schemas.completion_v2_schema import CompletionCreate, PromptSchema
from app.schemas.embed_schema import (
    EmbedBootstrapSchema,
    EmbedChatCreate,
    EmbedSessionRequest,
    EmbedSessionSchema,
)
from app.services.completion_service import CompletionService
from app.services.embed_service import (
    PLATFORM,
    EmbedSessionContext,
    bounded_context,
    embed_service,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["embed"])
completion_service = CompletionService()

SDK_DIR = Path(__file__).resolve().parent.parent / "static" / "embed"
SDK_FILE = SDK_DIR / "bow-embed.js"
# Replays of a send with the same idempotency key inside this window attach
# to the original run instead of starting a second one.
IDEMPOTENCY_WINDOW = timedelta(minutes=10)


# ── dependencies ──────────────────────────────────────────────────────────

async def current_embed_session(
    request: Request,
    db: AsyncSession = Depends(get_async_db),
) -> EmbedSessionContext:
    header = request.headers.get("Authorization", "")
    token = header[7:].strip() if header.startswith("Bearer ") else ""
    ctx = await embed_service.resolve_session(db, token)
    if ctx is None:
        raise AppError.unauthorized(ErrorCode.EMBED_SESSION_INVALID, "Embed session is invalid or expired")
    return ctx


async def _load_embed_or_404(db: AsyncSession, public_id: str):
    embed = await embed_service.get_by_public_id(db, public_id)
    if embed is None:
        raise AppError.not_found(ErrorCode.EMBED_NOT_FOUND, "Embed not found")
    return embed


def _bind(ctx: EmbedSessionContext, public_id: str) -> None:
    """A session is good for exactly one embed: the one it was issued for."""
    if ctx.embed.public_id != public_id:
        raise AppError.unauthorized(ErrorCode.EMBED_SESSION_INVALID, "Embed session does not belong to this embed")


async def _audit(db, embed, action, *, user_id=None, details=None, request=None):
    try:
        from app.ee.audit.service import audit_service
        await audit_service.log(
            db=db, organization_id=str(embed.organization_id), action=action, user_id=user_id,
            resource_type="embed", resource_id=str(embed.id), details=details or {}, request=request,
        )
    except Exception:
        logger.debug("embed audit log failed", exc_info=True)


# ── public: SDK + bootstrap ───────────────────────────────────────────────

def _sdk_etag() -> str:
    try:
        return '"' + hashlib.sha256(SDK_FILE.read_bytes()).hexdigest()[:24] + '"'
    except OSError:
        return '"missing"'


@router.get("/embed/sdk/v1/bow-embed.js", include_in_schema=False)
async def embed_sdk(request: Request):
    """The loader a customer page includes. Static, versioned by path, served
    by this BOW instance (no CDN). Revalidates on every load so an upgraded
    BOW ships the matching SDK without a customer redeploy."""
    if not SDK_FILE.is_file():
        raise HTTPException(status_code=404, detail="SDK not bundled")
    etag = _sdk_etag()
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag, "Cache-Control": "no-cache"})
    return FileResponse(
        str(SDK_FILE),
        media_type="application/javascript; charset=utf-8",
        headers={"Cache-Control": "no-cache", "ETag": etag, "Access-Control-Allow-Origin": "*",
                 "X-Content-Type-Options": "nosniff"},
    )


@router.get("/embed/{public_id}/bootstrap", response_model=EmbedBootstrapSchema)
async def embed_bootstrap(public_id: str, db: AsyncSession = Depends(get_async_db)):
    """Appearance for the iframe's first paint and the origins it may accept a
    handshake from. The origins also appear in the frame's CSP header, so
    nothing here is private."""
    embed = await _load_embed_or_404(db, public_id)
    return EmbedBootstrapSchema(
        embed_id=embed.public_id,
        name=embed.name,
        enabled=bool(embed.enabled),
        allowed_origins=list(embed.allowed_origins or []),
        branding=embed_service.public_branding(embed),
    )


@router.get("/embed/{public_id}/logo", include_in_schema=False)
async def embed_logo(public_id: str, db: AsyncSession = Depends(get_async_db)):
    embed = await _load_embed_or_404(db, public_id)
    path = embed_service.logo_path(embed)
    if not path:
        raise HTTPException(status_code=404, detail="No logo")
    return FileResponse(path, media_type="image/png", headers={"Cache-Control": "public, max-age=3600"})


# ── sessions ──────────────────────────────────────────────────────────────

async def _session_payload(db, embed, user, token, record) -> EmbedSessionSchema:
    from app.models.data_source import DataSource

    organization = await db.get(Organization, str(embed.organization_id))
    agent_ids = await embed_service.effective_agent_ids(db, embed, organization, user)
    agents = []
    if agent_ids:
        rows = (await db.execute(select(DataSource).where(DataSource.id.in_(agent_ids)))).scalars().all()
        by_id = {str(ds.id): ds for ds in rows}
        agents = [{"id": aid, "name": by_id[aid].name} for aid in agent_ids if aid in by_id]
    return EmbedSessionSchema(
        token=token,
        expires_at=record.expires_at,
        user={"name": user.name, "email": user.email},
        agents=agents,
        allow_agent_selection=bool((embed.runtime or {}).get("allow_agent_selection")),
        embed_id=embed.public_id,
    )


@router.post("/embed/{public_id}/session", response_model=EmbedSessionSchema)
async def create_embed_session(
    public_id: str,
    body: EmbedSessionRequest,
    request: Request,
    db: AsyncSession = Depends(get_async_db),
):
    """Trusted host assertion → embed session. One-time: the assertion's jti
    is consumed. Failures are typed (never echo the assertion)."""
    embed = await _load_embed_or_404(db, public_id)
    try:
        user, _membership = await embed_service.authenticate_assertion(db, embed, body.assertion)
    except AppError as e:
        await _audit(db, embed, "embed.auth_rejected", details={"error_code": e.error_code, **(e.params or {})},
                     request=request)
        raise
    token, record = await embed_service.issue_session(db, embed, user)
    await _audit(db, embed, "embed.session_issued", user_id=str(user.id), request=request,
                 details={"expires_at": record.expires_at.isoformat()})
    return await _session_payload(db, embed, user, token, record)


@router.post("/embed/{public_id}/session/preview", response_model=EmbedSessionSchema)
@requires_permission("manage_settings")
async def create_embed_preview_session(
    public_id: str,
    request: Request,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    """The settings-page preview: the signed-in admin opens the chat as
    themselves. Requires a normal BOW session plus manage_settings and works
    on disabled embeds too (so an admin can look before switching it on)."""
    embed = await _load_embed_or_404(db, public_id)
    if str(embed.organization_id) != str(organization.id):
        raise AppError.not_found(ErrorCode.EMBED_NOT_FOUND, "Embed not found")
    token, record = await embed_service.issue_session(db, embed, current_user)
    await _audit(db, embed, "embed.preview_session_issued", user_id=str(current_user.id), request=request)
    return await _session_payload(db, embed, current_user, token, record)


@router.get("/embed/{public_id}/session", response_model=EmbedSessionSchema)
async def get_embed_session(public_id: str, ctx: EmbedSessionContext = Depends(current_embed_session),
                            db: AsyncSession = Depends(get_async_db)):
    """Session status + current capabilities (re-resolved live; the token is
    not re-issued and is returned blank)."""
    _bind(ctx, public_id)
    payload = await _session_payload(db, ctx.embed, ctx.user, "", ctx.token_record)
    return payload


@router.delete("/embed/{public_id}/session", status_code=204)
async def delete_embed_session(public_id: str, request: Request,
                               ctx: EmbedSessionContext = Depends(current_embed_session),
                               db: AsyncSession = Depends(get_async_db)):
    """Logout: the session is revoked server-side, not merely forgotten."""
    _bind(ctx, public_id)
    await embed_service.revoke_session(db, ctx.token_record)
    await _audit(db, ctx.embed, "embed.session_revoked", user_id=str(ctx.user.id), request=request)
    return Response(status_code=204)


# ── the employee's thread ─────────────────────────────────────────────────

@router.get("/embed/{public_id}/chat/completions")
async def get_embed_chat(
    public_id: str,
    limit: int = 30,
    before: Optional[str] = None,
    ctx: EmbedSessionContext = Depends(current_embed_session),
    db: AsyncSession = Depends(get_async_db),
):
    _bind(ctx, public_id)
    thread = await embed_service.find_thread(db, ctx.embed, ctx.user)
    if thread is None:
        return {"report_id": None, "completions": [], "total_completions": 0}
    limit = max(1, min(int(limit), 100))
    return await completion_service.get_completions_v2(db, str(thread.id), ctx.organization, ctx.user,
                                                       limit=limit, before=before)


async def _replay_for_key(db, thread, key: str):
    """The system completion of a recent turn sent with this key, if any."""
    if not key:
        return None
    since = datetime.utcnow() - IDEMPOTENCY_WINDOW
    rows = (await db.execute(
        select(Completion).where(
            Completion.report_id == str(thread.id),
            Completion.role == "user",
            Completion.created_at >= since,
        ).order_by(Completion.created_at.desc()).limit(25)
    )).scalars().all()
    for row in rows:
        prompt = row.prompt if isinstance(row.prompt, dict) else {}
        stored = ((prompt.get("platform_context") or {}).get("embed") or {}).get("idempotency_key")
        if stored == key:
            system = (await db.execute(
                select(Completion).where(Completion.parent_id == str(row.id)).limit(1)
            )).scalar_one_or_none()
            return system
    return None


@router.post("/embed/{public_id}/chat/completions")
async def create_embed_chat_completion(
    public_id: str,
    body: EmbedChatCreate,
    ctx: EmbedSessionContext = Depends(current_embed_session),
    db: AsyncSession = Depends(get_async_db),
):
    """Send a message. Always streams SSE (same events as the report page).

    The browser supplies text, host context and request metadata only. Model,
    effort, tools, follow-ups and roster come from the embed's saved runtime,
    re-applied here, and the user's own permissions are intersected live."""
    _bind(ctx, public_id)
    content = (body.content or "").strip()
    if not content:
        raise AppError.bad_request(ErrorCode.VALIDATION, "Message content is required")

    agent_ids = await embed_service.effective_agent_ids(db, ctx.embed, ctx.organization, ctx.user,
                                                        requested=body.data_source_ids)
    thread = await embed_service.resolve_thread(db, ctx.embed, ctx.user, agent_ids)

    replay = await _replay_for_key(db, thread, body.idempotency_key or "")
    if replay is not None:
        return await completion_service.watch_completion_stream(db, str(thread.id), str(replay.id),
                                                                ctx.user, ctx.organization)

    host_context = bounded_context(body.context)
    try:
        prompt = PromptSchema(
            content=content,
            mode="chat",
            platform=PLATFORM,
            platform_context={
                "embed": {"name": ctx.embed.name,
                          "display_name": (ctx.embed.branding or {}).get("display_name") or ctx.embed.name,
                          # Bookkeeping for retries (never rendered into the prompt;
                          # see PromptBuilder._format_embed_context). Not metadata:
                          # metadata is forwarded to MCP connections.
                          "idempotency_key": body.idempotency_key or None},
                "host": host_context or {},
            },
            metadata=body.metadata,
        )
    except ValidationError as e:
        raise AppError.bad_request(ErrorCode.VALIDATION, str(e.errors()[0].get("msg", "invalid request")))
    completion = CompletionCreate(prompt=prompt, stream=True, queue=False, queue_if_busy=False)
    return await completion_service.create_completion_stream(db, str(thread.id), completion, ctx.user, ctx.organization)


async def _own_completion(db, ctx: EmbedSessionContext, completion_id: str) -> Completion:
    """A completion inside the caller's own thread for this embed, else 404."""
    thread = await embed_service.find_thread(db, ctx.embed, ctx.user)
    completion = None
    if thread is not None:
        completion = (await db.execute(
            select(Completion).where(Completion.id == str(completion_id), Completion.report_id == str(thread.id))
        )).scalar_one_or_none()
    if completion is None:
        raise HTTPException(status_code=404, detail="Completion not found")
    return completion


@router.get("/embed/{public_id}/chat/completions/{completion_id}/stream")
async def watch_embed_chat_completion(
    public_id: str,
    completion_id: str,
    ctx: EmbedSessionContext = Depends(current_embed_session),
    db: AsyncSession = Depends(get_async_db),
):
    """Re-attach to a running (or finished) turn after a reload or drop."""
    _bind(ctx, public_id)
    completion = await _own_completion(db, ctx, completion_id)
    return await completion_service.watch_completion_stream(db, str(completion.report_id), str(completion.id),
                                                            ctx.user, ctx.organization)


@router.post("/embed/{public_id}/chat/completions/{completion_id}/cancel")
async def cancel_embed_chat_completion(
    public_id: str,
    completion_id: str,
    ctx: EmbedSessionContext = Depends(current_embed_session),
    db: AsyncSession = Depends(get_async_db),
):
    _bind(ctx, public_id)
    completion = await _own_completion(db, ctx, completion_id)
    return await completion_service.update_completion_sigkill(db, str(completion.id), ctx.user, ctx.organization)


@router.post("/embed/{public_id}/chat/completions/{completion_id}/mcp_tool_confirmations/{confirmation_id}")
async def respond_embed_tool_confirmation(
    public_id: str,
    completion_id: str,
    confirmation_id: str,
    body: dict,
    ctx: EmbedSessionContext = Depends(current_embed_session),
    db: AsyncSession = Depends(get_async_db),
):
    """Approve or deny a pending tool approval ('ask' policy) on the caller's
    own run. Same resolution path as the report page; the thread check here is
    the embed-specific part."""
    from app.routes.completion import respond_to_mcp_tool_confirmation

    _bind(ctx, public_id)
    completion = await _own_completion(db, ctx, completion_id)
    safe_body = {"approved": bool(body.get("approved")), "remember": False}
    return await respond_to_mcp_tool_confirmation(
        str(completion.id), confirmation_id, safe_body,
        current_user=ctx.user, organization=ctx.organization, db=db,
    )


# ── the chat document, when this process also serves the SPA ─────────────

frame_router = APIRouter(include_in_schema=False)


def frame_csp(embed) -> str:
    """frame-ancestors for the chat document: this origin (settings preview)
    plus the embed's exact allowed origins. Nothing else may frame it."""
    origins = " ".join(embed.allowed_origins or [])
    return f"frame-ancestors 'self' {origins}".strip() + "; object-src 'none'; base-uri 'self'"


@frame_router.get("/embed/{public_id}")
async def embed_frame_document(public_id: str, db: AsyncSession = Depends(get_async_db)):
    """``/embed/{id}``: the SPA index with the embed's own frame policy. Only
    mounted when SERVE_FRONTEND is on (the Nuxt server does the same in
    frontend/server/middleware/embed-frame.ts otherwise)."""
    dist_dir = Path(os.environ.get("FRONTEND_DIST_DIR", "/app/frontend/dist")).resolve()
    index_file = dist_dir / "index.html"
    if not index_file.is_file():
        raise HTTPException(status_code=404)
    embed = await embed_service.get_by_public_id(db, public_id)
    if embed is None:
        # Unknown embed: never frameable, still a normal SPA 404 page.
        csp = "frame-ancestors 'none'"
    else:
        csp = frame_csp(embed)
    return FileResponse(str(index_file), headers={"Cache-Control": "no-cache", "Content-Security-Policy": csp})
