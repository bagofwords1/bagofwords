"""The organization the current request was authorized against.

Set by ``get_current_organization`` (core/auth.py) once the caller's
membership is confirmed, and by the other entry points that establish one
organization for a unit of work: MCP auth, the SCIM token, an embedded
session, an inbound chat message, a scheduled prompt run, a webhook delivery. Each request is handled in its own task, so the value
never carries over to another request. Response schemas that embed a person
(``UserSchema``) read it to leave out that person's records from other
organizations — the account row is shared by every organization someone
belongs to, but what hangs off it (chat identities, ...) is per organization.

``None`` means "no organization in play" (routes without an organization,
background jobs): nothing is filtered.
"""
from __future__ import annotations

from contextvars import ContextVar
from typing import Optional

_organization_id: ContextVar[Optional[str]] = ContextVar("bow_request_organization_id", default=None)


def set_request_organization_id(organization_id: Optional[str]) -> None:
    _organization_id.set(str(organization_id) if organization_id else None)


def current_request_organization_id() -> Optional[str]:
    return _organization_id.get()
