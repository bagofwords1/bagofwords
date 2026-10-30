"""Where an embed session counts as the viewer.

An embed session (``Authorization: Bearer bow_embed_...``, issued by
HostApplicationService) stands for a real BOW user, but only inside the
artifact that allowed its host application. ``viewer_optional`` replaces
``current_user_optional`` on exactly the routes an embedded artifact needs:

* the published dashboard routes ``/api/d/{ref}/...`` (not the draft, not fork),
* the artifact runtime ``/api/artifacts/{artifact_id}/runtime/...``.

On those routes an embed session resolves to its user when, and only when, the
artifact in the path allows the session's host application; anything else is
refused (401 for a dead session, 403 for an artifact the host may not show).
The user then still goes through the artifact's own sharing checks, so
embedding never widens who may see it. Every other route in the API keeps
rejecting embed sessions, because ``current_user`` does not know them.
"""
from __future__ import annotations

from typing import Optional

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import current_user_optional
from app.dependencies import get_async_db
from app.errors import AppError, ErrorCode
from app.models.user import User
from app.services.host_application_service import SESSION_PREFIX, host_application_service


def embed_token(request: Request) -> Optional[str]:
    header = request.headers.get("Authorization", "")
    if header.startswith("Bearer " + SESSION_PREFIX):
        return header[7:].strip()
    return None


async def _artifact_id(db: AsyncSession, request: Request) -> Optional[str]:
    params = request.path_params
    if params.get("artifact_id"):
        return str(params["artifact_id"])
    if params.get("ref"):
        from app.services.dashboard_access import resolve

        return str((await resolve(db, str(params["ref"]))).id)
    return None


async def viewer_optional(
    request: Request,
    db: AsyncSession = Depends(get_async_db),
    user: Optional[User] = Depends(current_user_optional),
) -> Optional[User]:
    token = embed_token(request)
    if token is None:
        return user
    ctx = await host_application_service.resolve_session(db, token)
    if ctx is None:
        raise AppError.unauthorized(ErrorCode.EMBED_SESSION_INVALID, "The embedded session is invalid or expired")
    artifact_id = await _artifact_id(db, request)
    if artifact_id is None or not await host_application_service.allows(db, artifact_id, ctx.host.id):
        raise AppError.forbidden(ErrorCode.EMBED_NOT_ALLOWED, "This artifact is not embeddable in this application")
    request.state.embed = ctx
    from app.core.request_origin import HOST_APP, set_origin

    set_origin(HOST_APP, str(ctx.host.id))
    return ctx.user
