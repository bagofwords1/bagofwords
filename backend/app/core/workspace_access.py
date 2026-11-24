"""The workspace gate: what an *app user* may reach.

An app user is an organization member whose roles do not carry
``access_workspace`` (see permissions_registry). They use the published
artifacts shared with them — embedded in a host application, at ``/d/{ref}``,
or from the ``/apps`` launcher — and nothing else in BOW.

A BOW session is the same session for everyone (an embedded artifact's SSO
popup leaves the ordinary 7-day cookie), so the workspace cannot be withheld
in the browser: it is withheld here, in the two dependencies every
authenticated route goes through.

* ``current_user`` calls :func:`gate_user`: an account that is an app user in
  every organization it belongs to is refused outside the allowlist below.
* ``get_current_organization`` calls :func:`gate_organization`: the same, for
  the one organization the request names (an account may be an app user in one
  organization and a workspace member of another).

Deny by default: a new route is closed to app users until its path is added to
``_OPEN``. Service accounts are not members and are never gated (an API key
holds exactly what its role grants); a personal API key or OAuth token is its
owner and is gated like them.
"""
from __future__ import annotations

import re
from typing import Iterable, Optional, Tuple

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import AppError, ErrorCode

_ANY = ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS")
_READ = ("GET", "HEAD", "OPTIONS")

# (path pattern, methods). Paths are matched without the ``/api`` prefix.
_OPEN: Tuple[Tuple[re.Pattern, Iterable[str]], ...] = tuple(
    (re.compile(pattern), methods) for pattern, methods in (
        # Signing in and out, the account itself.
        (r"^/auth/", _ANY),
        (r"^/users/whoami$", _READ),
        (r"^/users/me$", _ANY),
        (r"^/users/me/viewer_context$", _READ),
        (r"^/users/me/avatar$", _ANY),
        (r"^/users/avatar/", _READ),
        (r"^/invites/accept$", _ANY),
        # What the shell needs to render at all.
        (r"^/organizations$", _READ),
        (r"^/organization/locale$", _READ),
        # Feature switches the artifact page reads (code visibility, credit),
        # as every member's does.
        (r"^/organization/settings$", _READ),
        (r"^/settings$", _READ),
        (r"^/config/", _READ),
        (r"^/general/icon/", _READ),
        (r"^/thumbnails/", _READ),
        # The apps themselves: the published artifact, its runtime (resources,
        # conversations), asking its owner for access, and files it embeds.
        # The published artifact only: its draft and forking it are the
        # workspace (dashboard_access.can_edit refuses an app user as well).
        (r"^/d/[^/]+(?:/(?!draft(?:/|$)|fork(?:/|$)).*)?$", _ANY),
        (r"^/artifacts/[^/]+/runtime(/|$)", _ANY),
        (r"^/artifacts/[^/]+/access_requests?$", _ANY),
        (r"^/files/[^/]+/embed$", _READ),
        (r"^/embed/", _ANY),
        # The launcher.
        (r"^/apps(/|$)", _ANY),
    )
)


def is_open(path: str, method: str) -> bool:
    """Whether ``method path`` is reachable without workspace access."""
    if path.startswith("/api/"):
        path = path[4:]
    elif path == "/api":
        path = "/"
    method = (method or "GET").upper()
    return any(pattern.match(path) and method in methods for pattern, methods in _OPEN)


# ── signing up through a host application ──────────────────────────────────
#
# An embedded artifact's sign-in window runs BOW's ordinary sign-in and
# sign-up, which know nothing of the host application. So the window first
# gets an *intent*: a signed, httpOnly cookie naming the host application and
# the moment the window opened (routes/embed.py). ``current_user`` then calls
# :func:`apply_signup_intent` on every request that carries it, BEFORE the
# gate: a membership created since that moment was created by that window,
# and gets the host application's new-user role there and then. The account
# therefore never makes a workspace request as a member, whether or not the
# window ever finishes (closed after sign-up, or left open).
#
# Tampering buys nothing: without the cookie the account is an ordinary
# sign-up, governed by the organization's own sign-up policy; forging one
# needs the signing key and could only lower its bearer's access.

INTENT_COOKIE = "bow_embed_signup"
INTENT_PATH = "/api"
INTENT_LIFETIME = 60 * 60
_INTENT_AUDIENCE = "bow:embed-signup"


def issue_signup_intent(host_public_id: str) -> str:
    import time

    import jwt

    from app.core.auth import SECRET

    now = int(time.time())
    # One second of slack: the membership's created_at is this clock too, but
    # the cookie's timestamp is whole seconds.
    return jwt.encode({"host": host_public_id, "started": now - 1, "aud": _INTENT_AUDIENCE,
                       "exp": now + INTENT_LIFETIME}, SECRET, algorithm="HS256")


def read_signup_intent(request: Request):
    """(host public id, started) of a valid intent cookie, else None."""
    from datetime import datetime

    import jwt

    from app.core.auth import SECRET

    raw = request.cookies.get(INTENT_COOKIE)
    if not raw:
        return None
    try:
        claims = jwt.decode(raw, SECRET, algorithms=["HS256"], audience=_INTENT_AUDIENCE)
        return str(claims["host"]), datetime.utcfromtimestamp(float(claims["started"]))
    except Exception:
        return None


async def apply_signup_intent(request: Request, db: AsyncSession, user) -> None:
    """Give an account created through a host application's sign-in window
    that application's new-user role. Cheap and idempotent: nothing is read
    without the cookie, and a membership is only ever changed once."""
    if not _gated(user) or getattr(request.state, "_signup_intent_done", False):
        return
    request.state._signup_intent_done = True
    intent = read_signup_intent(request)
    if intent is None:
        return
    public_id, started = intent
    try:
        from app.services.host_application_service import host_application_service
        from app.services.host_identity import apply_new_user_role

        host = await host_application_service.by_public_id(db, public_id)
        if host is None or not host.enabled:
            return
        host_id, organization_id, role, user_id = str(host.id), str(host.organization_id), host.new_user_role, str(user.id)
        if await apply_new_user_role(db, host_id, organization_id, role, user_id, since=started):
            from app.routes.host_applications import audit

            await audit(db, organization_id, "embed.user_created", user_id=user_id, resource_id=host_id,
                        request=request, details={"role": role, "identity_mode": host.identity_mode})
    except Exception:
        # Never a reason to fail the request; the next one tries again.
        request.state._signup_intent_done = False
        import logging

        logging.getLogger(__name__).warning("applying a sign-up intent failed", exc_info=True)
        try:
            await db.rollback()
        except Exception:
            pass


async def apply_signup_intent_now(request: Optional[Request], user) -> None:
    """The same, at the moment an account is registered or signs in (the
    registration hook, the OIDC callback, the login hook), with a session of
    its own: the role is stored before the account's session is issued.
    ``current_user`` remains the backstop for any path that does not call
    this."""
    if request is None or user is None or read_signup_intent(request) is None:
        return
    from app.dependencies import async_session_maker

    try:
        async with async_session_maker() as db:
            await apply_signup_intent(request, db, user)
    except Exception:
        import logging

        logging.getLogger(__name__).warning("applying a sign-up intent at sign-in failed", exc_info=True)
    finally:
        # Let current_user look again on a later request of this account.
        request.state._signup_intent_done = False


def _refuse() -> AppError:
    return AppError.forbidden(
        ErrorCode.WORKSPACE_ACCESS_REQUIRED,
        "Your account can use the apps shared with you, not the workspace",
    )


def _gated(user) -> bool:
    return user is not None and not getattr(user, "is_service_account", False)


async def has_workspace_access(db: AsyncSession, user, organization_id: str) -> bool:
    """Whether ``user`` may use the workspace of ``organization_id``. True for
    a non-member too: membership is another check's job, with its own error."""
    if not _gated(user):
        return True
    from app.core.permission_resolver import (
        has_workspace_access as _has, principal_belongs_to_org, resolve_permissions,
    )

    if not await principal_belongs_to_org(db, user, str(organization_id)):
        return True
    resolved = await resolve_permissions(db, str(user.id), str(organization_id))
    return _has(resolved.org_permissions)


async def assert_workspace_access(db: AsyncSession, user, organization_id: str) -> None:
    """For the surfaces that do not go through the HTTP dependencies (MCP,
    chat platforms): refuse an app user."""
    if not await has_workspace_access(db, user, organization_id):
        raise _refuse()


async def gate_organization(request: Request, db: AsyncSession, user, organization_id: str) -> None:
    if is_open(request.url.path, request.method):
        return
    await assert_workspace_access(db, user, organization_id)


async def gate_user(request: Request, db: AsyncSession, user) -> None:
    """Refuse an account that has memberships and the workspace in none of
    them. An account with no membership yet (accepting its first invite,
    creating its organization) passes."""
    if not _gated(user) or is_open(request.url.path, request.method):
        return
    cached: Optional[bool] = getattr(request.state, "_workspace_somewhere", None)
    if cached is None:
        from app.core.permission_resolver import has_workspace_access as _has, resolve_permissions
        from app.models.membership import Membership

        org_ids = (await db.execute(
            select(Membership.organization_id).where(
                Membership.user_id == str(user.id), Membership.deleted_at.is_(None))
        )).scalars().all()
        cached = not org_ids
        for org_id in org_ids:
            if _has((await resolve_permissions(db, str(user.id), str(org_id))).org_permissions):
                cached = True
                break
        request.state._workspace_somewhere = cached
    if not cached:
        raise _refuse()
