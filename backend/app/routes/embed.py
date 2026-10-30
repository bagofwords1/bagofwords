"""The surface a customer application talks to when it embeds an artifact.

* ``GET /api/embed/sdk/v1/bow-embed.js`` — the loader the host page includes.
* ``GET /api/embed/{host}/bootstrap`` — public: name, mode, origins. The
  origins are in the frame's CSP header anyway, so nothing here is private.
* ``GET /api/embed/{host}/frame?artifact=<ref>`` — the frame-ancestors the
  embedded ``/d/{ref}?host=`` document must send (the Nuxt server asks this;
  ``frame_router`` answers it itself when this process serves the SPA).
* ``POST /api/embed/{host}/session`` — one credential matching the host's
  identity mode (signed assertion, IdP token, or SSO code + PKCE verifier)
  becomes a short-lived ``bow_embed_`` session for one BOW user. ``GET``
  reports it, ``DELETE`` revokes it server-side.
* ``POST /api/embed/{host}/sso/code`` — for SSO mode: the signed-in BOW user
  (normal app session, in the sign-in popup) gets a one-time code bound to the
  embedded page's PKCE challenge.

Embed sessions are accepted only by ``viewer_optional`` (app/core/embed_auth.py).
"""
from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import current_user
from app.core.embed_auth import embed_token
from app.dependencies import get_async_db
from app.errors import AppError, ErrorCode
from app.models.host_application import HostApplication
from app.models.user import User
from app.routes.host_applications import audit
from app.schemas.host_application_schema import (
    EmbedBootstrapSchema,
    EmbedSessionRequest,
    EmbedSessionSchema,
    SsoCodeRequest,
)
from app.services.artifact_admission import admit
from app.services.host_application_service import host_application_service as service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/embed", tags=["embed"])

SDK_FILE = Path(__file__).resolve().parent.parent / "static" / "embed" / "bow-embed.js"
SESSION_LIMIT_PER_CLIENT = 30   # sign-ins per minute from one address, per host
SESSION_LIMIT_PER_HOST = 600    # sign-ins per minute per host


def _client(request: Request) -> str:
    return request.client.host if request.client else "unknown"


async def _host(db: AsyncSession, public_id: str) -> HostApplication:
    host = await service.by_public_id(db, public_id)
    if host is None:
        raise AppError.not_found(ErrorCode.HOST_APP_NOT_FOUND, "Host application not found")
    return host


async def _session_schema(db: AsyncSession, host: HostApplication, user: User, token: str, expires_at):
    from app.models.organization import Organization
    from app.routes.user_profile import viewer_context

    organization = await db.get(Organization, str(host.organization_id))
    viewer = await viewer_context(db, user, organization)
    return EmbedSessionSchema(token=token, expires_at=expires_at,
                              user={"id": str(user.id), "name": user.name, "email": user.email},
                              host_application=host.public_id, organization_id=str(host.organization_id),
                              viewer=viewer.model_dump())


# ── public: SDK, bootstrap, frame policy ──────────────────────────────────

def _sdk_etag() -> str:
    try:
        return '"' + hashlib.sha256(SDK_FILE.read_bytes()).hexdigest()[:24] + '"'
    except OSError:
        return '"missing"'


@router.get("/sdk/v1/bow-embed.js")
async def embed_sdk(request: Request):
    """Static and versioned by path, served by this BOW (no CDN). Revalidates
    on every load so an upgraded BOW ships the matching SDK."""
    if not SDK_FILE.is_file():
        raise HTTPException(status_code=404)
    etag = _sdk_etag()
    headers = {"Cache-Control": "no-cache", "ETag": etag, "Access-Control-Allow-Origin": "*",
               "Cross-Origin-Resource-Policy": "cross-origin"}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    return FileResponse(str(SDK_FILE), media_type="application/javascript; charset=utf-8", headers=headers)


@router.get("/{public_id}/bootstrap", response_model=EmbedBootstrapSchema)
async def embed_bootstrap(public_id: str, db: AsyncSession = Depends(get_async_db)):
    host = await _host(db, public_id)
    return EmbedBootstrapSchema(host_application=host.public_id, name=host.name, enabled=bool(host.enabled),
                                allowed_origins=list(host.allowed_origins or []),
                                identity_mode=host.identity_mode)


async def frame_ancestors(db: AsyncSession, ref: Optional[str], public_id: Optional[str]) -> str:
    """The frame-ancestors source list for ``/d/{ref}?host={public_id}``:
    this origin plus the host's exact origins when the artifact allows that
    host, else this origin only."""
    if ref and public_id:
        from app.services.dashboard_access import resolve

        try:
            artifact = await resolve(db, ref)
        except Exception:
            artifact = None
        if artifact is not None:
            origins = await service.frame_origins(db, str(artifact.id), public_id)
            if origins:
                return " ".join(["'self'", *origins])
    return "'self'"


@router.get("/{public_id}/frame")
async def embed_frame_policy(public_id: str, artifact: str, db: AsyncSession = Depends(get_async_db)):
    return {"frame_ancestors": await frame_ancestors(db, artifact, public_id)}


# ── sessions ──────────────────────────────────────────────────────────────

@router.post("/{public_id}/session", response_model=EmbedSessionSchema)
async def create_embed_session(public_id: str, body: EmbedSessionRequest, request: Request,
                               db: AsyncSession = Depends(get_async_db)):
    """One credential becomes a session. Refusals are typed and audited;
    the credential itself is never echoed or logged."""
    host = await _host(db, public_id)
    # Read before signing in: a refusal rolls the session back and expires the row.
    host_id, organization_id = str(host.id), str(host.organization_id)
    await admit(db, ["embed-session", host_id, _client(request)], SESSION_LIMIT_PER_CLIENT)
    await admit(db, ["embed-session", host_id], SESSION_LIMIT_PER_HOST)
    try:
        user = await service.sign_in(db, host, assertion=body.assertion, id_token=body.id_token,
                                     sso_code=body.sso_code, code_verifier=body.code_verifier)
    except AppError as e:
        await db.rollback()
        await audit(db, organization_id, "embed.sign_in_rejected", resource_id=host_id, request=request,
                    details={"error_code": e.error_code, "reason": (e.params or {}).get("reason")})
        raise
    token, record = await service.issue_session(db, host, user)
    await audit(db, host.organization_id, "embed.session_issued", user_id=str(user.id), resource_id=str(host.id),
                request=request, details={"identity_mode": host.identity_mode})
    return await _session_schema(db, host, user, token, record.expires_at)


async def _own_session(db: AsyncSession, request: Request, public_id: str):
    ctx = await service.resolve_session(db, embed_token(request) or "")
    if ctx is None or ctx.host.public_id != public_id:
        raise AppError.unauthorized(ErrorCode.EMBED_SESSION_INVALID, "The embedded session is invalid or expired")
    return ctx


@router.get("/{public_id}/session", response_model=EmbedSessionSchema)
async def get_embed_session(public_id: str, request: Request, db: AsyncSession = Depends(get_async_db)):
    """Whether the session is still live; the token is not re-issued."""
    ctx = await _own_session(db, request, public_id)
    return await _session_schema(db, ctx.host, ctx.user, "", ctx.token_record.expires_at)


@router.delete("/{public_id}/session", status_code=204)
async def delete_embed_session(public_id: str, request: Request, db: AsyncSession = Depends(get_async_db)):
    ctx = await _own_session(db, request, public_id)
    await service.revoke_session(db, ctx.token_record)
    await audit(db, ctx.host.organization_id, "embed.session_revoked", user_id=str(ctx.user.id),
                resource_id=str(ctx.host.id), request=request)
    return Response(status_code=204)


@router.post("/{public_id}/sso/code")
async def create_sso_code(public_id: str, body: SsoCodeRequest, request: Request,
                          user: User = Depends(current_user), db: AsyncSession = Depends(get_async_db)):
    """Called from BOW's own sign-in popup with the app session; the popup
    hands the code to its opener (exact origin) and closes."""
    host = await _host(db, public_id)
    await admit(db, ["embed-sso", host.id, str(user.id)], SESSION_LIMIT_PER_CLIENT)
    code = await service.issue_sso_code(db, host, user, body.code_challenge)
    return {"code": code, "allowed_origins": list(host.allowed_origins or [])}


# ── /d/{ref}?host=… when this process also serves the SPA ─────────────────

frame_router = APIRouter(tags=["embed"])


@frame_router.get("/d/{ref}")
async def embedded_dashboard_document(ref: str, request: Request, db: AsyncSession = Depends(get_async_db)):
    """The SPA index for a published artifact, with its frame policy. Mounted
    only when SERVE_FRONTEND is on; frontend/server/middleware/embed-frame.ts
    does the same for the Nuxt server."""
    index_file = Path(os.environ.get("FRONTEND_DIST_DIR", "/app/frontend/dist")).resolve() / "index.html"
    if not index_file.is_file():
        raise HTTPException(status_code=404)
    headers = {"Cache-Control": "no-cache"}
    host = request.query_params.get("host")
    if host:
        policy = await frame_ancestors(db, ref, host)
        headers["Content-Security-Policy"] = f"frame-ancestors {policy}; object-src 'none'; base-uri 'self'"
    return FileResponse(str(index_file), headers=headers)
