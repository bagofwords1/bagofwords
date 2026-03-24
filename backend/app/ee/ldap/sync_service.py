# LDAP Group Sync Service
# Licensed under the Business Source License 1.1
# See ENTERPRISE_LICENSE for details

import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional, Set

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.models.group import Group
from app.models.group_membership import GroupMembership
from app.models.user import User
from app.models.membership import Membership
from app.ee.ldap.connection import LDAPConnectionManager
from app.ee.ldap.schemas import SyncResult, LDAPSyncPreview, LDAPGroupPreview
from app.settings.bow_config import LDAPConfig

logger = logging.getLogger(__name__)

PROVIDER_NAME = "ldap"


class LDAPGroupSyncService:

    def __init__(self, config: LDAPConfig):
        self.config = config
        self.connection = LDAPConnectionManager(config)

    async def sync_groups(self, db: AsyncSession, organization_id: str) -> SyncResult:
        """
        Full sync of LDAP groups into the application.

        Algorithm:
        1. Fetch all LDAP groups and users
        2. Build DN→email lookup
        3. Match LDAP groups to existing Group records by external_id
        4. Create/update groups, diff memberships
        5. Mark removed LDAP groups as deleted
        """
        result = SyncResult(timestamp=datetime.now(timezone.utc))

        try:
            ldap_groups = self.connection.search_groups()
            ldap_users = self.connection.search_users()
        except Exception as e:
            result.errors.append(f"LDAP search failed: {e}")
            logger.error(f"LDAP sync failed for org {organization_id}: {e}")
            return result

        # Build DN→email map for member resolution
        dn_to_email: Dict[str, str] = {u["dn"]: u["email"] for u in ldap_users}

        # Build email→user_id map for org members
        email_to_user_id = await self._get_org_user_map(db, organization_id)

        # Get existing LDAP-synced groups for this org
        existing_groups = await self._get_ldap_groups(db, organization_id)
        existing_by_dn: Dict[str, Group] = {g.external_id: g for g in existing_groups if g.external_id}

        seen_dns: Set[str] = set()

        for ldap_group in ldap_groups:
            group_dn = ldap_group["dn"]
            group_name = ldap_group["name"]
            seen_dns.add(group_dn)

            # Resolve LDAP members to app user IDs
            target_user_ids = self._resolve_members(
                ldap_group["members"], dn_to_email, email_to_user_id, result
            )

            if group_dn in existing_by_dn:
                # Update existing group
                group = existing_by_dn[group_dn]
                if group.name != group_name:
                    group.name = group_name
                    result.groups_updated += 1

                await self._sync_memberships(db, group, target_user_ids, result)
            else:
                # Create new group
                group = Group(
                    organization_id=organization_id,
                    name=group_name,
                    external_id=group_dn,
                    external_provider=PROVIDER_NAME,
                )
                db.add(group)
                await db.flush()
                result.groups_created += 1

                await self._sync_memberships(db, group, target_user_ids, result)

        # Remove groups no longer in LDAP
        for dn, group in existing_by_dn.items():
            if dn not in seen_dns:
                group.deleted_at = datetime.utcnow()
                result.groups_removed += 1

        await db.commit()
        logger.info(
            f"LDAP sync completed for org {organization_id}: "
            f"created={result.groups_created} updated={result.groups_updated} "
            f"removed={result.groups_removed} memberships_added={result.memberships_added} "
            f"memberships_removed={result.memberships_removed}"
        )
        return result

    async def preview_sync(self, db: AsyncSession, organization_id: str) -> LDAPSyncPreview:
        """Dry-run: compute what a sync would change without writing."""
        ldap_groups = self.connection.search_groups()
        ldap_users = self.connection.search_users()

        dn_to_email: Dict[str, str] = {u["dn"]: u["email"] for u in ldap_users}
        email_to_user_id = await self._get_org_user_map(db, organization_id)

        existing_groups = await self._get_ldap_groups(db, organization_id)
        existing_by_dn: Dict[str, Group] = {g.external_id: g for g in existing_groups if g.external_id}

        preview = LDAPSyncPreview()
        seen_dns: Set[str] = set()

        for ldap_group in ldap_groups:
            group_dn = ldap_group["dn"]
            seen_dns.add(group_dn)

            target_user_ids = set()
            for member_ref in ldap_group["members"]:
                email = dn_to_email.get(member_ref) if self.config.group_member_format == "dn" else None
                if email and email in email_to_user_id:
                    target_user_ids.add(email_to_user_id[email])

            exists = group_dn in existing_by_dn
            to_add = 0
            to_remove = 0

            if exists:
                group = existing_by_dn[group_dn]
                current_ids = await self._get_current_member_ids(db, str(group.id))
                to_add = len(target_user_ids - current_ids)
                to_remove = len(current_ids - target_user_ids)
                if to_add or to_remove or group.name != ldap_group["name"]:
                    preview.groups_to_update += 1
            else:
                preview.groups_to_create += 1
                to_add = len(target_user_ids)

            preview.total_membership_changes += to_add + to_remove
            preview.groups.append(LDAPGroupPreview(
                dn=group_dn,
                name=ldap_group["name"],
                member_count=len(ldap_group["members"]),
                exists_in_app=exists,
                members_to_add=to_add,
                members_to_remove=to_remove,
            ))

        # Groups to remove
        for dn in existing_by_dn:
            if dn not in seen_dns:
                preview.groups_to_remove += 1

        return preview

    def _resolve_members(
        self,
        member_refs: List[str],
        dn_to_email: Dict[str, str],
        email_to_user_id: Dict[str, str],
        result: SyncResult,
    ) -> Set[str]:
        """Resolve LDAP member references to app user IDs."""
        user_ids: Set[str] = set()
        for ref in member_refs:
            if self.config.group_member_format == "dn":
                email = dn_to_email.get(ref)
            else:
                # memberUid format — ref is the uid, try as email
                email = ref

            if not email:
                result.users_not_found += 1
                continue

            user_id = email_to_user_id.get(email.lower())
            if user_id:
                user_ids.add(user_id)
            else:
                result.users_not_found += 1
        return user_ids

    async def _sync_memberships(
        self,
        db: AsyncSession,
        group: Group,
        target_user_ids: Set[str],
        result: SyncResult,
    ) -> None:
        """Add/remove GroupMembership rows to match target set."""
        current_ids = await self._get_current_member_ids(db, str(group.id))

        to_add = target_user_ids - current_ids
        to_remove = current_ids - target_user_ids

        for user_id in to_add:
            db.add(GroupMembership(group_id=group.id, user_id=user_id))
            result.memberships_added += 1

        if to_remove:
            stmt = select(GroupMembership).where(
                GroupMembership.group_id == group.id,
                GroupMembership.user_id.in_(to_remove),
            )
            rows = (await db.execute(stmt)).scalars().all()
            for row in rows:
                await db.delete(row)
                result.memberships_removed += 1

    async def _get_current_member_ids(self, db: AsyncSession, group_id: str) -> Set[str]:
        stmt = select(GroupMembership.user_id).where(GroupMembership.group_id == group_id)
        rows = (await db.execute(stmt)).scalars().all()
        return set(rows)

    async def _get_org_user_map(self, db: AsyncSession, organization_id: str) -> Dict[str, str]:
        """Build email→user_id map for all users in the org."""
        stmt = (
            select(User.email, User.id)
            .join(Membership, Membership.user_id == User.id)
            .where(Membership.organization_id == organization_id)
            .where(Membership.deleted_at.is_(None))
        )
        rows = (await db.execute(stmt)).all()
        return {email.lower(): uid for email, uid in rows}

    async def _get_ldap_groups(self, db: AsyncSession, organization_id: str) -> List[Group]:
        stmt = (
            select(Group)
            .where(Group.organization_id == organization_id)
            .where(Group.external_provider == PROVIDER_NAME)
            .where(Group.deleted_at.is_(None))
        )
        return list((await db.execute(stmt)).scalars().all())
