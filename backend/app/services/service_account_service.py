import uuid
import logging
from datetime import datetime
from typing import List, Optional

from fastapi import HTTPException
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi_users.password import PasswordHelper

from app.models.user import User
from app.models.organization import Organization
from app.models.service_account import ServiceAccount
from app.models.role import Role
from app.models.role_assignment import RoleAssignment
from app.models.api_key import ApiKey
from app.schemas.api_key_schema import ApiKeyCreate, ApiKeyResponse, ApiKeyCreated
from app.schemas.service_account_schema import (
    ServiceAccountCreate, ServiceAccountUpdate, ServiceAccountResponse,
    ServiceAccountDetail, ServiceAccountRoleSummary, ServiceAccountKeyCreate,
)
from app.services.api_key_service import ApiKeyService
from app.core.permission_resolver import resolve_permissions, FULL_ADMIN

logger = logging.getLogger(__name__)


class ServiceAccountService:
    """CRUD for service accounts — non-human, org-managed API principals.

    Each service account is backed by a hidden ``users`` row
    (``is_service_account=True``, ``is_active=False``) so all existing
    ``user_id`` foreign keys, ownership checks, and the RBAC resolver work
    unchanged. Org binding and metadata live on the ``service_accounts`` row,
    so the account consumes no seat and never appears in member lists.
    """

    def __init__(self):
        self.api_key_service = ApiKeyService()
        self._password_helper = PasswordHelper()

    # ── helpers ──────────────────────────────────────────────────────────

    def _synthetic_email(self, sa_id: str) -> str:
        # Non-routable, unique, RFC-valid. Never receives mail; excluded from
        # SSO/LDAP/SCIM adoption (outside any allowed signup domain).
        return f"svc.{sa_id}@service.invalid"

    async def _get_role_or_404(self, db: AsyncSession, org_id: str, role_id: str) -> Role:
        from sqlalchemy import or_, and_
        result = await db.execute(
            select(Role).where(
                Role.id == role_id,
                Role.deleted_at.is_(None),
                or_(
                    and_(Role.is_system == True, Role.organization_id.is_(None)),
                    Role.organization_id == org_id,
                ),
            )
        )
        role = result.scalar_one_or_none()
        if not role:
            raise HTTPException(status_code=404, detail="Role not found")
        return role

    async def _default_member_role(self, db: AsyncSession, org_id: str) -> Role:
        return await self._resolve_system_role(db, "member")

    async def _resolve_system_role(self, db: AsyncSession, name: str) -> Role:
        result = await db.execute(
            select(Role).where(
                Role.name == name,
                Role.is_system == True,
                Role.organization_id.is_(None),
                Role.deleted_at.is_(None),
            )
        )
        role = result.scalar_one_or_none()
        if not role:
            raise HTTPException(status_code=500, detail=f"System role '{name}' missing")
        return role

    async def _assert_creator_can_grant(self, db: AsyncSession, creator: User, org_id: str, role: Role) -> None:
        """A creator may only assign a role whose permissions their own
        permissions cover — you cannot mint an account more powerful than
        yourself."""
        resolved = await resolve_permissions(db, str(creator.id), str(org_id))
        if FULL_ADMIN in resolved.org_permissions:
            return  # full admins can assign anything
        role_perms = set(role.permissions or [])
        if FULL_ADMIN in role_perms:
            raise HTTPException(status_code=403, detail="Only a full admin can create a full-admin service account")
        if not role_perms.issubset(resolved.org_permissions):
            missing = sorted(role_perms - resolved.org_permissions)
            raise HTTPException(
                status_code=403,
                detail=f"You cannot grant permissions you do not hold: {', '.join(missing)}",
            )

    async def _roles_for(self, db: AsyncSession, org_id: str, user_id: str) -> List[ServiceAccountRoleSummary]:
        result = await db.execute(
            select(Role.id, Role.name)
            .join(RoleAssignment, RoleAssignment.role_id == Role.id)
            .where(
                RoleAssignment.organization_id == org_id,
                RoleAssignment.principal_type == "user",
                RoleAssignment.principal_id == user_id,
                RoleAssignment.deleted_at.is_(None),
                Role.deleted_at.is_(None),
            )
        )
        return [ServiceAccountRoleSummary(id=r.id, name=r.name) for r in result.all()]

    async def _key_stats(self, db: AsyncSession, sa_id: str) -> tuple[int, Optional[datetime]]:
        result = await db.execute(
            select(func.count(ApiKey.id), func.max(ApiKey.last_used_at)).where(
                ApiKey.service_account_id == sa_id,
                ApiKey.deleted_at.is_(None),
            )
        )
        count, last_used = result.one()
        return int(count or 0), last_used

    async def _to_response(self, db: AsyncSession, sa: ServiceAccount) -> ServiceAccountResponse:
        roles = await self._roles_for(db, sa.organization_id, sa.user_id)
        count, last_used = await self._key_stats(db, sa.id)
        return ServiceAccountResponse(
            id=sa.id,
            name=sa.name,
            description=sa.description,
            disabled=sa.disabled_at is not None,
            created_at=sa.created_at,
            created_by_user_id=sa.created_by_user_id,
            roles=roles,
            key_count=count,
            last_used_at=last_used,
        )

    async def _get_sa_or_404(self, db: AsyncSession, org_id: str, sa_id: str) -> ServiceAccount:
        result = await db.execute(
            select(ServiceAccount).where(
                ServiceAccount.id == sa_id,
                ServiceAccount.organization_id == org_id,
                ServiceAccount.deleted_at.is_(None),
            )
        )
        sa = result.scalar_one_or_none()
        if not sa:
            raise HTTPException(status_code=404, detail="Service account not found")
        return sa

    # ── CRUD ─────────────────────────────────────────────────────────────

    async def list_service_accounts(self, db: AsyncSession, org: Organization) -> List[ServiceAccountResponse]:
        result = await db.execute(
            select(ServiceAccount)
            .where(
                ServiceAccount.organization_id == org.id,
                ServiceAccount.deleted_at.is_(None),
            )
            .order_by(ServiceAccount.created_at.desc())
        )
        return [await self._to_response(db, sa) for sa in result.scalars().all()]

    async def create_service_account(
        self, db: AsyncSession, data: ServiceAccountCreate, creator: User, org: Organization,
    ) -> ServiceAccountResponse:
        if data.role_id:
            role = await self._get_role_or_404(db, org.id, data.role_id)
        else:
            role = await self._default_member_role(db, org.id)
        await self._assert_creator_can_grant(db, creator, org.id, role)

        # 1. Backing users row — cannot log in (is_active=False), API-key auth
        #    bypasses that check.
        user_id = str(uuid.uuid4())
        backing = User(
            id=user_id,
            email=self._synthetic_email(user_id),
            name=data.name,
            hashed_password=self._password_helper.hash(self._password_helper.generate()),
            is_active=False,
            is_verified=True,
            is_superuser=False,
            is_service_account=True,
        )
        db.add(backing)
        await db.flush()

        # 2. The service account row (org binding + metadata).
        sa = ServiceAccount(
            organization_id=org.id,
            user_id=user_id,
            name=data.name,
            description=data.description,
            created_by_user_id=str(creator.id),
        )
        db.add(sa)
        await db.flush()

        # 3. RBAC role assignment (principal_type="user" on the backing row).
        db.add(RoleAssignment(
            organization_id=org.id,
            role_id=role.id,
            principal_type="user",
            principal_id=user_id,
        ))
        await db.commit()
        await db.refresh(sa)
        return await self._to_response(db, sa)

    async def get_service_account(self, db: AsyncSession, org: Organization, sa_id: str) -> ServiceAccountDetail:
        sa = await self._get_sa_or_404(db, org.id, sa_id)
        base = await self._to_response(db, sa)
        keys_result = await db.execute(
            select(ApiKey)
            .where(ApiKey.service_account_id == sa.id, ApiKey.deleted_at.is_(None))
            .order_by(ApiKey.created_at.desc())
        )
        keys = [ApiKeyResponse.model_validate(k) for k in keys_result.scalars().all()]
        return ServiceAccountDetail(**base.model_dump(), keys=keys)

    async def update_service_account(
        self, db: AsyncSession, org: Organization, sa_id: str, data: ServiceAccountUpdate, actor: User,
    ) -> ServiceAccountResponse:
        sa = await self._get_sa_or_404(db, org.id, sa_id)

        if data.name is not None:
            sa.name = data.name
            # keep the backing user's display name in sync (used in attribution)
            backing = await db.get(User, sa.user_id)
            if backing:
                backing.name = data.name
        if data.description is not None:
            sa.description = data.description
        if data.disabled is not None:
            sa.disabled_at = datetime.utcnow() if data.disabled else None

        if data.role_id is not None:
            role = await self._get_role_or_404(db, org.id, data.role_id)
            await self._assert_creator_can_grant(db, actor, org.id, role)
            # Replace all role assignments for this principal with the new one.
            existing = await db.execute(
                select(RoleAssignment).where(
                    RoleAssignment.organization_id == org.id,
                    RoleAssignment.principal_type == "user",
                    RoleAssignment.principal_id == sa.user_id,
                    RoleAssignment.deleted_at.is_(None),
                )
            )
            for ra in existing.scalars().all():
                await db.delete(ra)
            db.add(RoleAssignment(
                organization_id=org.id,
                role_id=role.id,
                principal_type="user",
                principal_id=sa.user_id,
            ))

        await db.commit()
        await db.refresh(sa)
        return await self._to_response(db, sa)

    async def delete_service_account(self, db: AsyncSession, org: Organization, sa_id: str) -> None:
        sa = await self._get_sa_or_404(db, org.id, sa_id)
        now = datetime.utcnow()
        # Soft-disable + soft-delete the account and revoke all its keys; the
        # backing users row is kept (is_active=False already) so attribution on
        # objects it created is preserved.
        sa.disabled_at = sa.disabled_at or now
        sa.deleted_at = now
        keys = await db.execute(
            select(ApiKey).where(ApiKey.service_account_id == sa.id, ApiKey.deleted_at.is_(None))
        )
        for k in keys.scalars().all():
            k.deleted_at = now
        await db.commit()

    # ── Keys ─────────────────────────────────────────────────────────────

    async def create_key(
        self, db: AsyncSession, org: Organization, sa_id: str, data: ServiceAccountKeyCreate,
    ) -> ApiKeyCreated:
        sa = await self._get_sa_or_404(db, org.id, sa_id)
        backing = await db.get(User, sa.user_id)
        return await self.api_key_service.create_api_key(
            db,
            ApiKeyCreate(name=data.name, expires_at=data.expires_at),
            backing,
            org,
            service_account_id=sa.id,
        )

    async def revoke_key(self, db: AsyncSession, org: Organization, sa_id: str, key_id: str) -> None:
        sa = await self._get_sa_or_404(db, org.id, sa_id)
        result = await db.execute(
            select(ApiKey).where(
                ApiKey.id == key_id,
                ApiKey.service_account_id == sa.id,
                ApiKey.deleted_at.is_(None),
            )
        )
        key = result.scalar_one_or_none()
        if not key:
            raise HTTPException(status_code=404, detail="API key not found")
        key.deleted_at = datetime.utcnow()
        await db.commit()
