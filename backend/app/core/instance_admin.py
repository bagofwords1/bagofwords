"""Instance-level administration: who is an instance administrator
("superadmin"), which settings belong to the instance rather than to one
organization, and who may create another organization.

Instance administrators are named in ``auth.superuser_emails`` /
``BOW_SUPERUSER_EMAILS``. A non-empty list is authoritative: it is applied to
every account at startup and to each account when it signs in. An empty list
leaves ``users.is_superuser`` exactly as stored.
"""
from __future__ import annotations

from typing import FrozenSet, Optional

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import AppError, ErrorCode
from app.models.organization import Organization
from app.models.user import User
from app.settings.config import settings
from app.settings.logging_config import get_logger

logger = get_logger(__name__)

MULTI_ORG_LICENSE_FEATURE = "multi_org"


def configured_superuser_emails() -> FrozenSet[str]:
    emails = getattr(settings.bow_config.auth, "superuser_emails", None) or []
    return frozenset(str(e).strip().lower() for e in emails if str(e or "").strip())


def is_reserved_instance_admin_email(email: str) -> bool:
    """Whether ``email`` is named in the configured instance-administrator
    list. The role follows the address, so an account may only be created
    under such an address with proof of it."""
    return (email or "").strip().lower() in configured_superuser_emails()


def directory_sign_in_refused_for_superuser(user) -> bool:
    """Whether a directory (SAML, LDAP) sign-in must be refused because the
    account is an instance administrator.

    With no configured list, an instance administrator is an account whose
    flag was set by hand: a local break-glass account that no directory
    identity may sign in as. With ``BOW_SUPERUSER_EMAILS`` set, the list alone
    decides who is an instance administrator and the operator means those
    people to be ordinary directory users with the role, so the stored flag
    (which this sign-in is about to bring in line with the list) refuses
    nobody. Which accounts a directory identity may be attached to at all is
    decided by the caller and is not relaxed here.
    """
    if not getattr(user, "is_superuser", False):
        return False
    return not configured_superuser_emails()


async def _audit_superuser_change(db: AsyncSession, user_id: str, email: str, granted: bool) -> None:
    try:
        from app.ee.audit.service import audit_service

        await audit_service.log(
            db=db,
            organization_id=None,
            action="user.superuser_granted" if granted else "user.superuser_revoked",
            resource_type="user",
            resource_id=str(user_id),
            details={"email": email, "source": "BOW_SUPERUSER_EMAILS"},
        )
    except Exception:
        logger.debug("superuser audit log failed", exc_info=True)


async def sync_superusers(db: AsyncSession) -> None:
    """Make ``users.is_superuser`` match the configured list, for every account.

    Idempotent, so it is safe to run from every worker and replica at once.
    Service accounts are never instance administrators.
    """
    emails = configured_superuser_emails()
    if not emails:
        return
    on_list = func.lower(User.email).in_(emails)
    rows = (
        await db.execute(
            select(User.id, User.email, User.is_superuser).where(
                (on_list & User.is_superuser.is_(False) & User.is_service_account.is_(False))
                | (User.is_superuser.is_(True) & (~on_list | User.is_service_account.is_(True)))
            )
        )
    ).all()
    if not rows:
        return
    grant = [str(r.id) for r in rows if not r.is_superuser]
    revoke = [str(r.id) for r in rows if r.is_superuser]
    if grant:
        await db.execute(update(User).where(User.id.in_(grant)).values(is_superuser=True))
    if revoke:
        await db.execute(update(User).where(User.id.in_(revoke)).values(is_superuser=False))
    await db.commit()
    logger.info("Instance administrators synced from configuration: %d granted, %d revoked", len(grant), len(revoke))
    for r in rows:
        await _audit_superuser_change(db, str(r.id), r.email, granted=not r.is_superuser)


async def sync_superusers_at_startup() -> None:
    """Startup hook. Never raises: a failure here must not stop the app."""
    try:
        if not configured_superuser_emails():
            return
        from app.dependencies import async_session_maker

        async with async_session_maker() as db:
            await sync_superusers(db)
    except Exception:
        logger.exception("Could not apply BOW_SUPERUSER_EMAILS at startup; continuing")


async def apply_superuser_policy(user) -> None:
    """Bring one account in line with the configured list (sign-in, sign-up).

    Best-effort: a failure is logged and never blocks the sign-in.
    """
    try:
        emails = configured_superuser_emails()
        if not emails or user is None:
            return
        should = (
            (getattr(user, "email", "") or "").strip().lower() in emails
            and not getattr(user, "is_service_account", False)
        )
        from app.dependencies import async_session_maker

        async with async_session_maker() as db:
            result = await db.execute(
                update(User)
                .where(User.id == str(user.id), User.is_superuser.is_(not should))
                .values(is_superuser=should)
            )
            await db.commit()
            if result.rowcount:
                await _audit_superuser_change(db, str(user.id), user.email, granted=should)
        try:
            user.is_superuser = should
        except Exception:
            pass
    except Exception:
        logger.warning("Could not apply BOW_SUPERUSER_EMAILS for user %s", getattr(user, "id", None), exc_info=True)


async def organization_count(db: AsyncSession) -> int:
    """Every organization row, soft-deleted ones included. Used for "has this
    deployment ever had an organization" (the free first one), where a deleted
    row must not reopen the first-run path."""
    return int((await db.execute(select(func.count(Organization.id)))).scalar() or 0)


async def live_organization_count(db: AsyncSession) -> int:
    return int((await db.execute(
        select(func.count(Organization.id)).where(Organization.deleted_at.is_(None))
    )).scalar() or 0)


async def instance_settings_require_superuser(db: AsyncSession) -> bool:
    """Whether instance-wide settings (license, system logs, product updates,
    sites allowed to frame the app) are reserved for instance administrators.

    With a single organization its admins own the instance. As soon as the
    instance can hold, or already holds, more than one organization, one
    organization's admin must not decide for the others.
    """
    if settings.bow_config.features.allow_multiple_organizations:
        return True
    # A soft-deleted organization has no admins left to protect.
    return await live_organization_count(db) > 1


async def is_instance_settings_admin(db: AsyncSession, user) -> bool:
    if getattr(user, "is_superuser", False):
        return True
    return not await instance_settings_require_superuser(db)


def additional_organization_refusal(user) -> Optional[AppError]:
    """Why ``user`` may not create an organization beyond the first, or None.

    The first organization of a deployment is not covered here: anyone signed
    in may create it, with no license.
    """
    if not settings.bow_config.features.allow_multiple_organizations:
        return AppError.bad_request(
            ErrorCode.ORGANIZATION_MULTIPLE_NOT_ENABLED,
            "You cannot create more than one organization",
        )
    if not getattr(user, "is_superuser", False) or getattr(user, "is_service_account", False):
        return AppError.forbidden(
            ErrorCode.ORGANIZATION_CREATE_INSTANCE_ADMIN_REQUIRED,
            "Only an instance administrator can create an organization",
        )
    from app.ee.license import has_feature

    if not has_feature(MULTI_ORG_LICENSE_FEATURE):
        return AppError(
            ErrorCode.ORGANIZATION_CREATE_LICENSE_REQUIRED,
            "Creating another organization requires an enterprise license",
            status_code=402,
        )
    return None


def can_create_additional_organization(user) -> bool:
    return additional_organization_refusal(user) is None
