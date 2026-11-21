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
  path as the members list, once no current rule for that organization applies
  to the person: they left the group, or the rule (even the last one) was
  deleted. Removal needs certainty — an organization with a rule that cannot
  be decided this sign-in (keyed by display name while a group's name could
  not be resolved) is left alone. Memberships that came from anywhere else
  (invite, SCIM, manual) are never removed here, even when a rule matched them.

Nothing here may fail the sign-in: every organization is handled on its own and
errors are logged.
"""

import re
from typing import Dict, Optional, Set

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
    # Only GUID object ids compare case-insensitively (their casing differs
    # between Microsoft surfaces). Everything else is a name or a provider's
    # own opaque id and must match exactly: a directory may hold both
    # "Finance" and "finance".
    if _GUID_RE.match(rule_group):
        return rule_group.casefold() in group_ids
    return rule_group in group_names


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


_GUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)

# (provider, rule group) pairs already reported as impossible to decide by name.
_warned_undecidable: Set[tuple] = set()


def _has_unnamed_groups(group_ids, group_names) -> bool:
    """Whether the sign-in carries a group whose display name is unknown.

    Only opaque object ids count: a readable claim value (Okta, Keycloak, ...)
    is the group's name already. While such a group exists, a rule keyed by
    display name cannot be ruled out — the person may be in exactly that group.
    """
    names = group_names or {}
    return any(_GUID_RE.match(str(g)) and not names.get(g) for g in (group_ids or []))


async def _evaluate_rules(db, cfg, group_ids, group_names):
    """``(wanted, undecided)`` for this sign-in's groups.

    ``wanted``: organization id -> highest role among the rules that match.
    ``undecided``: organizations with a rule that did not match but could not
    be ruled out either (keyed by display name while a group's name is
    unknown). A membership there is neither granted nor removed this sign-in.
    """
    claimed = [str(g) for g in (group_ids or [])]
    # GUID object ids, folded. A claim value that is not a GUID is the group's
    # name (Okta, Keycloak, ...) or an opaque id, and keeps its exact spelling.
    ids = {g.casefold() for g in claimed if _GUID_RE.match(g)}
    names = {g for g in claimed if not _GUID_RE.match(g)}
    names |= {n for n in (group_names or {}).values() if n}
    unnamed = _has_unnamed_groups(group_ids, group_names)
    wanted: Dict[str, str] = {}
    undecided: Set[str] = set()
    resolved: Dict[str, Optional[str]] = {}
    for rule in getattr(cfg, "org_mapping", None) or []:
        matched = _matches(rule.group, ids, names)
        # An id-keyed rule is decided by the ids alone.
        unsure = not matched and unnamed and not _GUID_RE.match(rule.group)
        if not matched and not unsure:
            continue
        if rule.organization not in resolved:
            resolved[rule.organization] = await _resolve_organization(db, cfg.name, rule.organization)
        org_id = resolved[rule.organization]
        if not org_id:
            continue
        if matched:
            if _rank(rule.role) > _rank(wanted.get(org_id)):
                wanted[org_id] = rule.role
            continue
        undecided.add(org_id)
        if not getattr(cfg, "resolve_group_names", False):
            key = (cfg.name, rule.group)
            if key not in _warned_undecidable:
                _warned_undecidable.add(key)
                logger.warning(
                    "OIDC org mapping (%s): rule for group '%s' is keyed by display name but the "
                    "provider sends group ids and resolve_group_names is off; it can never match, "
                    "and memberships in its organization are never removed. Use the group id or "
                    "enable resolve_group_names.",
                    cfg.name, rule.group,
                )
    return wanted, undecided


async def mapping_admits(cfg, group_ids, group_names) -> bool:
    """Whether the mapping places a person with these groups in at least one
    existing organization (which stands in for an invitation at first sign-in)."""
    if not (getattr(cfg, "org_mapping", None) and group_ids):
        return False
    from app.dependencies import async_session_maker

    async with async_session_maker() as db:
        wanted, _ = await _evaluate_rules(db, cfg, group_ids, group_names)
        return bool(wanted)


async def apply_org_mapping(cfg, user, groups) -> None:
    """Bring ``user``'s memberships in line with ``cfg.org_mapping`` as it is now.

    ``groups`` is ``(group_ids, group_names)`` — the complete list the provider
    asserts for this sign-in (an empty list means "no groups") and the display
    names known for those ids — or None when the groups could not be determined.

    Memberships this provider's mapping created are reconciled against the
    current rules, an empty rule set included: deleting a rule (or all of them)
    ends the memberships it produced at the person's next sign-in. A removal
    needs certainty that no rule for that organization applies, so with rules
    configured nothing is removed while the groups are unknown, and an
    organization with a rule that cannot be decided this sign-in is left alone.
    """
    from app.dependencies import async_session_maker

    rules = list(getattr(cfg, "org_mapping", None) or [])
    provider_name = cfg.name
    marker = mapping_marker(provider_name)
    user_id = str(user.id)
    if rules and groups is None:
        return

    async with async_session_maker() as db:
        if not rules:
            # Providers that never had a mapping stop here, after one lookup.
            marked = await db.scalar(
                select(Membership.id).where(
                    Membership.user_id == user_id,
                    Membership.deleted_at.is_(None),
                    Membership.directory_provider == marker,
                ).limit(1)
            )
            if marked is None:
                return
            wanted, undecided = {}, set()
        else:
            if await db.scalar(select(User.id).where(User.id == user_id)) is None:
                return
            wanted, undecided = await _evaluate_rules(db, cfg, groups[0], groups[1])

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
                    Membership.directory_provider == marker,
                )
            )
        ).all()
        for membership_id, org_id in stale:
            if str(org_id) in wanted:
                continue
            if str(org_id) in undecided:
                logger.info(
                    "OIDC org mapping (%s): kept user %s in org %s this sign-in "
                    "(a group's display name could not be resolved)",
                    provider_name, user_id, org_id,
                )
                continue
            try:
                from app.services.organization_service import OrganizationService

                # The members-list path, so roles, groups, grants, API keys and
                # schedules held in that organization end with the membership.
                await OrganizationService().remove_member(db, str(org_id), str(membership_id), None, None)
                logger.info(
                    "OIDC org mapping (%s): removed user %s from org %s (no rule applies any more)",
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
