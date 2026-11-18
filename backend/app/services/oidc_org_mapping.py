"""Operator-owned placement of people into organizations by identity-provider group.

An OIDC provider in ``bow-config.yaml`` may carry ``org_mapping`` rules
(``{group, organization, role}``). At every sign-in through that provider the
rules are compared with the groups the provider asserts for the person:

- A matching rule ensures a live membership in its organization. A membership
  created here is stamped ``directory_provider = "oidc_org_mapping:<provider>"``.
- The mapped role is applied when the membership is created. On later sign-ins
  a higher mapped role is granted, but a role is never lowered: an organization
  admin may have granted it by hand, and the two cannot be told apart.
- A membership created by this provider's mapping is removed, through the same
  path as the members list, once the person holds no group mapped to that
  organization. Memberships that came from anywhere else (invite, SCIM, manual)
  are never removed here, even when a rule matched them at some point.

Nothing here may fail the sign-in: every organization is handled on its own and
errors are logged.
"""

from typing import Dict, List, Optional, Set

from fastapi import HTTPException
from sqlalchemy import select

from app.models.membership import Membership
from app.models.organization import Organization
from app.models.user import User
from app.settings.bow_config import ORG_MAPPING_MARKER_PREFIX, ORG_MAPPING_ROLES
from app.settings.logging_config import get_logger

logger = get_logger(__name__)

# (provider, organization reference) pairs already reported as unresolvable, so
# a stale rule is logged once per process instead of at every sign-in.
_warned_unresolved: Set[tuple] = set()


def mapping_marker(provider_name: str) -> str:
    """``Membership.directory_provider`` value for memberships this provider's
    mapping created."""
    return ORG_MAPPING_MARKER_PREFIX + provider_name


def _rank(role: Optional[str]) -> int:
    try:
        return ORG_MAPPING_ROLES.index((role or "").lower())
    except ValueError:
        return -1


def _matches(rule_group: str, group_ids: Set[str], group_names: Set[str]) -> bool:
    # Ids compare case-insensitively (GUID casing differs between Microsoft
    # surfaces); display names must match exactly.
    return rule_group.casefold() in group_ids or rule_group in group_names


async def _resolve_organization(db, provider_name: str, reference: str) -> Optional[str]:
    """Organization id for a rule's ``organization`` (an id, or an exact name)."""
    org_id = await db.scalar(
        select(Organization.id).where(Organization.id == reference, Organization.deleted_at.is_(None))
    )
    if org_id:
        return str(org_id)
    by_name = (
        await db.scalars(
            select(Organization.id).where(Organization.name == reference, Organization.deleted_at.is_(None)).limit(2)
        )
    ).all()
    if len(by_name) == 1:
        return str(by_name[0])
    key = (provider_name, reference)
    if key not in _warned_unresolved:
        _warned_unresolved.add(key)
        logger.warning(
            "OIDC org mapping (%s): organization '%s' %s; rules for it are skipped",
            provider_name,
            reference,
            "is the name of more than one organization — use its id" if by_name else "does not exist",
        )
    return None


async def _ensure_membership(db, provider_name: str, user: User, organization_id: str, role: str) -> None:
    from app.core.permission_resolver import (
        claim_pending_invite,
        ensure_system_role_assignment,
        insert_membership_if_absent,
    )
    from app.core.seats import has_seat_for
    from app.core.user_lifecycle import reactivate_user_for_membership

    user_id = str(user.id)
    membership = (
        await db.scalars(
            select(Membership)
            .where(
                Membership.organization_id == organization_id,
                Membership.user_id == user_id,
                Membership.deleted_at.is_(None),
            )
            .limit(1)
        )
    ).first()

    if membership is None:
        # An open invite is the admin's own decision: claim it (keeping its
        # role and leaving it unmarked) instead of creating a second row.
        membership = await claim_pending_invite(db, user, organization_id)
        if membership is not None:
            await reactivate_user_for_membership(db, user_id, organization_id)
            logger.info(
                "OIDC org mapping (%s): user %s claimed an open invite in org %s",
                provider_name, user_id, organization_id,
            )

    if membership is None:
        if not await has_seat_for(db, organization_id):
            logger.warning(
                "OIDC org mapping (%s): org %s is at its license seat limit; "
                "skipping new membership for user %s",
                provider_name, organization_id, user_id,
            )
            return
        membership, created = await insert_membership_if_absent(
            db,
            Membership(
                user_id=user_id,
                organization_id=organization_id,
                role=role,
                directory_provider=mapping_marker(provider_name),
            ),
        )
        if created:
            await reactivate_user_for_membership(db, user_id, organization_id)
            await ensure_system_role_assignment(db, organization_id, user_id, role)
            logger.info(
                "OIDC org mapping (%s): added user %s to org %s as %s",
                provider_name, user_id, organization_id, role,
            )
            return

    if _rank(role) > _rank(membership.role):
        from app.services.organization_service import OrganizationService

        previous = membership.role
        membership.role = role
        # Same switch the member-edit path makes, so RBAC follows the role.
        await OrganizationService()._sync_system_role_assignment(db, organization_id, user_id, role)
        logger.info(
            "OIDC org mapping (%s): raised user %s in org %s from %s to %s",
            provider_name, user_id, organization_id, previous, role,
        )


async def _wanted_roles(db, cfg, group_ids, group_names) -> Dict[str, str]:
    """organization id -> highest role among the rules these groups match."""
    ids = {str(g).casefold() for g in (group_ids or [])}
    names = {n for n in (group_names or {}).values() if n}
    wanted: Dict[str, str] = {}
    resolved: Dict[str, Optional[str]] = {}
    for rule in getattr(cfg, "org_mapping", None) or []:
        if not _matches(rule.group, ids, names):
            continue
        if rule.organization not in resolved:
            resolved[rule.organization] = await _resolve_organization(db, cfg.name, rule.organization)
        org_id = resolved[rule.organization]
        if org_id and _rank(rule.role) > _rank(wanted.get(org_id)):
            wanted[org_id] = rule.role
    return wanted


async def mapping_admits(cfg, group_ids, group_names) -> bool:
    """Whether the mapping places a person with these groups in at least one
    existing organization (which stands in for an invitation at first sign-in)."""
    if not (getattr(cfg, "org_mapping", None) and group_ids):
        return False
    from app.dependencies import async_session_maker

    async with async_session_maker() as db:
        return bool(await _wanted_roles(db, cfg, group_ids, group_names))


async def apply_org_mapping(cfg, user, group_ids: List[str], group_names: Optional[Dict[str, Optional[str]]]) -> None:
    """Bring ``user``'s memberships in line with ``cfg.org_mapping``.

    ``group_ids`` must be the complete list the provider asserts for this
    sign-in (an empty list means "no groups"); callers must not call this when
    the groups could not be determined, or mapped memberships would be removed
    on a provider outage.
    """
    rules = list(getattr(cfg, "org_mapping", None) or [])
    if not rules:
        return

    from app.dependencies import async_session_maker

    provider_name = cfg.name

    async with async_session_maker() as db:
        account = await db.scalar(select(User).where(User.id == str(user.id)))
        if account is None:
            return
        user_id = str(account.id)
        wanted = await _wanted_roles(db, cfg, group_ids, group_names)

        for org_id, role in wanted.items():
            try:
                # Re-read per organization: a commit or rollback expires the row.
                account = await db.scalar(select(User).where(User.id == user_id))
                await _ensure_membership(db, provider_name, account, org_id, role)
                await db.commit()
            except Exception as exc:
                await db.rollback()
                logger.warning(
                    "OIDC org mapping (%s): could not place user %s in org %s: %s",
                    provider_name, user_id, org_id, exc, exc_info=True,
                )

        stale = (
            await db.execute(
                select(Membership.id, Membership.organization_id).where(
                    Membership.user_id == user_id,
                    Membership.deleted_at.is_(None),
                    Membership.directory_provider == mapping_marker(provider_name),
                )
            )
        ).all()
        for membership_id, org_id in stale:
            if str(org_id) in wanted:
                continue
            try:
                from app.services.organization_service import OrganizationService

                # The members-list path, so roles, groups, grants, API keys and
                # schedules held in that organization end with the membership.
                await OrganizationService().remove_member(db, str(org_id), str(membership_id), None, None)
                logger.info(
                    "OIDC org mapping (%s): removed user %s from org %s (no mapped group left)",
                    provider_name, user_id, org_id,
                )
            except HTTPException as exc:
                await db.rollback()
                # e.g. the organization's last administrator is never removed.
                logger.warning(
                    "OIDC org mapping (%s): kept user %s in org %s: %s",
                    provider_name, user_id, org_id, exc.detail,
                )
            except Exception as exc:
                await db.rollback()
                logger.warning(
                    "OIDC org mapping (%s): could not remove user %s from org %s: %s",
                    provider_name, user_id, org_id, exc, exc_info=True,
                )
