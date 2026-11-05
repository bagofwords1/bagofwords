# Security (authentication / session) audit events.
# Licensed under the BOW Enterprise License
# See backend/app/ee/LICENSE for details
#
# One entry point for every login provider and session hook. Routing rule
# (docs/design/audit-event-contract.md, "Pre-authentication events"): an
# event about a known account is recorded once per organization the account
# belongs to, because membership is a justified association. An event about
# an unknown account (failed login for an email with no user) has no
# organization and is recorded with organization_id NULL, where only the
# platform stream can deliver it. Nothing is routed from the login input.

import logging
from typing import Any, Dict, Iterable, Optional

from fastapi import Request
from sqlalchemy import select

from app.ee.audit import context as audit_context

logger = logging.getLogger(__name__)


async def _membership_org_ids(db, user_id: str) -> list:
    from app.models.membership import Membership

    rows = (await db.execute(
        select(Membership.organization_id).where(Membership.user_id == str(user_id), Membership.deleted_at.is_(None))
    )).scalars().all()
    return list(dict.fromkeys(str(o) for o in rows if o))


async def record_security_event(
    action: str,
    *,
    request: Optional[Request] = None,
    user: Any = None,
    user_id: Optional[str] = None,
    outcome: str = "success",
    failure_reason: Optional[str] = None,
    method: Optional[str] = None,
    details: Optional[Dict[str, Any]] = None,
    organization_ids: Optional[Iterable[str]] = None,
) -> int:
    """Record one security event for every justified organization (or once
    without one). Fire-and-forget: never raises into the auth flow. Returns
    the number of rows written."""
    try:
        from app.dependencies import async_session_maker
        from app.ee.audit.service import audit_service

        uid = str(user.id) if user is not None else (str(user_id) if user_id else None)
        payload = dict(details or {})
        if method:
            payload.setdefault("auth_method", method)
        async with async_session_maker() as db:
            orgs = list(organization_ids) if organization_ids is not None else ([] if not uid else await _membership_org_ids(db, uid))
            targets = orgs or [None]
            written = 0
            for org in targets:
                await audit_service.log(
                    db=db, organization_id=org, action=action, user_id=uid, resource_type="auth",
                    resource_id=uid, details=payload, request=request, outcome=outcome,
                    failure_reason=failure_reason, actor=user, service_account=False,
                )
                written += 1
            return written
    except Exception:
        logger.warning("record_security_event(%s) failed", action, exc_info=True)
        return 0


def session_id_of(user: Any) -> Optional[str]:
    """The opaque session id minted for this login, if the strategy stashed it."""
    return getattr(user, "_session_id", None)
