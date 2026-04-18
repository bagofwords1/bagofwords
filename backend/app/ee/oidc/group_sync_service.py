# OIDC Group Sync Service
# Licensed under the Business Source License 1.1
#
# Syncs group memberships from OIDC token claims (e.g., Entra ID groups)
# into BOW Groups + GroupMemberships on each user login.

import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional, Set

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.models.group import Group
from app.models.group_membership import GroupMembership
from app.models.membership import Membership
from app.ee.ldap.schemas import SyncResult

logger = logging.getLogger(__name__)

PROVIDER_NAME = "oidc"


async def sync_user_oidc_groups(
    db: AsyncSession,
    user_id: str,
    organization_id: str,
    group_ids: List[str],
    group_names: Optional[Dict[str, str]] = None,
) -> SyncResult:
    """Sync a single user's group memberships from OIDC token claims.

    Args:
        db: Database session.
        user_id: The BOW user ID.
        organization_id: The org to sync groups into.
        group_ids: List of external group IDs from the id_token groups claim.
        group_names: Optional mapping of group_id → display name (from Graph API).
                     If not provided, group IDs are used as names.

    Returns:
        SyncResult with counts of groups created/updated and memberships added/removed.
    """
    result = SyncResult(timestamp=datetime.now(timezone.utc))
    if not group_ids:
        return result

    group_names = group_names or {}

    # Get existing OIDC-synced groups for this org
    existing_groups = await _get_oidc_groups(db, organization_id)
    existing_by_ext_id: Dict[str, Group] = {
        g.external_id: g for g in existing_groups if g.external_id
    }

    # Upsert groups from token claims
    token_group_ids: Set[str] = set()
    for ext_id in group_ids:
        token_group_ids.add(ext_id)
        name = group_names.get(ext_id, ext_id)

        if ext_id in existing_by_ext_id:
            group = existing_by_ext_id[ext_id]
            if group.name != name and ext_id in group_names:
                group.name = name
                result.groups_updated += 1
        else:
            group = Group(
                organization_id=organization_id,
                name=name,
                external_id=ext_id,
                external_provider=PROVIDER_NAME,
            )
            db.add(group)
            await db.flush()
            existing_by_ext_id[ext_id] = group
            result.groups_created += 1
            logger.info(
                f"OIDC group sync: created group '{name}' (external_id={ext_id}) "
                f"in org {organization_id}"
            )

    # Sync this user's memberships
    # Current: OIDC groups user is currently a member of
    current_group_ext_ids = await _get_user_oidc_group_ext_ids(
        db, user_id, organization_id
    )
    # Target: OIDC groups from the token
    target_group_ext_ids = token_group_ids

    to_add = target_group_ext_ids - current_group_ext_ids
    to_remove = current_group_ext_ids - target_group_ext_ids

    for ext_id in to_add:
        group = existing_by_ext_id.get(ext_id)
        if group:
            db.add(GroupMembership(group_id=group.id, user_id=user_id))
            result.memberships_added += 1

    if to_remove:
        # Find and delete memberships for groups the user is no longer in
        for ext_id in to_remove:
            group = existing_by_ext_id.get(ext_id)
            if not group:
                continue
            stmt = select(GroupMembership).where(
                GroupMembership.group_id == group.id,
                GroupMembership.user_id == user_id,
            )
            row = (await db.execute(stmt)).scalar_one_or_none()
            if row:
                await db.delete(row)
                result.memberships_removed += 1

    # Ensure user has org Membership
    await _ensure_org_membership(db, organization_id, user_id)

    await db.commit()

    logger.info(
        f"OIDC group sync for user {user_id} in org {organization_id}: "
        f"created={result.groups_created} updated={result.groups_updated} "
        f"added={result.memberships_added} removed={result.memberships_removed}"
    )
    return result


async def _get_oidc_groups(db: AsyncSession, organization_id: str) -> List[Group]:
    """Get all OIDC-synced groups for an org."""
    stmt = (
        select(Group)
        .where(Group.organization_id == organization_id)
        .where(Group.external_provider == PROVIDER_NAME)
        .where(Group.deleted_at.is_(None))
    )
    return list((await db.execute(stmt)).scalars().all())


async def _get_user_oidc_group_ext_ids(
    db: AsyncSession, user_id: str, organization_id: str
) -> Set[str]:
    """Get external_ids of OIDC groups the user is currently a member of."""
    stmt = (
        select(Group.external_id)
        .join(GroupMembership, GroupMembership.group_id == Group.id)
        .where(Group.organization_id == organization_id)
        .where(Group.external_provider == PROVIDER_NAME)
        .where(Group.deleted_at.is_(None))
        .where(GroupMembership.user_id == user_id)
    )
    rows = (await db.execute(stmt)).scalars().all()
    return set(r for r in rows if r)


async def _ensure_org_membership(
    db: AsyncSession, organization_id: str, user_id: str
) -> None:
    """Ensure the user has an org Membership (create if missing)."""
    stmt = (
        select(Membership)
        .where(Membership.organization_id == organization_id)
        .where(Membership.user_id == user_id)
        .where(Membership.deleted_at.is_(None))
    )
    existing = (await db.execute(stmt)).scalar_one_or_none()
    if not existing:
        db.add(Membership(
            user_id=user_id,
            organization_id=organization_id,
            role="member",
        ))
        logger.info(
            f"OIDC group sync: auto-created org membership for user {user_id} "
            f"in org {organization_id}"
        )
