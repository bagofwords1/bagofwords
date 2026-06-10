"""Welcome email sent once, just after a user registers.

Simple by design: a short greeting, a summary of the agents (data sources) the
user can already access, and a single CTA into the app. SMTP-gated; never raises
into the registration flow (callers fire it best-effort).
"""

import logging
from typing import List

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


async def send_welcome_email(user_id: str) -> None:
    """Send the welcome email. Safe to call fire-and-forget; swallows errors."""
    try:
        from app.settings.config import settings
        if settings.email_client is None:
            return

        from sqlalchemy import select
        from app.dependencies import async_session_maker
        from app.models.user import User
        from app.models.membership import Membership
        from app.services.notification_service import notification_service
        from app.services.email_copy import welcome_email as build_welcome_email

        async with async_session_maker() as db:
            user = await db.get(User, user_id)
            if not user or not getattr(user, "email", None):
                return

            membership = (await db.execute(
                select(Membership).where(Membership.user_id == user_id)
            )).scalars().first()
            org_id = membership.organization_id if membership else None

            agent_names = await _accessible_agent_names(db, user_id, org_id) if org_id else []
            recipient = user.email
            name = getattr(user, "name", None)

        base_url = (settings.bow_config.base_url or "http://localhost:3000").rstrip("/")
        subject, body = build_welcome_email(name, agent_names, base_url)

        result = await notification_service.send_custom_email(
            recipients=[recipient],
            subject=subject,
            body=body,
            subtype="plain",
            retries=2,
            timeout=15,
        )
        if result.status != "sent":
            logger.error("Welcome email to %s failed: %s", recipient, result.error)
    except Exception as e:
        logger.warning("Failed to send welcome email for user %s: %s", user_id, e)
