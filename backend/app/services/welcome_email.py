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
        from app.services.email_branding import cta_button

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
        greeting = f"Hi {name}," if name else "Welcome,"

        if agent_names:
            shown = agent_names[:5]
            items = "".join(f"<li>{n}</li>" for n in shown)
            extra = f"<li>…and {len(agent_names) - len(shown)} more</li>" if len(agent_names) > len(shown) else ""
            agents_block = (
                "You already have access to these agents:"
                f"<ul style=\"margin:8px 0;padding-left:20px;\">{items}{extra}</ul>"
            )
        else:
            agents_block = (
                "Your team hasn't connected any agents yet — once they do, "
                "they'll show up here for you to chat with."
            )

        body = (
            f"{greeting}<br /><br />"
            "Welcome to <strong>BOW</strong> — where you chat with your data and get "
            "answers, charts, and reports in plain language."
            "<br /><br />"
            f"{agents_block}"
            "<br /><br />"
            f"{cta_button(base_url, 'Open BOW')}"
        )

        result = await notification_service.send_custom_email(
            recipients=[recipient],
            subject="Welcome to BOW",
            body=body,
            subtype="html",
            retries=2,
            timeout=15,
        )
        if result.status != "sent":
            logger.error("Welcome email to %s failed: %s", recipient, result.error)
    except Exception as e:
        logger.warning("Failed to send welcome email for user %s: %s", user_id, e)
