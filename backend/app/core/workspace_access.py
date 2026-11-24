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

import contextvars
import re
from datetime import datetime
from typing import Iterable, NamedTuple, Optional, Tuple

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
# sign-up, which know nothing of the host application. So the window gets an
# *intent*: a signed, httpOnly cookie naming the host application, its
# organization, the role it gives new people, and the moment it was issued
# (routes/embed.py; renewed when a sign-up form is submitted and when a
# provider's authorization starts, so it is always fresh at the moment an
# account can be created).
#
# The role is bound to the membership's own write. `SignupIntentMiddleware`
# reads the cookie into a context variable for the request, and a mapper
# event on `Membership` (`_on_membership_write`) stamps the role and the host
# application on a membership as it is inserted, or as a row made in this
# sign-in gets its user: in the same flush, the same transaction. There is no
# second step that could fail or be skipped and leave a workspace membership
# behind, and `ensure_system_role_assignment` gives such a membership the
# `app_user` assignment, never `member`.
#
# An invitation an admin sent earlier keeps the role it grants (its row is
# older than the intent). Tampering buys nothing: without the cookie the
# account is an ordinary sign-up, governed by the organization's own sign-up
# policy; forging one needs the signing key and could only lower its
# bearer's access.

INTENT_COOKIE = "bow_embed_signup"
INTENT_PATH = "/api"
INTENT_LIFETIME = 60 * 60
_INTENT_AUDIENCE = "bow:embed-signup"


class SignupIntent(NamedTuple):
    host_public_id: str
    host_id: str
    organization_id: str
    role: str
    started: datetime


_signup_intent: contextvars.ContextVar[Optional[SignupIntent]] = contextvars.ContextVar(
    "bow_signup_intent", default=None)


def issue_signup_intent(host) -> str:
    """The cookie value for a sign-in window of ``host`` opened now."""
    import time

    import jwt

    from app.core.auth import SECRET
    from app.core.permissions_registry import APP_USER_ROLE

    now = int(time.time())
    role = host.new_user_role if host.new_user_role in (APP_USER_ROLE, "member") else APP_USER_ROLE
    # One second of slack: the membership's created_at is this clock too, but
    # the cookie's timestamp is whole seconds.
    return jwt.encode({"host": host.public_id, "hid": str(host.id), "org": str(host.organization_id), "role": role,
                       "started": now - 1, "aud": _INTENT_AUDIENCE, "exp": now + INTENT_LIFETIME},
                      SECRET, algorithm="HS256")


def decode_signup_intent(raw: Optional[str]) -> Optional[SignupIntent]:
    if not raw:
        return None
    import jwt

    from app.core.auth import SECRET

    try:
        claims = jwt.decode(raw, SECRET, algorithms=["HS256"], audience=_INTENT_AUDIENCE)
        return SignupIntent(str(claims["host"]), str(claims["hid"]), str(claims["org"]), str(claims["role"]),
                            datetime.utcfromtimestamp(float(claims["started"])))
    except Exception:
        return None


def set_signup_intent_cookie(response, host) -> None:
    from app.services.auth_providers import _cookie_secure

    response.set_cookie(INTENT_COOKIE, issue_signup_intent(host), max_age=INTENT_LIFETIME, httponly=True,
                        secure=_cookie_secure(), samesite="lax", path=INTENT_PATH)


async def bind_signup_intent(request: Request, response) -> None:
    """A provider's authorization is starting (the sign-in page's provider
    button). If it starts inside a host application's sign-in window — the
    page says so with ``?embed_host=``, or the browser still carries an
    intent — issue the intent afresh on this response, so it is valid for the
    whole round trip to the provider however long the page sat open."""
    public_id = request.query_params.get("embed_host")
    if not public_id:
        current = decode_signup_intent(request.cookies.get(INTENT_COOKIE))
        public_id = current.host_public_id if current else None
    if not public_id or not re.fullmatch(r"host_[a-f0-9]{8,32}", public_id):
        return
    from app.dependencies import async_session_maker
    from app.services.host_application_service import host_application_service

    async with async_session_maker() as db:
        host = await host_application_service.by_public_id(db, public_id)
        if host is not None and host.enabled:
            set_signup_intent_cookie(response, host)


class SignupIntentMiddleware:
    """Makes a request's sign-up intent, if it carries a valid one, visible
    to the membership write (pure ASGI: no work without the cookie)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        raw = None
        for name, value in scope.get("headers") or ():
            if name == b"cookie" and INTENT_COOKIE.encode() in value:
                from http.cookies import SimpleCookie

                try:
                    jar = SimpleCookie()
                    jar.load(value.decode("latin-1"))
                    raw = jar[INTENT_COOKIE].value if INTENT_COOKIE in jar else None
                except Exception:
                    raw = None
                break
        intent = decode_signup_intent(raw)
        if intent is None:
            return await self.app(scope, receive, send)
        token = _signup_intent.set(intent)
        try:
            return await self.app(scope, receive, send)
        finally:
            _signup_intent.reset(token)


def stamp_membership(membership, intent: SignupIntent, *, claimed_now: bool) -> None:
    """Give ``membership`` the intent's role and origin if this sign-in is
    creating it. Raises nothing by itself; anything raised here fails the
    write that creates the membership, which is the point."""
    if not membership.user_id or str(membership.organization_id) != intent.organization_id:
        return
    if membership.created_via_host_application_id or (membership.role or "member") != "member":
        return
    if claimed_now and membership.created_at is not None and membership.created_at < intent.started:
        return  # an invitation from before this window: it keeps its role
    membership.created_via_host_application_id = intent.host_id
    membership.role = intent.role


def _on_membership_write(mapper, connection, target) -> None:
    intent = _signup_intent.get()
    if intent is None:
        return
    from sqlalchemy import inspect as sa_inspect

    state = sa_inspect(target)
    claimed_now = False
    if state.persistent:
        # An existing row: only the moment it gets its user counts (a pending
        # row being claimed), never a later edit of someone's membership.
        history = state.attrs.user_id.history
        if not history.added or (history.deleted and history.deleted[0] is not None):
            return
        claimed_now = True
    # Looked up on the module so the write fails with whatever this raises.
    import app.core.workspace_access as module

    module.stamp_membership(target, intent, claimed_now=claimed_now)


def _register_membership_events() -> None:
    from sqlalchemy import event

    from app.models.membership import Membership

    if not event.contains(Membership, "before_insert", _on_membership_write):
        event.listen(Membership, "before_insert", _on_membership_write)
        event.listen(Membership, "before_update", _on_membership_write)


_register_membership_events()


async def apply_signup_intent(request: Request, db: AsyncSession, user) -> None:
    """Backstop, called by ``current_user`` before the gate: a membership
    this sign-in created that the write-time stamp did not reach (a path that
    bypasses the ORM) still gets the role before the request is decided.
    Nothing is read without the cookie."""
    if not _gated(user) or getattr(request.state, "_signup_intent_done", False):
        return
    request.state._signup_intent_done = True
    intent = decode_signup_intent(request.cookies.get(INTENT_COOKIE))
    if intent is None:
        return
    from app.services.host_identity import apply_new_user_role

    if await apply_new_user_role(db, intent.host_id, intent.organization_id, intent.role, str(user.id),
                                 since=intent.started):
        from app.routes.host_applications import audit

        await audit(db, intent.organization_id, "embed.user_created", user_id=str(user.id),
                    resource_id=intent.host_id, request=request, details={"role": intent.role})


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
