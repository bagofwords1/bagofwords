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
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
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
# sign-up, which know nothing of the host application. The window says which
# application it belongs to with an *intent*: a short-lived signed token
# naming the host application, its organization, the role it gives new
# people, and the moment it was issued.
#
# An intent belongs to ONE transaction, never to the browser:
#
# * a registration or password/LDAP sign-in carries it in the
#   ``X-Embed-Signup`` header of that request (the form asks for it as it
#   submits, so it is always fresh);
# * an OIDC authorization gets it in a cookie that names that authorization's
#   own ``state``; the callback accepts it only for that state;
# * a SAML request gets it in a cookie bound to that request's browser token
#   (SameSite=None, as the request's own cookie is, since the assertion comes
#   back in a cross-site POST).
#
# So a window that was opened and abandoned leaves nothing behind: an
# ordinary sign-in in another tab carries no intent and is an ordinary one.
#
# The role is bound to the membership's own write. Whoever holds a valid
# intent for the request opens :func:`signup_intent_scope`; a mapper event on
# ``Membership`` (``_on_membership_write``) then stamps the role and the host
# application on a membership as it is inserted, or as a row made in this
# sign-in gets its user: in the same flush, the same transaction. There is no
# second step that could fail or be skipped and leave a workspace membership
# behind, and ``ensure_system_role_assignment`` gives such a membership the
# ``app_user`` assignment, never ``member``. When the scope ends without an
# error, each membership it stamped is audited once (``embed.user_created``).
#
# An invitation an admin sent earlier keeps the role it grants (its row is
# older than the intent). Tampering buys nothing: without an intent the
# account is an ordinary sign-up, governed by the organization's own sign-up
# policy; forging one needs the signing key and could only lower its
# bearer's access.

INTENT_HEADER = "x-embed-signup"
INTENT_LIFETIME = 10 * 60  # a form submit, or one round trip to a provider
INTENT_FORM = "form"       # what a header intent is bound to
_INTENT_AUDIENCE = "bow:embed-signup"
_OIDC_CALLBACK = re.compile(r"^/api/auth/([A-Za-z0-9_.-]+)/callback$")


class SignupIntent(NamedTuple):
    host_public_id: str
    host_id: str
    organization_id: str
    role: str
    started: datetime


_signup_intent: contextvars.ContextVar[Optional[SignupIntent]] = contextvars.ContextVar(
    "bow_signup_intent", default=None)
_stamped: contextvars.ContextVar[Optional[list]] = contextvars.ContextVar("bow_signup_stamped", default=None)


def intent_for(host) -> SignupIntent:
    from app.core.permissions_registry import APP_USER_ROLE

    role = host.new_user_role if host.new_user_role in (APP_USER_ROLE, "member") else APP_USER_ROLE
    # One second of slack: the membership's created_at is this clock too.
    return SignupIntent(host.public_id, str(host.id), str(host.organization_id), role,
                        datetime.utcnow() - timedelta(seconds=1))


def issue_signup_intent(host, bound_to: str) -> str:
    """A token for one transaction of ``host``'s sign-in window: ``bound_to``
    is INTENT_FORM, an OIDC ``state``, or a SAML request's browser hash."""
    import time

    import jwt

    from app.core.auth import SECRET

    intent = intent_for(host)
    now = int(time.time())
    return jwt.encode({"host": intent.host_public_id, "hid": intent.host_id, "org": intent.organization_id,
                       "role": intent.role, "started": now - 1, "for": str(bound_to), "aud": _INTENT_AUDIENCE,
                       "exp": now + INTENT_LIFETIME}, SECRET, algorithm="HS256")


def decode_signup_intent(raw: Optional[str], bound_to: Optional[str]) -> Optional[SignupIntent]:
    """The intent in ``raw`` if it is valid AND was issued for ``bound_to``."""
    if not raw or not bound_to:
        return None
    import jwt

    from app.core.auth import SECRET

    try:
        claims = jwt.decode(raw, SECRET, algorithms=["HS256"], audience=_INTENT_AUDIENCE)
        if str(claims.get("for") or "") != str(bound_to):
            return None
        return SignupIntent(str(claims["host"]), str(claims["hid"]), str(claims["org"]), str(claims["role"]),
                            datetime.utcfromtimestamp(float(claims["started"])))
    except Exception:
        return None


async def host_of_request(request: Request):
    """The enabled host application an authorization names (``?embed_host=``)."""
    public_id = request.query_params.get("embed_host")
    if not public_id or not re.fullmatch(r"host_[a-f0-9]{8,32}", public_id):
        return None
    from app.dependencies import async_session_maker
    from app.services.host_application_service import host_application_service

    async with async_session_maker() as db:
        host = await host_application_service.by_public_id(db, public_id)
        if host is None or not host.enabled:
            return None
        db.expunge(host)
        return host


def oidc_intent_cookie(provider: str) -> str:
    return f"oidc_{provider}_embed"


async def bind_oidc_authorization(provider: str, request: Request, response) -> None:
    """A provider's authorization is starting. Inside a host application's
    sign-in window (the page says so with ``?embed_host=``) the intent is
    bound to THIS authorization's state; otherwise any earlier one is
    dropped, so an ordinary sign-in is an ordinary one."""
    import json as _json
    from urllib.parse import parse_qs, urlparse

    from app.services.auth_providers import _cookie_secure

    name, path = oidc_intent_cookie(provider), f"/api/auth/{provider}"
    host = await host_of_request(request)
    state = None
    if host is not None:
        try:
            url = _json.loads(bytes(response.body))["authorization_url"]
            state = (parse_qs(urlparse(url).query).get("state") or [None])[0]
        except Exception:
            state = None
    if host is None or not state:
        response.delete_cookie(name, path=path)
        return
    response.set_cookie(name, issue_signup_intent(host, state), max_age=INTENT_LIFETIME, httponly=True,
                        secure=_cookie_secure(), samesite="lax", path=path)


def _cookie(scope, name: str) -> Optional[str]:
    for key, value in scope.get("headers") or ():
        if key == b"cookie" and name.encode() in value:
            from http.cookies import SimpleCookie

            try:
                jar = SimpleCookie()
                jar.load(value.decode("latin-1"))
                return jar[name].value if name in jar else None
            except Exception:
                return None
    return None


def _intent_of_scope(scope) -> Optional[SignupIntent]:
    for key, value in scope.get("headers") or ():
        if key == INTENT_HEADER.encode():
            return decode_signup_intent(value.decode("latin-1"), INTENT_FORM)
    match = _OIDC_CALLBACK.match(scope.get("path") or "")
    if match:
        from urllib.parse import parse_qs

        raw = _cookie(scope, oidc_intent_cookie(match.group(1)))
        if raw:
            state = (parse_qs((scope.get("query_string") or b"").decode("latin-1")).get("state") or [None])[0]
            return decode_signup_intent(raw, state)
    return None


@asynccontextmanager
async def signup_intent_scope(intent: Optional[SignupIntent], request: Optional[Request] = None):
    """While open, a membership this sign-in creates is stamped with the
    intent's role and host application as it is written. Each one is audited
    once when the scope ends without an error."""
    if intent is None:
        yield
        return
    stamped: list = []
    tokens = (_signup_intent.set(intent), _stamped.set(stamped))
    try:
        yield
    finally:
        _signup_intent.reset(tokens[0])
        _stamped.reset(tokens[1])
    for user_id in dict.fromkeys(stamped):
        try:
            from app.dependencies import async_session_maker
            from app.routes.host_applications import audit

            async with async_session_maker() as db:
                await audit(db, intent.organization_id, "embed.user_created", user_id=str(user_id),
                            resource_id=intent.host_id, request=request, details={"role": intent.role})
        except Exception:
            import logging

            logging.getLogger(__name__).debug("embed.user_created audit failed", exc_info=True)


class SignupIntentMiddleware:
    """Opens the scope for a request that carries its own intent (the header,
    or an OIDC callback's bound cookie). Pure ASGI; no work otherwise."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        intent = _intent_of_scope(scope)
        if intent is None:
            return await self.app(scope, receive, send)
        async with signup_intent_scope(intent):
            await self.app(scope, receive, send)


def stamp_membership(membership, intent: SignupIntent, *, claimed_now: bool) -> None:
    """Give ``membership`` the intent's role and origin if this sign-in is
    creating it. Anything raised here fails the write that creates the
    membership, which is the point."""
    if not membership.user_id or str(membership.organization_id) != intent.organization_id:
        return
    if membership.created_via_host_application_id or (membership.role or "member") != "member":
        return
    if claimed_now and membership.created_at is not None and membership.created_at < intent.started:
        return  # an invitation from before this window: it keeps its role
    membership.created_via_host_application_id = intent.host_id
    membership.role = intent.role
    stamped = _stamped.get()
    if stamped is not None:
        stamped.append(str(membership.user_id))


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
