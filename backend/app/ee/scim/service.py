# SCIM Services
# Licensed under the BOW Enterprise License
# See backend/app/ee/LICENSE for details

import re
import secrets
import hashlib
import logging
from typing import Optional, List
from datetime import datetime, timedelta
from types import SimpleNamespace

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, literal, union_all
from sqlalchemy.exc import IntegrityError
from fastapi import HTTPException
from fastapi_users.password import PasswordHelper

from app.models.user import User
from app.models.membership import Membership
from app.models.organization import Organization
from app.ee.scim.models import ScimToken
from app.ee.scim.errors import ScimError
from app.settings.config import settings
from app.core.user_lifecycle import (
    has_live_membership_elsewhere,
    is_instance_admin_account,
    may_attach_existing_account,
)
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


def _scim_bool(value) -> bool:
    """SCIM boolean from a PATCH value.

    Entra apps without the `aadOptscim062020` flag send booleans as strings
    ("False"), and bool("False") is True — which would turn a deprovisioning
    PATCH into a no-op that leaves the user able to sign in.
    """
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("true", "1"):
            return True
        if lowered in ("false", "0"):
            return False
        raise HTTPException(status_code=400, detail=f"Invalid boolean value: {value!r}")
    return bool(value)


def _pending_resources_enabled() -> bool:
    """Whether an open invite for an existing account is shown as a SCIM
    resource. Only where the instance hosts multiple organizations; on a
    single-organization deployment list/get see members only, as before."""
    return bool(settings.bow_config.features.allow_multiple_organizations)


def _pending_to_scim(user: User, membership: Membership, base_url: str = "") -> ScimUser:
    """SCIM representation of a person this organization has provisioned but
    who has not joined yet (an open invite for an existing account).

    Built from what this organization supplied — the address — and not from
    the account's profile, which belongs to the organizations the person is
    already in.
    """
    return ScimUser(
        id=user.id,
        externalId=None,
        userName=membership.email or user.email,
        name=ScimName(formatted=None, givenName="", familyName=""),
        displayName=None,
        emails=[ScimEmail(value=membership.email or user.email, type="work", primary=True)],
        active=True,
        meta=ScimMeta(
            resourceType="User",
            created=membership.created_at,
            lastModified=membership.updated_at,
            location=f"{base_url}/scim/v2/Users/{user.id}",
        ),
    )


def _user_to_scim(
    user: User,
    membership: Optional[Membership] = None,
    base_url: str = "",
    active: Optional[bool] = None,
) -> ScimUser:
    """Convert internal User to SCIM User representation.

    ``active`` overrides the account flag when the answer is about this
    organization's membership rather than the account (see ScimUserService).
    """
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
        active=user.is_active if active is None else active,
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
        email_filter = filters.get("userName") or filters.get("emails.value")
        external_id_filter = filters.get("externalId")

        live = (
            select(User.id.label("user_id"), Membership.id.label("membership_id"), literal(0).label("pending"))
            .join(Membership, Membership.user_id == User.id)
            .where(Membership.organization_id == organization_id)
            .where(Membership.deleted_at.is_(None))
        )
        if email_filter:
            live = live.where(User.email == email_filter)
        if external_id_filter:
            live = live.where(User.scim_external_id == external_id_filter)

        # People provisioned here who have not joined yet (see create_user):
        # an open invite whose address is an existing account. Listed so the
        # identity provider finds the resource it created. Never matched by
        # externalId, which is the account's, not this organization's.
        # Multi-organization deployments only: with a single organization the
        # listing is exactly what it always was.
        if external_id_filter or not _pending_resources_enabled():
            keys = live.subquery()
        else:
            pending = (
                select(User.id.label("user_id"), Membership.id.label("membership_id"), literal(1).label("pending"))
                .join(Membership, func.lower(Membership.email) == func.lower(User.email))
                .where(Membership.organization_id == organization_id)
                .where(Membership.user_id.is_(None))
                .where(Membership.deleted_at.is_(None))
            )
            if email_filter:
                pending = pending.where(User.email == email_filter)
            keys = union_all(live, pending).subquery()

        # Get total count
        total_result = await db.execute(select(func.count()).select_from(keys))
        total = total_result.scalar()

        # Apply pagination (SCIM uses 1-based indexing)
        offset = max(0, start_index - 1)
        page = (await db.execute(
            select(keys.c.user_id, keys.c.membership_id, keys.c.pending)
            .order_by(keys.c.pending, keys.c.user_id, keys.c.membership_id)
            .offset(offset).limit(count)
        )).all()

        users = {
            u.id: u for u in (await db.execute(
                select(User).where(User.id.in_([r.user_id for r in page]))
            )).scalars().all()
        } if page else {}
        memberships = {
            m.id: m for m in (await db.execute(
                select(Membership).where(Membership.id.in_([r.membership_id for r in page]))
            )).scalars().all()
        } if page else {}

        resources = []
        for r in page:
            user, membership = users.get(r.user_id), memberships.get(r.membership_id)
            if user is None or membership is None:
                continue
            if r.pending:
                resources.append(_pending_to_scim(user, membership, base_url))
            else:
                resources.append(_user_to_scim(user, membership, base_url))

        return ScimListResponse(
            totalResults=total,
            startIndex=start_index,
            itemsPerPage=count,
            Resources=resources,
        )

    async def _load(self, db: AsyncSession, organization_id: str, user_id: str):
        """Resolve a SCIM user id inside this organization.

        Returns ``(user, membership, pending)``. ``pending`` is True when the
        person has only an open invite here (see create_user). 404 when the
        user has neither a membership nor an invite in this organization.
        """
        result = await db.execute(
            select(User, Membership)
            .join(Membership, Membership.user_id == User.id)
            .where(User.id == user_id)
            .where(Membership.organization_id == organization_id)
            .where(Membership.deleted_at.is_(None))
            .limit(1)
        )
        row = result.first()
        if row:
            return row[0], row[1], False
        if not _pending_resources_enabled():
            raise HTTPException(status_code=404, detail="User not found")

        result = await db.execute(
            select(User, Membership)
            .join(Membership, func.lower(Membership.email) == func.lower(User.email))
            .where(User.id == user_id)
            .where(Membership.organization_id == organization_id)
            .where(Membership.user_id.is_(None))
            .where(Membership.deleted_at.is_(None))
            .order_by(Membership.created_at.asc(), Membership.id.asc())
            .limit(1)
        )
        row = result.first()
        if row:
            return row[0], row[1], True
        raise HTTPException(status_code=404, detail="User not found")

    async def _is_shared(self, db: AsyncSession, organization_id: str, user: User, pending: bool) -> bool:
        """Whether the account also belongs outside this organization.

        The account row (email, name, active flag, externalId) is shared by
        every organization the person is in. This organization's identity
        provider may write it only while this is the person's sole
        organization; otherwise its writes are limited to the membership here.
        """
        if pending or is_instance_admin_account(user):
            return True
        return await has_live_membership_elsewhere(db, str(user.id), organization_id)

    async def _end_membership(self, db: AsyncSession, organization_id: str, membership: Membership) -> None:
        """Remove this organization's membership (or open invite) through the
        same path as the members list, so the same cleanup runs."""
        from app.services.organization_service import OrganizationService

        await OrganizationService().remove_member(
            db, organization_id, str(membership.id), None, None
        )

    async def _apply_shared(
        self,
        db: AsyncSession,
        organization_id: str,
        user: User,
        membership: Membership,
        pending: bool,
        active: bool,
        base_url: str,
    ) -> ScimUser:
        """PUT/PATCH for an account that also belongs elsewhere: profile fields
        are left as stored, and ``active`` decides only whether the person
        stays in this organization."""
        if not active:
            await self._end_membership(db, organization_id, membership)
            return _user_to_scim(user, None, base_url, active=False)
        if pending:
            return _pending_to_scim(user, membership, base_url)
        return _user_to_scim(user, membership, base_url)

    async def get_user(
        self,
        db: AsyncSession,
        organization_id: str,
        user_id: str,
        base_url: str = "",
    ) -> ScimUser:
        user, membership, pending = await self._load(db, organization_id, user_id)
        if pending:
            return _pending_to_scim(user, membership, base_url)
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
        # Case-insensitive: a case variant of a registered address is the same
        # person, not a second account.
        existing_result = await db.execute(
            select(User).where(func.lower(User.email) == (email or "").strip().lower())
            .order_by(User.id.asc()).limit(1)
        )
        existing_user = existing_result.scalars().first()

        if existing_user:
            # Check if already a member of this org
            membership_result = await db.execute(
                select(Membership.id)
                .where(Membership.user_id == existing_user.id)
                .where(Membership.organization_id == organization_id)
                .where(Membership.deleted_at.is_(None))
                .limit(1)
            )
            if membership_result.first():
                raise HTTPException(status_code=409, detail="User already exists in this organization")

            # An instance administrator's account is not an organization's to
            # provision. The refusal says nothing about the account.
            if is_instance_admin_account(existing_user):
                raise ScimError(409, "userName is not available in this organization", "uniqueness")

            if not await may_attach_existing_account(db, existing_user, organization_id):
                return await self._invite_existing_account(
                    db, organization_id, existing_user, data, base_url
                )

            # Claim an open email invite rather than creating a SECOND membership
            # that would collide with the user's later invite acceptance (the
            # invite + SSO duplicate). A pending invite already counts toward the
            # seat cap, so claiming reuses its seat and runs before enforcement.
            from app.core.permission_resolver import claim_pending_invite
            claimed = await claim_pending_invite(db, existing_user, organization_id)
            if claimed is not None:
                existing_user.is_active = data.active
                if data.externalId:
                    existing_user.scim_external_id = data.externalId
                await db.commit()
                await db.refresh(claimed)
                return _user_to_scim(existing_user, claimed, base_url)

            # Enforce the license seat cap before adding a new member. Checked after
            # the duplicate guard so re-provisioning an existing member still 409s
            # rather than being masked by a seat-limit 402.
            from app.core.seats import enforce_seat_limit
            await enforce_seat_limit(db, organization_id)

            # Add membership to existing user
            # The org's default role for new members (Members page), not a
            # hardcoded "member".
            from app.core.permission_resolver import resolve_new_member_role, grant_member_role
            role = await resolve_new_member_role(db, organization_id)
            membership = Membership(
                user_id=existing_user.id,
                organization_id=organization_id,
                email=existing_user.email,
                role=role,
            )
            db.add(membership)
            # Give the user a real RBAC assignment (not just the legacy string).
            await grant_member_role(db, organization_id, role, user_id=str(existing_user.id))

            # Honor the provisioned active state, same as the create branch
            # below and the PUT/PATCH handlers. Without this, re-provisioning
            # someone whose account was deactivated (removed from their last
            # organization — see app/core/user_lifecycle) would hand them a
            # membership they still could not log in with.
            existing_user.is_active = data.active

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

        # The admin may have invited this address before SCIM provisioned it.
        # Claim that open invite (reusing its seat) instead of creating a second
        # membership that the user's later sign-in would duplicate.
        from app.core.permission_resolver import claim_pending_invite
        claimed = await claim_pending_invite(db, user, organization_id)
        if claimed is not None:
            await db.commit()
            await db.refresh(user)
            await db.refresh(claimed)
            return _user_to_scim(user, claimed, base_url)

        # No invite to reuse: enforce the license seat cap before adding a
        # brand-new membership, so a full org can't be grown via SCIM
        # provisioning. Nothing is committed until the end, so a 402 here leaves
        # no orphan User behind.
        from app.core.seats import enforce_seat_limit
        await enforce_seat_limit(db, organization_id)

        from app.core.permission_resolver import resolve_new_member_role, grant_member_role
        role = await resolve_new_member_role(db, organization_id)
        membership = Membership(
            user_id=user.id,
            organization_id=organization_id,
            email=user.email,
            role=role,
        )
        db.add(membership)
        # Give the user a real RBAC assignment (not just the legacy string).
        await grant_member_role(db, organization_id, role, user_id=str(user.id))
        await db.commit()
        await db.refresh(user)
        await db.refresh(membership)

        return _user_to_scim(user, membership, base_url)

    async def _invite_existing_account(
        self,
        db: AsyncSession,
        organization_id: str,
        user: User,
        data: ScimUserCreate,
        base_url: str = "",
    ) -> ScimUser:
        """Provision an address whose account already belongs to another
        organization.

        The person is not placed in this organization directly: they get the
        same open invite the members list issues (address set, no user yet)
        and join by accepting it. The account row is not written.
        """
        email = (user.email or "").strip().lower()
        open_invite = (await db.execute(
            select(Membership.id)
            .where(func.lower(Membership.email) == email)
            .where(Membership.organization_id == organization_id)
            .where(Membership.user_id.is_(None))
            .where(Membership.deleted_at.is_(None))
            .limit(1)
        )).first()
        if open_invite:
            raise ScimError(409, "User already exists in this organization", "uniqueness")

        if not data.active:
            # Provisioned as inactive: nothing to grant, so no invite is
            # issued, and nothing about the existing account is returned.
            raise ScimError(409, "userName is not available in this organization", "uniqueness")

        from app.core.seats import enforce_seat_limit
        await enforce_seat_limit(db, organization_id)

        from app.services.organization_service import OrganizationService, INVITE_EXPIRY_DAYS
        from app.core.permission_resolver import resolve_new_member_role, grant_member_role
        role = await resolve_new_member_role(db, organization_id)
        membership = Membership(
            organization_id=organization_id,
            email=email,
            role=role,
            invite_expires_at=datetime.utcnow() + timedelta(days=INVITE_EXPIRY_DAYS),
        )
        db.add(membership)
        await db.flush()
        # A custom default role rides on the pending invite and is rewritten
        # onto the user when they register.
        await grant_member_role(db, organization_id, role, membership_id=str(membership.id))
        await db.commit()
        await db.refresh(membership)

        try:
            await OrganizationService()._send_invitation_email(
                db, str(organization_id), email, membership.invite_token
            )
        except Exception:
            logger.warning("SCIM: failed to send the invitation email", exc_info=True)

        if not _pending_resources_enabled():
            # Not addressable through SCIM on this deployment (see _load):
            # echo what the identity provider sent, nothing from the account.
            return ScimUser(
                userName=data.userName,
                emails=[ScimEmail(value=email, type="work", primary=True)],
                active=True,
                meta=ScimMeta(resourceType="User", created=membership.created_at,
                              lastModified=membership.updated_at),
            )
        return _pending_to_scim(user, membership, base_url)

    async def update_user(
        self,
        db: AsyncSession,
        organization_id: str,
        user_id: str,
        data: ScimUserCreate,
        base_url: str = "",
    ) -> ScimUser:
        """PUT - full replace of user attributes."""
        user, membership, pending = await self._load(db, organization_id, user_id)
        if await self._is_shared(db, organization_id, user, pending):
            return await self._apply_shared(
                db, organization_id, user, membership, pending, data.active, base_url
            )

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

        await self._commit_user_update(db, user)
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
        user, membership, pending = await self._load(db, organization_id, user_id)
        if await self._is_shared(db, organization_id, user, pending):
            # Evaluate the operations against a scratch copy: only the
            # resulting ``active`` matters here, the account row is not written.
            scratch = SimpleNamespace(
                email=user.email, name=user.name, is_active=True,
                scim_external_id=user.scim_external_id,
            )
            for op in patch.Operations:
                if op.op.lower() in ("replace", "add"):
                    self._apply_replace(scratch, op.path, op.value)
            return await self._apply_shared(
                db, organization_id, user, membership, pending, scratch.is_active, base_url
            )

        for op in patch.Operations:
            if op.op.lower() == "replace":
                self._apply_replace(user, op.path, op.value)
            elif op.op.lower() == "add":
                self._apply_replace(user, op.path, op.value)  # add behaves like replace for single-valued attrs
            elif op.op.lower() == "remove":
                if op.path and op.path.lower() == "externalid":
                    user.scim_external_id = None

        await self._commit_user_update(db, user)
        return _user_to_scim(user, membership, base_url)

    async def _commit_user_update(self, db: AsyncSession, user: User) -> None:
        """Commit a PUT/PATCH, answering an email that belongs to another
        account with 409 uniqueness (what Entra/Okta expect) instead of the
        raw constraint error."""
        conflict = ScimError(409, "userName is already in use by another user", "uniqueness")
        if user.email:
            # no_autoflush: the pending email change must not hit the unique
            # index before we have checked it.
            with db.no_autoflush:
                taken = await db.scalar(
                    select(User.id).where(
                        func.lower(User.email) == user.email.lower(),
                        User.id != user.id,
                    )
                )
            if taken:
                await db.rollback()
                raise conflict
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
            raise conflict
        await db.refresh(user)

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
            user.is_active = _scim_bool(value)
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
        """SCIM DELETE - deactivate the account when this is its sole
        organization, otherwise end this organization's membership."""
        user, membership, pending = await self._load(db, organization_id, user_id)
        if await self._is_shared(db, organization_id, user, pending):
            # The account also belongs elsewhere: deprovisioning here ends this
            # organization's membership and leaves the account alone.
            await self._end_membership(db, organization_id, membership)
        else:
            user.is_active = False
            await db.commit()
        try:
            from app.services.wait_service import wait_service
            wait_service.cancel_waits_for_user(str(user_id))
        except Exception:
            logger.warning("SCIM delete: failed to cancel pending waits", exc_info=True)
