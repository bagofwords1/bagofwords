"""Delayed "you've been added to a data source" member notifications.

When a user is added as a member of a data source (an "agent" in product
terms), we email them — but only if SMTP is configured, and only after a short
delay. The delay is the whole point: if the add was a mistake and the
membership is removed within the window, the email is never sent. Send time
re-checks that the membership still exists before mailing.

The job is scheduled on the shared APScheduler (``date`` trigger), so it
survives a process restart and runs even if the request worker is gone. Every
worker runs its own scheduler against the shared job store, so the send path
claims the run to guarantee exactly one email.
"""

import logging
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

# How long to wait before a member-added email actually goes out. Short enough
# to feel timely, long enough that an accidental add can be undone first.
MEMBER_ADDED_EMAIL_DELAY_MINUTES = 5


def _job_id(data_source_id: str, user_id: str) -> str:
    return f"ds_member_email:{data_source_id}:{user_id}"


def schedule_member_added_email(
    data_source_id: str,
    user_id: str,
    added_by_user_id: str,
    organization_id: str,
) -> None:
    """Schedule a delayed member-added email.

    No-ops (silently) when SMTP isn't configured, when there's no recipient, or
    when the actor added themselves. Never raises into the caller — a member is
    added regardless of whether the notification can be scheduled.
    """
    from app.settings.config import settings

    # "if smtp is configured, an email is sent" — otherwise don't even schedule.
    if settings.email_client is None:
        return
    if not user_id:
        return
    # Don't notify someone for adding themselves (e.g. the creator/owner).
    if added_by_user_id and str(user_id) == str(added_by_user_id):
        return

    try:
        from app.core.scheduler import scheduler

        run_date = datetime.utcnow() + timedelta(minutes=MEMBER_ADDED_EMAIL_DELAY_MINUTES)
        # Unique id per (data source, user, schedule time) so re-adding after a
        # remove schedules a fresh email instead of being silently dropped.
        job_id = f"{_job_id(data_source_id, user_id)}:{int(datetime.utcnow().timestamp())}"
        scheduler.add_job(
            func="app.services.data_source_member_email:send_member_added_email",
            trigger="date",
            run_date=run_date,
            args=[str(data_source_id), str(user_id), str(added_by_user_id or ""), str(organization_id)],
            id=job_id,
            replace_existing=True,
            misfire_grace_time=3600,
        )
        logger.info(
            "Scheduled member-added email for user=%s data_source=%s at %s",
            user_id, data_source_id, run_date.isoformat(),
        )
    except Exception as e:
        # Scheduling is best-effort; never block the membership write.
        logger.warning("Failed to schedule member-added email: %s", e)


async def send_member_added_email(
    data_source_id: str,
    user_id: str,
    added_by_user_id: str,
    organization_id: str,
) -> None:
    """APScheduler job body: re-validate, then send the member-added email.

    Skips silently if: another worker already claimed this fire, SMTP is no
    longer configured, the membership was removed (the mistake-undo path), or
    the user/data source no longer exists.
    """
    import asyncio

    from app.core.scheduler import claim_scheduled_run

    # Every worker fires this once; only the claim winner proceeds.
    if not await asyncio.to_thread(claim_scheduled_run, _job_id(data_source_id, user_id)):
        return

    from app.settings.config import settings

    fm = settings.email_client
    if fm is None:
        return

    from sqlalchemy import select

    from app.dependencies import async_session_maker
    from app.models.data_source import DataSource
    from app.models.data_source_membership import DataSourceMembership, PRINCIPAL_TYPE_USER
    from app.models.user import User

    async with async_session_maker() as db:
        # Undo safety: only mail if the membership still exists. If the add was a
        # mistake and was removed within the delay window, the row is gone.
        membership = await db.execute(
            select(DataSourceMembership).where(
                DataSourceMembership.data_source_id == data_source_id,
                DataSourceMembership.principal_type == PRINCIPAL_TYPE_USER,
                DataSourceMembership.principal_id == user_id,
            )
        )
        if membership.scalar_one_or_none() is None:
            logger.info(
                "Membership user=%s data_source=%s no longer exists; skipping email",
                user_id, data_source_id,
            )
            return

        data_source = await db.get(DataSource, data_source_id)
        if data_source is None or data_source.deleted_at is not None:
            return

        user = await db.get(User, user_id)
        if user is None or not getattr(user, "email", None):
            return
        recipient = user.email
        recipient_name = getattr(user, "name", None)
        ds_name = data_source.name

        added_by = await db.get(User, added_by_user_id) if added_by_user_id else None
        added_by_name = getattr(added_by, "name", None) if added_by else None

    from fastapi_mail import MessageSchema

    base_url = getattr(settings.bow_config, "base_url", None) or "http://localhost:3000"
    agent_url = f"{base_url.rstrip('/')}/agents/{data_source_id}"

    greeting = f"Hi {recipient_name},<br /><br />" if recipient_name else ""
    if added_by_name:
        intro = f"{added_by_name} added you to <strong>{ds_name}</strong> on BOW."
    else:
        intro = f"You've been added to <strong>{ds_name}</strong> on BOW."

    subject = f"You've been given access to {ds_name}"
    body = (
        f"{greeting}"
        f"{intro}<br /><br />"
        f"You can now chat with this agent and explore its data.<br /><br />"
        f"<a href=\"{agent_url}\" "
        f"style=\"display:inline-block;padding:10px 18px;background:#111827;color:#ffffff;"
        f"text-decoration:none;border-radius:6px;font-weight:600;\">Open {ds_name}</a>"
    )

    message = MessageSchema(
        subject=subject,
        recipients=[recipient],
        body=body,
        subtype="html",
    )
    try:
        await fm.send_message(message)
        logger.info("Member-added email sent to %s for data_source=%s", recipient, data_source_id)
    except Exception as e:
        logger.error("Failed to send member-added email to %s: %s", recipient, e)
