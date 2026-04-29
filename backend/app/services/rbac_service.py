"""
RBAC service — CRUD for roles, groups, role assignments, and resource grants.
"""
import logging
from typing import List, Optional
from sqlalchemy import select, func, and_, or_, delete
from sqlalchemy.orm.attributes import flag_modified
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from fastapi import HTTPException

from app.models.role import Role
from app.models.group import Group
from app.models.group_membership import GroupMembership
from app.models.role_assignment import RoleAssignment
from app.models.resource_grant import ResourceGrant
from app.models.user import User
from app.core.permission_resolver import assert_full_admin_exists, FULL_ADMIN
from app.schemas.rbac_schema import (
    RoleCreate, RoleUpdate, RoleSchema, RoleResourceGrantInput, RoleResourceGrantOutput,
    GroupCreate, GroupUpdate, GroupSchema, GroupMemberSchema,
    RoleAssignmentCreate, RoleAssignmentSchema,
    ResourceGrantCreate, ResourceGrantUpdate, ResourceGrantSchema,
)

logger = logging.getLogger(__name__)


class RBACService:

    # ── Roles ────────────────────────────────────────────────────────────

    async def list_roles(self, db: AsyncSession, org_id: str, include_system: bool = True) -> List[RoleSchema]:
        conditions = [Role.deleted_at.is_(None)]
        if include_system:
            conditions.append(
                or_(Role.organization_id == org_id, Role.organization_id.is_(None))
            )
        else:
            conditions.append(Role.organization_id == org_id)

        result = await db.execute(select(Role).where(*conditions).order_by(Role.is_system.desc(), Role.name))
        roles = result.scalars().all()
        # Bulk fetch role-scoped resource grants
        role_ids = [r.id for r in roles]
        grants_by_role = await self._fetch_role_grants(db, role_ids)
        return [self._role_to_schema(r, grants_by_role.get(r.id, [])) for r in roles]

    async def create_role(self, db: AsyncSession, org_id: str, data: RoleCreate) -> RoleSchema:
        role = Role(
            organization_id=org_id,
            name=data.name,
            description=data.description,
            permissions=data.permissions,
            is_system=False,
        )
        db.add(role)
        await db.flush()
        await self._sync_role_grants(db, org_id, role.id, data.resource_grants or [])
        await db.commit()
        await db.refresh(role)
        grants = (await self._fetch_role_grants(db, [role.id])).get(role.id, [])
        return self._role_to_schema(role, grants)

    async def update_role(self, db: AsyncSession, org_id: str, role_id: str, data: RoleUpdate) -> RoleSchema:
        role = await self._get_role(db, org_id, role_id)
        if role.is_system:
            raise HTTPException(status_code=403, detail="Cannot modify system roles")

        # If removing full_admin_access, check lockout
        if data.permissions is not None:
            had_full_admin = isinstance(role.permissions, list) and FULL_ADMIN in role.permissions
            will_have_full_admin = FULL_ADMIN in data.permissions
            if had_full_admin and not will_have_full_admin:
                await assert_full_admin_exists(db, org_id, exclude_role_id=role_id)

        if data.name is not None:
            role.name = data.name
        if data.description is not None:
            role.description = data.description
        if data.permissions is not None:
            role.permissions = list(data.permissions)
            flag_modified(role, "permissions")

        if data.resource_grants is not None:
            await self._sync_role_grants(db, org_id, role.id, data.resource_grants)

        await db.commit()
        await db.refresh(role)
        grants = (await self._fetch_role_grants(db, [role.id])).get(role.id, [])
        return self._role_to_schema(role, grants)

    async def _sync_role_grants(
        self, db: AsyncSession, org_id: str, role_id: str,
        grants: List[RoleResourceGrantInput],
    ) -> None:
        """Replace all role-scoped resource grants for the given role."""
        # Hard-delete existing role grants for this role (idempotent replace)
        await db.execute(
            delete(ResourceGrant).where(
                ResourceGrant.organization_id == org_id,
                ResourceGrant.principal_type == "role",
                ResourceGrant.principal_id == role_id,
            )
        )
        for g in grants:
            db.add(ResourceGrant(
                organization_id=org_id,
                resource_type=g.resource_type,
                resource_id=g.resource_id,
                principal_type="role",
                principal_id=role_id,
                permissions=list(g.permissions or []),
            ))
        await db.flush()

    async def _fetch_role_grants(
        self, db: AsyncSession, role_ids: List[str],
    ) -> dict:
        """Return {role_id: [RoleResourceGrantOutput, ...]}."""
        if not role_ids:
            return {}
        result = await db.execute(
            select(ResourceGrant).where(
                ResourceGrant.principal_type == "role",
                ResourceGrant.principal_id.in_(role_ids),
                ResourceGrant.deleted_at.is_(None),
            )
        )
        out: dict = {}
        for g in result.scalars().all():
            out.setdefault(g.principal_id, []).append(RoleResourceGrantOutput(
                resource_type=g.resource_type,
                resource_id=g.resource_id,
                permissions=list(g.permissions or []),
            ))
        return out

    def _role_to_schema(self, role: Role, grants: List[RoleResourceGrantOutput]) -> RoleSchema:
        return RoleSchema(
            id=role.id,
            name=role.name,
            description=role.description,
            permissions=list(role.permissions or []),
            resource_grants=grants,
            organization_id=role.organization_id,
            is_system=role.is_system,
        )

    async def delete_role(self, db: AsyncSession, org_id: str, role_id: str) -> None:
        role = await self._get_role(db, org_id, role_id)
        if role.is_system:
            raise HTTPException(status_code=403, detail="Cannot delete system roles")

        # If role had full_admin_access, check lockout
        if isinstance(role.permissions, list) and FULL_ADMIN in role.permissions:
            await assert_full_admin_exists(db, org_id, exclude_role_id=role_id)

        # Drop role-scoped resource grants
        await db.execute(
            delete(ResourceGrant).where(
                ResourceGrant.principal_type == "role",
                ResourceGrant.principal_id == role_id,
            )
        )
        await db.delete(role)
        await db.commit()

    async def _get_role(self, db: AsyncSession, org_id: str, role_id: str) -> Role:
        result = await db.execute(
            select(Role).where(
                Role.id == role_id,
                or_(Role.organization_id == org_id, Role.organization_id.is_(None)),
                Role.deleted_at.is_(None),
            )
        )
        role = result.scalar_one_or_none()
        if not role:
            raise HTTPException(status_code=404, detail="Role not found")
        return role

    # ── Groups ───────────────────────────────────────────────────────────

    async def list_groups(self, db: AsyncSession, org_id: str) -> List[GroupSchema]:
        result = await db.execute(
            select(Group)
            .where(Group.organization_id == org_id, Group.deleted_at.is_(None))
            .order_by(Group.name)
        )
        groups = result.scalars().all()
        if not groups:
            return []

        group_ids = [g.id for g in groups]
        memberships_result = await db.execute(
            select(GroupMembership.group_id, GroupMembership.user_id)
            .where(
                GroupMembership.group_id.in_(group_ids),
                GroupMembership.deleted_at.is_(None),
            )
        )
        memberships_by_group: dict[str, list[str]] = {g.id: [] for g in groups}
        for group_id, user_id in memberships_result.all():
            memberships_by_group[group_id].append(user_id)

        schemas = []
        for g in groups:
            schema = GroupSchema.model_validate(g)
            schema.member_user_ids = memberships_by_group[g.id]
            schema.member_count = len(schema.member_user_ids)
            schemas.append(schema)
        return schemas

    async def create_group(self, db: AsyncSession, org_id: str, data: GroupCreate) -> GroupSchema:
        group = Group(
            organization_id=org_id,
            name=data.name,
            description=data.description,
        )
        db.add(group)
        await db.commit()
        await db.refresh(group)
        schema = GroupSchema.model_validate(group)
        schema.member_count = 0
        return schema

    async def update_group(self, db: AsyncSession, org_id: str, group_id: str, data: GroupUpdate) -> GroupSchema:
        group = await self._get_group(db, org_id, group_id)
        if data.name is not None:
            group.name = data.name
        if data.description is not None:
            group.description = data.description
        await db.commit()
        await db.refresh(group)
        return GroupSchema.model_validate(group)

    async def delete_group(self, db: AsyncSession, org_id: str, group_id: str) -> None:
        group = await self._get_group(db, org_id, group_id)
        await db.delete(group)
        await db.commit()

    async def list_group_members(self, db: AsyncSession, org_id: str, group_id: str) -> List[GroupMemberSchema]:
        await self._get_group(db, org_id, group_id)
        result = await db.execute(
            select(GroupMembership)
            .options(selectinload(GroupMembership.user))
            .where(
                GroupMembership.group_id == group_id,
                GroupMembership.deleted_at.is_(None),
            )
        )
        memberships = result.scalars().all()
        return [
            GroupMemberSchema(
                user_id=m.user_id,
                user_name=m.user.name if m.user else None,
                user_email=m.user.email if m.user else None,
            )
            for m in memberships
        ]

    async def add_group_member(self, db: AsyncSession, org_id: str, group_id: str, user_id: str) -> None:
        await self._get_group(db, org_id, group_id)
        # Verify user exists
        user = await db.execute(select(User).where(User.id == user_id))
        if not user.scalar_one_or_none():
            raise HTTPException(status_code=404, detail="User not found")

        existing = await db.execute(
            select(GroupMembership).where(
                GroupMembership.group_id == group_id,
                GroupMembership.user_id == user_id,
            )
        )
        if existing.scalar_one_or_none():
            raise HTTPException(status_code=409, detail="User is already a member of this group")

        gm = GroupMembership(group_id=group_id, user_id=user_id)
        db.add(gm)
        await db.commit()

    async def remove_group_member(self, db: AsyncSession, org_id: str, group_id: str, user_id: str) -> None:
        await self._get_group(db, org_id, group_id)
        result = await db.execute(
            select(GroupMembership).where(
                GroupMembership.group_id == group_id,
                GroupMembership.user_id == user_id,
            )
        )
        gm = result.scalar_one_or_none()
        if not gm:
            raise HTTPException(status_code=404, detail="Group membership not found")
        await db.delete(gm)
        await db.commit()

    async def _get_group(self, db: AsyncSession, org_id: str, group_id: str) -> Group:
        result = await db.execute(
            select(Group).where(
                Group.id == group_id,
                Group.organization_id == org_id,
                Group.deleted_at.is_(None),
            )
        )
        group = result.scalar_one_or_none()
        if not group:
            raise HTTPException(status_code=404, detail="Group not found")
        return group

    # ── Role Assignments ─────────────────────────────────────────────────

    async def list_role_assignments(
        self, db: AsyncSession, org_id: str,
        principal_type: Optional[str] = None,
        principal_id: Optional[str] = None,
    ) -> List[RoleAssignmentSchema]:
        stmt = (
            select(RoleAssignment)
            .options(selectinload(RoleAssignment.role))
            .where(
                RoleAssignment.organization_id == org_id,
                RoleAssignment.deleted_at.is_(None),
            )
        )
        if principal_type:
            stmt = stmt.where(RoleAssignment.principal_type == principal_type)
        if principal_id:
            stmt = stmt.where(RoleAssignment.principal_id == principal_id)

        result = await db.execute(stmt)
        assignments = result.scalars().all()
        return [
            RoleAssignmentSchema(
                id=a.id,
                organization_id=a.organization_id,
                role_id=a.role_id,
                principal_type=a.principal_type,
                principal_id=a.principal_id,
                role=RoleSchema.model_validate(a.role) if a.role else None,
            )
            for a in assignments
        ]

    async def create_role_assignment(
        self, db: AsyncSession, org_id: str, data: RoleAssignmentCreate
    ) -> RoleAssignmentSchema:
        # Verify role exists
        await self._get_role(db, org_id, data.role_id)

        # Check for duplicate
        existing = await db.execute(
            select(RoleAssignment).where(
                RoleAssignment.organization_id == org_id,
                RoleAssignment.role_id == data.role_id,
                RoleAssignment.principal_type == data.principal_type,
                RoleAssignment.principal_id == data.principal_id,
                RoleAssignment.deleted_at.is_(None),
            )
        )
        if existing.scalar_one_or_none():
            raise HTTPException(status_code=409, detail="Role assignment already exists")

        assignment = RoleAssignment(
            organization_id=org_id,
            role_id=data.role_id,
            principal_type=data.principal_type,
            principal_id=data.principal_id,
        )
        db.add(assignment)
        await db.commit()

        # Re-fetch with eagerly loaded role to avoid lazy-load issues
        result = await db.execute(
            select(RoleAssignment)
            .options(selectinload(RoleAssignment.role))
            .where(RoleAssignment.id == assignment.id)
        )
        assignment = result.scalar_one()
        role = assignment.role
        return RoleAssignmentSchema(
            id=assignment.id,
            organization_id=assignment.organization_id,
            role_id=assignment.role_id,
            principal_type=assignment.principal_type,
            principal_id=assignment.principal_id,
            role=RoleSchema.model_validate(role) if role else None,
        )

    async def delete_role_assignment(self, db: AsyncSession, org_id: str, assignment_id: str) -> None:
        result = await db.execute(
            select(RoleAssignment)
            .options(selectinload(RoleAssignment.role))
            .where(
                RoleAssignment.id == assignment_id,
                RoleAssignment.organization_id == org_id,
                RoleAssignment.deleted_at.is_(None),
            )
        )
        assignment = result.scalar_one_or_none()
        if not assignment:
            raise HTTPException(status_code=404, detail="Role assignment not found")

        # Check lockout if removing a role with full_admin_access from a direct user
        if assignment.principal_type == "user" and assignment.role:
            perms = assignment.role.permissions or []
            if FULL_ADMIN in perms:
                await assert_full_admin_exists(db, org_id, exclude_user_id=assignment.principal_id)

        await db.delete(assignment)
        await db.commit()

    # ── Resource Grants ──────────────────────────────────────────────────

    async def list_resource_grants(
        self, db: AsyncSession, org_id: str,
        resource_type: Optional[str] = None,
        resource_id: Optional[str] = None,
        principal_type: Optional[str] = None,
        principal_id: Optional[str] = None,
    ) -> List[ResourceGrantSchema]:
        stmt = select(ResourceGrant).where(
            ResourceGrant.organization_id == org_id,
            ResourceGrant.deleted_at.is_(None),
        )
        if resource_type:
            stmt = stmt.where(ResourceGrant.resource_type == resource_type)
        if resource_id:
            stmt = stmt.where(ResourceGrant.resource_id == resource_id)
        if principal_type:
            stmt = stmt.where(ResourceGrant.principal_type == principal_type)
        if principal_id:
            stmt = stmt.where(ResourceGrant.principal_id == principal_id)

        result = await db.execute(stmt)
        grants = result.scalars().all()
        return [ResourceGrantSchema.model_validate(g) for g in grants]

    async def create_resource_grant(
        self, db: AsyncSession, org_id: str, data: ResourceGrantCreate
    ) -> ResourceGrantSchema:
        # Check for duplicate
        existing = await db.execute(
            select(ResourceGrant).where(
                ResourceGrant.organization_id == org_id,
                ResourceGrant.resource_type == data.resource_type,
                ResourceGrant.resource_id == data.resource_id,
                ResourceGrant.principal_type == data.principal_type,
                ResourceGrant.principal_id == data.principal_id,
                ResourceGrant.deleted_at.is_(None),
            )
        )
        if existing.scalar_one_or_none():
            raise HTTPException(status_code=409, detail="Resource grant already exists")

        grant = ResourceGrant(
            organization_id=org_id,
            resource_type=data.resource_type,
            resource_id=data.resource_id,
            principal_type=data.principal_type,
            principal_id=data.principal_id,
            permissions=data.permissions,
        )
        db.add(grant)
        await db.commit()
        await db.refresh(grant)
        return ResourceGrantSchema.model_validate(grant)

    async def update_resource_grant(
        self, db: AsyncSession, org_id: str, grant_id: str, data: ResourceGrantUpdate
    ) -> ResourceGrantSchema:
        result = await db.execute(
            select(ResourceGrant).where(
                ResourceGrant.id == grant_id,
                ResourceGrant.organization_id == org_id,
                ResourceGrant.deleted_at.is_(None),
            )
        )
        grant = result.scalar_one_or_none()
        if not grant:
            raise HTTPException(status_code=404, detail="Resource grant not found")

        grant.permissions = data.permissions
        await db.commit()
        await db.refresh(grant)
        return ResourceGrantSchema.model_validate(grant)

    async def delete_resource_grant(self, db: AsyncSession, org_id: str, grant_id: str) -> None:
        result = await db.execute(
            select(ResourceGrant).where(
                ResourceGrant.id == grant_id,
                ResourceGrant.organization_id == org_id,
                ResourceGrant.deleted_at.is_(None),
            )
        )
        grant = result.scalar_one_or_none()
        if not grant:
            raise HTTPException(status_code=404, detail="Resource grant not found")

        await db.delete(grant)
        await db.commit()


rbac_service = RBACService()
