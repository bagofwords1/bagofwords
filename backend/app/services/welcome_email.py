"""Welcome email sent once, just after a user registers.

Simple by design: a short greeting, a summary of the agents (data sources) the
user can already access, and a single CTA into the app. SMTP-gated; never raises
into the registration flow (callers fire it best-effort).
"""

import logging
from typing import List, Optional

logger = logging.getLogger(__name__)


async def _accessible_agent_names(db, user_id: str, org_id: str) -> List[str]:
    """Names of data sources ("agents") the user can access in this org."""
    from sqlalchemy import select
    from app.models.data_source import DataSource
    from app.core.permission_resolver import get_accessible_data_source_ids

    is_admin, accessible_ids = await get_accessible_data_source_ids(db, user_id, org_id)
    rows = (await db.execute(
        select(DataSource).where(
            DataSource.organization_id == org_id,
            DataSource.deleted_at.is_(None),
        )
    )).scalars().all()

    if is_admin:
        visible = rows
    else:
        allowed = set(accessible_ids)
        visible = [d for d in rows if d.is_public or str(d.id) in allowed]
    return [d.name for d in visible if d.name]


async def _welcome_organization_id(db, user_id: str, organization_id: Optional[str]):
    """The one organization the welcome email speaks for.

    The organization the caller says the user just joined, when the user really
    is a live member of it. Otherwise the most recently created live
    membership; memberships attached in the same moment are ordered by
    organization id, so the choice never depends on row order.
    """
    from sqlalchemy import select
    from app.models.membership import Membership

    rows = (await db.execute(
        select(Membership.organization_id).where(
            Membership.user_id == user_id,
            Membership.deleted_at.is_(None),
        ).order_by(Membership.created_at.desc(), Membership.organization_id.asc())
    )).scalars().all()
    if organization_id and str(organization_id) in {str(r) for r in rows}:
        return str(organization_id)
    return rows[0] if rows else None


async def send_welcome_email(user_id: str, organization_id: Optional[str] = None) -> None:
    """Send the welcome email. Safe to call fire-and-forget; swallows errors.

    One email per sign-up, for one organization: ``organization_id`` when the
    caller knows which one the user just joined, else see
    ``_welcome_organization_id``.
    """
    try:
        from app.settings.config import settings

        from app.dependencies import async_session_maker
        from app.models.user import User
        from app.services.notification_service import notification_service
        from app.services.email_client_resolver import is_outbound_available
        from app.services.email_copy import welcome_email as build_welcome_email

        async with async_session_maker() as db:
            user = await db.get(User, user_id)
            if not user or not getattr(user, "email", None):
                return

            org_id = await _welcome_organization_id(db, user_id, organization_id)

            agent_names = await _accessible_agent_names(db, user_id, org_id) if org_id else []
            recipient = user.email
            name = getattr(user, "name", None)

            base_url = (settings.bow_config.base_url or "http://localhost:3000").rstrip("/")
            subject, body = build_welcome_email(name, agent_names, base_url)

            # The org context is what lets this reach the organization's own SMTP
            # server; without it the welcome mail always leaves via the global
            # bow-config relay, and orgs with no global SMTP get nothing.
            if org_id and not await is_outbound_available(db, str(org_id), purpose="system"):
                return

            result = await notification_service.send_custom_email(
                recipients=[recipient],
                subject=subject,
                body=body,
                subtype="plain",
                retries=2,
                timeout=15,
                db=db,
                organization_id=str(org_id) if org_id else None,
                purpose="system",
            )
        if result.status != "sent":
            logger.error("Welcome email to %s failed: %s", recipient, result.error)
        else:
            logger.info("Welcome email to %s sent via %s", recipient, result.source)
    except Exception as e:
        logger.warning("Failed to send welcome email for user %s: %s", user_id, e)
