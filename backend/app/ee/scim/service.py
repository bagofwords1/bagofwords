# SCIM Services
# Licensed under the Business Source License 1.1
# See ENTERPRISE_LICENSE for details

import re
import secrets
import hashlib
import logging
from typing import Optional, List
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from fastapi import HTTPException
from fastapi_users.password import PasswordHelper

from app.models.user import User
from app.models.membership import Membership
from app.models.organization import Organization
from app.ee.scim.models import ScimToken
from app.ee.scim.schemas import (
    ScimUser, ScimUserCreate, ScimPatchOp, ScimMeta, ScimName, ScimEmail,
    ScimListResponse, ScimTokenCreate, ScimTokenResponse, ScimTokenCreated,
)

logger = logging.getLogger(__name__)
password_helper = PasswordHelper()


# --- SCIM Token Service ---

class ScimTokenService:

    @staticmethod
    def _generate_token() -> tuple[str, str, str]:
        """Generate a SCIM token. Returns (full_token, token_hash, token_prefix)."""
        random_bytes = secrets.token_urlsafe(32)
        full_token = f"bow_scim_{random_bytes}"
        token_hash = hashlib.sha256(full_token.encode()).hexdigest()
        token_prefix = full_token[:16]
        return full_token, token_hash, token_prefix

    async def create_token(
        self,
        db: AsyncSession,
        organization: Organization,
        user_id: str,
        data: ScimTokenCreate,
    ) -> ScimTokenCreated:
        full_token, token_hash, token_prefix = self._generate_token()

        scim_token = ScimToken(
            organization_id=organization.id,
            name=data.name,
            token_hash=token_hash,
            token_prefix=token_prefix,
            created_by_user_id=user_id,
            expires_at=data.expires_at,
        )
        db.add(scim_token)
        await db.commit()
        await db.refresh(scim_token)

        return ScimTokenCreated(
            id=scim_token.id,
            name=scim_token.name,
            token_prefix=scim_token.token_prefix,
            created_at=scim_token.created_at,
            expires_at=scim_token.expires_at,
            last_used_at=scim_token.last_used_at,
            token=full_token,
        )

    async def list_tokens(
        self,
        db: AsyncSession,
        organization_id: str,
    ) -> List[ScimTokenResponse]:
        result = await db.execute(
            select(ScimToken)
            .where(ScimToken.organization_id == organization_id)
            .where(ScimToken.deleted_at.is_(None))
            .order_by(ScimToken.created_at.desc())
        )
        tokens = result.scalars().all()
        return [ScimTokenResponse.model_validate(t) for t in tokens]

    async def revoke_token(
        self,
        db: AsyncSession,
        organization_id: str,
        token_id: str,
    ) -> bool:
        result = await db.execute(
            select(ScimToken)
            .where(ScimToken.id == token_id)
            .where(ScimToken.organization_id == organization_id)
            .where(ScimToken.deleted_at.is_(None))
        )
        token = result.scalar_one_or_none()
        if not token:
            raise HTTPException(status_code=404, detail="SCIM token not found")

        token.deleted_at = datetime.utcnow()
        await db.commit()
        return True


# --- SCIM User Service ---

def _parse_scim_filter(filter_str: Optional[str]) -> dict:
    """
    Parse minimal SCIM filter expressions.
    Supports: 'userName eq "value"' and 'emails.value eq "value"'
    """
    if not filter_str:
        return {}

    match = re.match(r'(\S+)\s+eq\s+"([^"]*)"', filter_str.strip())
    if not match:
        return {}

    attr, value = match.group(1), match.group(2)
    return {attr: value}


def _user_to_scim(user: User, membership: Optional[Membership] = None, base_url: str = "") -> ScimUser:
    """Convert internal User to SCIM User representation."""
    name_parts = (user.name or "").split(" ", 1)
    given_name = name_parts[0] if name_parts else ""
    family_name = name_parts[1] if len(name_parts) > 1 else ""

    created_at = membership.created_at if membership else None
    updated_at = membership.updated_at if membership else None

    return ScimUser(
        id=user.id,
        externalId=user.scim_external_id,
        userName=user.email,
        name=ScimName(
            formatted=user.name,
            givenName=given_name,
            familyName=family_name,
        ),
        displayName=user.name,
        emails=[ScimEmail(value=user.email, type="work", primary=True)],
        active=user.is_active,
        meta=ScimMeta(
            resourceType="User",
            created=created_at,
            lastModified=updated_at,
            location=f"{base_url}/scim/v2/Users/{user.id}",
        ),
    )


class ScimUserService:

    async def list_users(
        self,
        db: AsyncSession,
        organization_id: str,
        filter_str: Optional[str] = None,
        start_index: int = 1,
        count: int = 100,
        base_url: str = "",
    ) -> ScimListResponse:
        filters = _parse_scim_filter(filter_str)

        query = (
            select(User, Membership)
            .join(Membership, Membership.user_id == User.id)
            .where(Membership.organization_id == organization_id)
            .where(Membership.deleted_at.is_(None))
        )

        # Apply SCIM filters
        email_filter = filters.get("userName") or filters.get("emails.value")
        if email_filter:
            query = query.where(User.email == email_filter)

        external_id_filter = filters.get("externalId")
        if external_id_filter:
            query = query.where(User.scim_external_id == external_id_filter)

        # Get total count
        count_query = select(func.count()).select_from(query.subquery())
        total_result = await db.execute(count_query)
        total = total_result.scalar()

        # Apply pagination (SCIM uses 1-based indexing)
        offset = max(0, start_index - 1)
        query = query.offset(offset).limit(count)

        result = await db.execute(query)
        rows = result.all()

        resources = [_user_to_scim(user, membership, base_url) for user, membership in rows]

        return ScimListResponse(
            totalResults=total,
            startIndex=start_index,
            itemsPerPage=count,
            Resources=resources,
        )

    async def get_user(
        self,
        db: AsyncSession,
        organization_id: str,
        user_id: str,
        base_url: str = "",
    ) -> ScimUser:
        result = await db.execute(
            select(User, Membership)
            .join(Membership, Membership.user_id == User.id)
            .where(User.id == user_id)
            .where(Membership.organization_id == organization_id)
            .where(Membership.deleted_at.is_(None))
        )
        row = result.one_or_none()
        if not row:
            raise HTTPException(status_code=404, detail="User not found")

        user, membership = row
        return _user_to_scim(user, membership, base_url)

    async def create_user(
        self,
        db: AsyncSession,
        organization_id: str,
        data: ScimUserCreate,
        base_url: str = "",
    ) -> ScimUser:
        email = data.userName
        if data.emails:
            email = data.emails[0].value

        # Check if user already exists
        existing_result = await db.execute(
            select(User).where(User.email == email)
        )
        existing_user = existing_result.scalar_one_or_none()

        if existing_user:
            # Check if already a member of this org
            membership_result = await db.execute(
                select(Membership)
                .where(Membership.user_id == existing_user.id)
                .where(Membership.organization_id == organization_id)
                .where(Membership.deleted_at.is_(None))
            )
            if membership_result.scalar_one_or_none():
                raise HTTPException(status_code=409, detail="User already exists in this organization")

            # Add membership to existing user
            membership = Membership(
                user_id=existing_user.id,
                organization_id=organization_id,
                email=existing_user.email,
                role="member",
            )
            db.add(membership)

            # Update external ID if provided
            if data.externalId:
                existing_user.scim_external_id = data.externalId

            await db.commit()
            await db.refresh(membership)
            return _user_to_scim(existing_user, membership, base_url)

        # Create new user
        display_name = data.displayName
        if not display_name and data.name:
            if data.name.formatted:
                display_name = data.name.formatted
            elif data.name.givenName or data.name.familyName:
                display_name = f"{data.name.givenName or ''} {data.name.familyName or ''}".strip()
        if not display_name:
            display_name = email.split("@")[0]

        user = User(
            email=email,
            name=display_name,
            hashed_password=password_helper.hash(password_helper.generate()),
            is_active=data.active,
            is_verified=True,
            is_superuser=False,
            scim_external_id=data.externalId,
        )
        db.add(user)
        await db.flush()  # Get user.id before creating membership

        membership = Membership(
            user_id=user.id,
            organization_id=organization_id,
            email=user.email,
            role="member",
        )
        db.add(membership)
        await db.commit()
        await db.refresh(user)
        await db.refresh(membership)

        return _user_to_scim(user, membership, base_url)

    async def update_user(
        self,
        db: AsyncSession,
        organization_id: str,
        user_id: str,
        data: ScimUserCreate,
        base_url: str = "",
    ) -> ScimUser:
        """PUT - full replace of user attributes."""
        result = await db.execute(
            select(User, Membership)
            .join(Membership, Membership.user_id == User.id)
            .where(User.id == user_id)
            .where(Membership.organization_id == organization_id)
            .where(Membership.deleted_at.is_(None))
        )
        row = result.one_or_none()
        if not row:
            raise HTTPException(status_code=404, detail="User not found")

        user, membership = row

        # Update name
        if data.displayName:
            user.name = data.displayName
        elif data.name:
            if data.name.formatted:
                user.name = data.name.formatted
            elif data.name.givenName or data.name.familyName:
                user.name = f"{data.name.givenName or ''} {data.name.familyName or ''}".strip()

        # Update email
        if data.emails:
            user.email = data.emails[0].value
        elif data.userName:
            user.email = data.userName

        # Update active status
        user.is_active = data.active

        # Update external ID
        if data.externalId is not None:
            user.scim_external_id = data.externalId

        await db.commit()
        await db.refresh(user)
        return _user_to_scim(user, membership, base_url)

    async def patch_user(
        self,
        db: AsyncSession,
        organization_id: str,
        user_id: str,
        patch: ScimPatchOp,
        base_url: str = "",
    ) -> ScimUser:
        """PATCH - partial update of user attributes."""
        result = await db.execute(
            select(User, Membership)
            .join(Membership, Membership.user_id == User.id)
            .where(User.id == user_id)
            .where(Membership.organization_id == organization_id)
            .where(Membership.deleted_at.is_(None))
        )
        row = result.one_or_none()
        if not row:
            raise HTTPException(status_code=404, detail="User not found")

        user, membership = row

        for op in patch.Operations:
            if op.op.lower() == "replace":
                self._apply_replace(user, op.path, op.value)
            elif op.op.lower() == "add":
                self._apply_replace(user, op.path, op.value)  # add behaves like replace for single-valued attrs
            elif op.op.lower() == "remove":
                if op.path and op.path.lower() == "externalid":
                    user.scim_external_id = None

        await db.commit()
        await db.refresh(user)
        return _user_to_scim(user, membership, base_url)

    def _apply_replace(self, user: User, path: Optional[str], value) -> None:
        """Apply a replace operation to user attributes."""
        if path is None and isinstance(value, dict):
            # Okta-style: {"op": "replace", "value": {"active": false}}
            for key, val in value.items():
                self._set_user_attr(user, key, val)
        elif path:
            self._set_user_attr(user, path, value)

    def _set_user_attr(self, user: User, attr: str, value) -> None:
        """Set a single user attribute from SCIM path/key."""
        attr_lower = attr.lower()
        if attr_lower == "active":
            user.is_active = bool(value)
        elif attr_lower == "username":
            user.email = str(value)
        elif attr_lower == "displayname":
            user.name = str(value)
        elif attr_lower == "externalid":
            user.scim_external_id = str(value) if value is not None else None
        elif attr_lower == "name":
            if isinstance(value, dict):
                formatted = value.get("formatted")
                given = value.get("givenName", "")
                family = value.get("familyName", "")
                user.name = formatted or f"{given} {family}".strip()
        elif attr_lower == "emails":
            if isinstance(value, list) and value:
                email_val = value[0].get("value") if isinstance(value[0], dict) else str(value[0])
                if email_val:
                    user.email = email_val

    async def delete_user(
        self,
        db: AsyncSession,
        organization_id: str,
        user_id: str,
    ) -> None:
        """SCIM DELETE - deactivate user (soft delete)."""
        result = await db.execute(
            select(User, Membership)
            .join(Membership, Membership.user_id == User.id)
            .where(User.id == user_id)
            .where(Membership.organization_id == organization_id)
            .where(Membership.deleted_at.is_(None))
        )
        row = result.one_or_none()
        if not row:
            raise HTTPException(status_code=404, detail="User not found")

        user, membership = row
        user.is_active = False
        await db.commit()
