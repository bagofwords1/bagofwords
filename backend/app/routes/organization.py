from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile, File
from sqlalchemy.ext.asyncio import AsyncSession
from uuid import UUID
from app.dependencies import get_async_db, ensure_path_organization_matches
from app.services.organization_service import OrganizationService
from app.schemas.organization_schema import OrganizationCreate, OrganizationSchema, OrganizationAndRoleSchema, OrganizationUpdate
from app.schemas.organization_schema import MembershipCreate, MembershipSchema, MembershipUpdate
from app.schemas.organization_schema import MembershipImportReport
from app.models.user import User
from app.models.organization import Organization
from app.core.auth import current_user, forbid_service_account_principal
from typing import List
from app.dependencies import get_current_organization
from app.core.permissions_decorator import requires_permission
from app.schemas.user_schema import UserSchema
from app.ee.audit.service import audit_service
from app.settings.config import settings

router = APIRouter(tags=["organizations"], dependencies=[Depends(ensure_path_organization_matches)])
organization_service = OrganizationService()

@router.post("/organizations", response_model=OrganizationSchema)
async def create_organization(organization: OrganizationCreate, request: Request, db: AsyncSession = Depends(get_async_db), current_user: User = Depends(current_user)):
    result = await organization_service.create_organization(db, organization, current_user)
    try:
        await audit_service.log(
            db=db,
            organization_id=result.id,
            action="organization.created",
            user_id=current_user.id,
            resource_type="organization",
            resource_id=result.id,
            details={
                "name": result.name,
                **({
                    "first_admin_email": str(organization.first_admin_email),
                    "first_admin_invite_status": result.first_admin_invite_status,
                } if organization.first_admin_email else {}),
            },
            request=request,
        )
    except Exception:
        pass
    return result

@router.post("/organizations/{organization_id}/members", response_model=MembershipSchema, dependencies=[Depends(forbid_service_account_principal)])
@requires_permission('manage_members')
async def add_member(
    organization_id: str,
    membership: MembershipCreate,
    request: Request,
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db)
):
    membership.organization_id = organization_id
    if membership.custom_attributes:
        from app.services.member_attributes import require_attribute_admin
        await require_attribute_admin(db, current_user.id, organization_id)
    # The invite's groups a registered user already holds (a removed member
    # keeps theirs), read before add_member clears the email.
    held_groups = await _groups_held_by_email(db, membership.email, membership.group_ids)
    result = await organization_service.add_member(db, membership, current_user)
    await audit_service.log(
        db=db,
        organization_id=organization_id,
        action="member.invited",
        user_id=current_user.id,
        resource_type="membership",
        resource_id=result.id,
        details={
            "email": membership.email,
            "role": membership.role,
            **({"role_ids": membership.role_ids} if membership.role_ids else {}),
            **({"group_ids": membership.group_ids} if membership.group_ids else {}),
            **({"custom_attributes": result.custom_attributes} if result.custom_attributes else {}),
        },
        request=request,
    )
    if result.user_id and set(map(str, membership.group_ids or [])) - held_groups:
        # A registered user's groups feed the row filters compiled for them:
        # when the invite added one, results they stored here before (a
        # re-added member) that this outdates are purged, once the invite is
        # committed and audited.
        from app.services import viewer_result_freshness
        await viewer_result_freshness.purge_members_results(
            db, organization_id=str(organization_id), user_ids=[str(result.user_id)])
    return result


async def _groups_held_by_email(db: AsyncSession, email: str | None, group_ids) -> set:
    """Which of `group_ids` the registered user with this email holds."""
    if not email or not group_ids:
        return set()
    from sqlalchemy import func, select

    from app.models.group_membership import GroupMembership

    return {str(g) for g in (await db.execute(
        select(GroupMembership.group_id)
        .join(User, User.id == GroupMembership.user_id)
        .where(func.lower(User.email) == email.strip().lower(),
               GroupMembership.group_id.in_([str(g) for g in group_ids]))
    )).scalars().all()}

@router.get("/organizations/{organization_id}/members", response_model=List[MembershipSchema])
@requires_permission('view_members')
async def get_members(organization_id: str, db: AsyncSession = Depends(get_async_db), organization: Organization = Depends(get_current_organization), current_user: User = Depends(current_user)):
    return await organization_service.get_members(db, organization, current_user)

@router.delete("/organizations/{organization_id}/members/{membership_id}", status_code=204, dependencies=[Depends(forbid_service_account_principal)])
@requires_permission('manage_members')
async def remove_member(
    organization_id: str,
    membership_id: str,
    request: Request,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization)
):
    await audit_service.log(
        db=db,
        organization_id=organization_id,
        action="member.removed",
        user_id=current_user.id,
        resource_type="membership",
        resource_id=membership_id,
        request=request,
    )
    return await organization_service.remove_member(db, organization_id, membership_id, current_user, organization)

@router.post("/organizations/{organization_id}/members/{membership_id}/sign-out", status_code=204, dependencies=[Depends(forbid_service_account_principal)])
@requires_permission('manage_members')
async def force_member_sign_out(
    organization_id: str,
    membership_id: str,
    request: Request,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization)
):
    """Revoke every session token held by a member.

    Invalidates session JWTs, custom OAuth access/refresh tokens and pending
    authorization codes across organizations. Does not revoke Entra sessions
    or stored downstream credentials.
    """
    from app.core.session_revocation import revoke_user_sessions

    # Scope the lookup to the org the caller was actually authorized against
    # (the resolved `organization`), not the org id in the path — those are
    # separate inputs, and only the former has been checked for membership and
    # manage_members.
    membership = await organization_service.get_member(
        db, membership_id, str(organization.id), current_user
    )
    if membership is None or not membership.user_id:
        raise HTTPException(status_code=404, detail="Member not found")

    # Sessions are per account, not per organization: revoking them signs the
    # person out everywhere. When they also belong to another organization,
    # that is an instance-level action, not one organization's to take.
    if settings.bow_config.features.allow_multiple_organizations and not current_user.is_superuser:
        from app.core.user_lifecycle import has_live_membership_elsewhere
        if await has_live_membership_elsewhere(db, str(membership.user_id), str(organization.id)):
            raise HTTPException(
                status_code=403,
                detail="This member also belongs to another organization; only an instance administrator can sign them out",
            )

    await revoke_user_sessions(db, membership.user_id)

    await audit_service.log(
        db=db,
        organization_id=organization_id,
        action="member.sessions_revoked",
        user_id=current_user.id,
        resource_type="membership",
        resource_id=membership_id,
        request=request,
    )
    return None


@router.put("/organizations/{organization_id}/members/{membership_id}", response_model=MembershipSchema, dependencies=[Depends(forbid_service_account_principal)])
@requires_permission('manage_members')
async def update_member(
    organization_id: str,
    membership_id: str,
    membership: MembershipUpdate,
    request: Request,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    if membership.custom_attributes is not None:
        from app.services.member_attributes import require_attribute_admin
        await require_attribute_admin(db, current_user.id, organization_id)
    previous = await organization_service.get_member(db, membership_id, organization_id, current_user)
    previous_attributes = dict(getattr(previous, "custom_attributes", None) or {}) if previous else {}
    result = await organization_service.update_member(db, membership_id, organization_id, membership, current_user, organization)
    if membership.custom_attributes is not None and (result.custom_attributes or {}) != previous_attributes:
        # Attributes can name the account a member acts as in another system,
        # so every change is audited with before/after values.
        try:
            await audit_service.log(
                db=db,
                organization_id=organization_id,
                action="member.attributes_changed",
                user_id=current_user.id,
                resource_type="membership",
                resource_id=membership_id,
                details={
                    "target_user_id": str(result.user_id) if result.user_id else None,
                    "before": previous_attributes,
                    "after": result.custom_attributes or {},
                },
                request=request,
            )
        except Exception:
            pass
    if membership.role is None and membership.custom_attributes is not None:
        return result
    try:
        await audit_service.log(
            db=db,
            organization_id=organization_id,
            action="member.role_changed",
            user_id=current_user.id,
            resource_type="membership",
            resource_id=membership_id,
            details={"new_role": membership.role, "target_user_id": str(result.user_id) if hasattr(result, "user_id") else None},
            request=request,
        )
    except Exception:
        pass
    return result


@router.post("/organizations/{organization_id}/members/{membership_id}/resend", response_model=MembershipSchema, dependencies=[Depends(forbid_service_account_principal)])
@requires_permission('manage_members')
async def resend_invite(
    organization_id: str,
    membership_id: str,
    request: Request,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    result = await organization_service.resend_invite(db, membership_id, organization_id)
    try:
        await audit_service.log(
            db=db,
            organization_id=organization_id,
            action="member.invite_resent",
            user_id=current_user.id,
            resource_type="membership",
            resource_id=membership_id,
            details={"email_status": result.invite_email_status},
            request=request,
        )
    except Exception:
        pass
    return result


@router.get("/organizations/{organization_id}/members/{membership_id}/invite-link", dependencies=[Depends(forbid_service_account_principal)])
@requires_permission('manage_members')
async def get_invite_link(
    organization_id: str,
    membership_id: str,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    return await organization_service.get_invite_link(db, membership_id, organization_id)


@router.post("/organizations/{organization_id}/members/import", response_model=MembershipImportReport, dependencies=[Depends(forbid_service_account_principal)])
@requires_permission('manage_members')
async def import_members(
    organization_id: str,
    request: Request,
    file: UploadFile = File(...),
    dry_run: bool = True,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    file_bytes = await file.read()
    if not file_bytes:
        raise HTTPException(status_code=400, detail="Empty file")
    report = await organization_service.import_members(
        db=db,
        organization=organization,
        file_bytes=file_bytes,
        filename=file.filename or "",
        dry_run=dry_run,
        current_user=current_user,
    )
    if not dry_run:
        try:
            await audit_service.log(
                db=db,
                organization_id=organization_id,
                action="member.imported",
                user_id=current_user.id,
                resource_type="membership",
                details={
                    "filename": file.filename,
                    "summary": report.summary.dict(),
                },
                request=request,
            )
        except Exception:
            pass
    return report

@router.get("/organizations", response_model=List[OrganizationAndRoleSchema])
async def get_organizations(db: AsyncSession = Depends(get_async_db), current_user: User = Depends(current_user)):
    return await organization_service.get_user_organizations(db, current_user)

# Member directory for share/recipient pickers: readable by any member of the
# org (get_current_organization enforces membership). Intentionally ungated.
@router.get("/organization/members", response_model=List[UserSchema])
async def get_organization_members(db: AsyncSession = Depends(get_async_db), current_user: User = Depends(current_user), organization: Organization = Depends(get_current_organization)):
    return await organization_service.get_organization_members(db, current_user, organization)


@router.get("/organization/groups")
async def get_organization_groups(db: AsyncSession = Depends(get_async_db), current_user: User = Depends(current_user), organization: Organization = Depends(get_current_organization)):
    """Minimal group directory (id, name, member count) for share pickers.

    Deliberately lighter than the RBAC groups endpoints: any org member can
    address a group by name when sharing, without manage_members access.
    """
    return await organization_service.get_organization_groups(db, organization)


@router.put("/organization", response_model=OrganizationSchema)
@requires_permission('manage_settings')
async def update_organization(
    payload: OrganizationUpdate,
    db: AsyncSession = Depends(get_async_db),
    current_user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization)
):
    return await organization_service.update_organization(db, organization, payload, current_user)