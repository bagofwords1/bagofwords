from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from typing import List, Optional

from app.dependencies import get_async_db, get_current_organization
from app.models.user import User
from app.models.organization import Organization
from app.core.auth import current_user
from app.core.permissions_decorator import requires_permission, check_resource_permissions, require_org_permission
from app.errors import AppError, ErrorCode

from app.models.entity import Entity
from app.schemas.entity_schema import (
    EntityCreate,
    EntityUpdate,
    EntitySchema,
    EntityListSchema,
    EntityFromStepCreate,
    EntityRunPayload,
    EntityPreviewPayload,
)
from app.services.entity_service import EntityService

router = APIRouter(prefix="/entities", tags=["entities"])
service = EntityService()


async def _holds_create_entities(db, user, organization, ds_ids: List[str]) -> bool:
    """Non-raising: does the user hold per-DS `create_entities` on ALL of
    ds_ids (org `manage_entities`/full admin included via implication)?"""
    try:
        await check_resource_permissions(
            db, str(user.id), str(organization.id),
            "data_source", ds_ids, "create_entities",
        )
        return True
    except HTTPException:
        return False


async def _require_ds_access(db, user, organization, ds_ids: List[str]) -> None:
    """403 unless the user can ACCESS every listed data source (member grant,
    public DS, or admin). This is the suggest-tier gate: weaker than the
    per-DS `create_entities` grant that publishing requires."""
    from app.core.permission_resolver import user_can_access_data_source
    from app.models.data_source import DataSource

    for rid in ds_ids:
        ds = await db.get(DataSource, str(rid))
        if (
            ds is None
            or str(ds.organization_id) != str(organization.id)
            or not await user_can_access_data_source(db, str(user.id), str(organization.id), ds)
        ):
            label = getattr(ds, "name", None) or str(rid)
            raise HTTPException(
                status_code=403,
                detail=f'No access to agent "{label}".',
            )


async def _require_entity_run_access(db, entity_id: str, organization, user) -> None:
    """Body check for run/preview: the entity's owner needs ACCESS to every
    attached data source; anyone else needs per-DS `create_entities` on all of
    them (org admins pass via implication)."""
    existing = await service.get_entity(db, entity_id, organization, user)
    if not existing:
        raise AppError.not_found(ErrorCode.ENTITY_NOT_FOUND, "Entity not found")
    ds_ids = [str(ds.id) for ds in (existing.data_sources or [])]
    if str(existing.owner_id) == str(user.id):
        if ds_ids:
            await _require_ds_access(db, user, organization, ds_ids)
        return
    if not ds_ids:
        # DS-less (org-wide) entity: rewriting its shared snapshot stays an
        # org-admin capability, mirroring publish for DS-less entities.
        from app.core.permission_resolver import resolve_permissions
        resolved = await resolve_permissions(db, str(user.id), str(organization.id))
        if not resolved.has_org_permission("manage_entities"):
            raise HTTPException(
                status_code=403,
                detail="Refreshing this entity's shared data needs 'manage_entities'. "
                       "Run it with parameter values instead to get your own result.",
            )
        return
    await check_resource_permissions(
        db, str(user.id), str(organization.id),
        "data_source", ds_ids, "create_entities",
    )


async def _require_entity_view_access(db, entity_id: str, organization, user) -> None:
    """Body check for viewer-mode (parameter VALUES) runs: the entity must be
    visible to the caller and they need ACCESS to every attached data source
    — the same bar as reading it. get_entity already applies the per-DS
    access filter; the explicit check keeps the error precise."""
    existing = await service.get_entity(db, entity_id, organization, user)
    if not existing:
        raise AppError.not_found(ErrorCode.ENTITY_NOT_FOUND, "Entity not found")
    ds_ids = [str(ds.id) for ds in (existing.data_sources or [])]
    if ds_ids:
        await _require_ds_access(db, user, organization, ds_ids)


@router.post("", response_model=EntitySchema)
@requires_permission('create_entities', resource_scoped=True)
async def create_private_entity(
    payload: EntityCreate,
    db: AsyncSession = Depends(get_async_db),
    current_user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization),
):
    """Create a new private entity (auto-published) - Private Published: published, null, published"""
    if payload.data_source_ids:
        await check_resource_permissions(
            db, str(current_user.id), str(organization.id),
            "data_source", payload.data_source_ids, "create_entities",
        )
    entity = await service.create_entity(db, payload, current_user, organization)
    return EntitySchema.model_validate(entity)


@router.post("/global", response_model=EntitySchema)
@requires_permission('create_entities', resource_scoped=True)
async def create_global_entity(
    payload: EntityCreate,
    db: AsyncSession = Depends(get_async_db),
    current_user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization),
):
    """Create a new global entity (admin only) - Global Draft/Published: null, approved, draft/published"""
    if payload.data_source_ids:
        await check_resource_permissions(
            db, str(current_user.id), str(organization.id),
            "data_source", payload.data_source_ids, "create_entities",
        )
    else:
        # Truly org-wide (no data source) → stays an org-level capability.
        await require_org_permission(
            db, str(current_user.id), str(organization.id), "create_entities",
        )
    entity = await service.create_entity(db, payload, current_user, organization)
    return EntitySchema.model_validate(entity)


# No org-level perm gate: entity visibility is derived from data_source
# access (public DSes are visible to every member). The service applies
# user-permission-based filtering internally.
@router.get("", response_model=List[EntityListSchema])
async def list_entities(
    q: Optional[str] = Query(None),
    type: Optional[str] = Query(None),
    owner_id: Optional[str] = Query(None),
    data_source_id: Optional[str] = Query(None, description="Filter by single agent ID (deprecated, use data_source_ids)"),
    data_source_ids: Optional[str] = Query(None, description="Comma-separated agent IDs to filter by"),
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=1000),
    db: AsyncSession = Depends(get_async_db),
    current_user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization),
):
    """List entities filtered by user's data source access"""
    # Parse data_source_ids from comma-separated string
    parsed_data_source_ids = None
    if data_source_ids:
        parsed_data_source_ids = [ds_id.strip() for ds_id in data_source_ids.split(',') if ds_id.strip()]
    elif data_source_id:
        parsed_data_source_ids = [data_source_id]
    
    entities = await service.list_entities(
        db,
        organization,
        current_user,
        q=q,
        type=type,
        owner_id=owner_id,
        data_source_ids=parsed_data_source_ids,
        skip=skip,
        limit=limit,
    )
    return [EntityListSchema.model_validate(e) for e in entities]


@router.get("/{entity_id}", response_model=EntitySchema)
async def get_entity(
    entity_id: str,
    db: AsyncSession = Depends(get_async_db),
    current_user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization),
):
    entity = await service.get_entity(db, entity_id, organization, current_user)
    if not entity:
        raise AppError.not_found(ErrorCode.ENTITY_NOT_FOUND, "Entity not found or access denied")
    schema = EntitySchema.model_validate(entity)
    # Withhold the materialized snapshot from non-owners when the entity reads
    # a credential-differentiated source (user_required / RLS): its `data` is
    # one identity's row slice and must not be served to other readers.
    from app.services.viewer_data_policy import entity_data_withheld
    if await entity_data_withheld(db, entity, current_user):
        schema.data = {}
        schema.snapshot_withheld = True
    return schema


@router.put("/{entity_id}", response_model=EntitySchema)
@requires_permission(['manage_entities', 'create_entities'], model=Entity, resource_scoped=True)
async def update_entity(
    entity_id: str,
    payload: EntityUpdate,
    db: AsyncSession = Depends(get_async_db),
    current_user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization),
):
    """Update an entity. Org `manage_entities` admins bypass; per-DS
    `create_entities` holders (agent owners via `manage`) may edit entities on
    their agents; the entity's own owner may edit it while it is not globally
    approved (decorator owner allowance) provided they still have access to
    its data sources."""
    existing = await service.get_entity(db, entity_id, organization, current_user)
    if not existing:
        raise AppError.not_found(ErrorCode.ENTITY_NOT_FOUND, "Entity not found")
    is_owner_unapproved = (
        str(existing.owner_id) == str(current_user.id)
        and existing.global_status != 'approved'
    )
    existing_ds_ids = [str(ds.id) for ds in (existing.data_sources or [])]
    resource_authorized = (
        await _holds_create_entities(db, current_user, organization, existing_ds_ids)
        if existing_ds_ids else False
    )
    if existing_ds_ids and not resource_authorized:
        if is_owner_unapproved:
            await _require_ds_access(db, current_user, organization, existing_ds_ids)
        else:
            await check_resource_permissions(
                db, str(current_user.id), str(organization.id),
                "data_source", existing_ds_ids, "create_entities",
            )
    # `is not None`: an empty list is falsy, so a truthiness check let a
    # per-agent author clear the scope and turn their entity into a global one,
    # bypassing the /entities/global gate.
    if payload.data_source_ids is not None:
        if payload.data_source_ids:
            if is_owner_unapproved and not resource_authorized:
                await _require_ds_access(db, current_user, organization, payload.data_source_ids)
            else:
                await check_resource_permissions(
                    db, str(current_user.id), str(organization.id),
                    "data_source", payload.data_source_ids, "create_entities",
                )
        elif not is_owner_unapproved:
            await require_org_permission(
                db, str(current_user.id), str(organization.id), "manage_entities",
            )
    entity = await service.update_entity(
        db, entity_id, payload, organization, current_user,
        resource_authorized=resource_authorized,
    )
    if not entity:
        raise AppError.not_found(ErrorCode.ENTITY_NOT_FOUND, "Entity not found")
    return EntitySchema.model_validate(entity)


@router.delete("/{entity_id}")
@requires_permission(['manage_entities', 'create_entities'], model=Entity, resource_scoped=True)
async def delete_entity(
    entity_id: str,
    db: AsyncSession = Depends(get_async_db),
    current_user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization),
):
    existing = await service.get_entity(db, entity_id, organization, current_user)
    if not existing:
        raise AppError.not_found(ErrorCode.ENTITY_NOT_FOUND, "Entity not found")
    is_owner_unapproved = (
        str(existing.owner_id) == str(current_user.id)
        and existing.global_status != 'approved'
    )
    existing_ds_ids = [str(ds.id) for ds in (existing.data_sources or [])]
    if existing_ds_ids and not is_owner_unapproved:
        await check_resource_permissions(
            db, str(current_user.id), str(organization.id),
            "data_source", existing_ds_ids, "create_entities",
        )
    ok = await service.delete_entity(db, entity_id, organization)
    if not ok:
        raise AppError.not_found(ErrorCode.ENTITY_NOT_FOUND, "Entity not found")
    return {"message": "Entity deleted successfully"}


@router.post("/from_step/{step_id}", response_model=EntitySchema)
@requires_permission('create_reports')
async def create_entity_from_step(
    step_id: str,
    payload: EntityFromStepCreate,
    db: AsyncSession = Depends(get_async_db),
    current_user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization),
):
    """Create an entity from a successful step.

    Two tiers, mirroring the dual-status workflow:
    - SUGGEST (draft pending admin approval): any member who can ACCESS every
      data source the entity will attach — saving your own query for review
      is baseline product usage, not an admin capability.
    - PUBLISH directly: per-DS `create_entities` on every attached data source
      (agent owners via `manage`, org `manage_entities`, full admins).
    """
    # Resolve the target data sources up front so the permission check matches
    # exactly what the service will attach (payload override, else the step
    # report's data sources).
    target_ds_ids = [str(i) for i in (payload.data_source_ids or []) if i]
    if not target_ds_ids:
        target_ds_ids = await service.step_report_data_source_ids(db, step_id, organization)

    if target_ds_ids:
        can_publish = await _holds_create_entities(db, current_user, organization, target_ds_ids)
    else:
        # DS-less entity → org-wide: publishing stays an org-admin capability.
        from app.core.permission_resolver import resolve_permissions
        resolved = await resolve_permissions(db, str(current_user.id), str(organization.id))
        can_publish = resolved.has_org_permission("manage_entities")

    if payload.publish and not can_publish:
        raise HTTPException(
            status_code=403,
            detail="Publishing an entity needs 'create_entities' on every attached agent. "
                   "You can still save it as a suggestion for review.",
        )
    if not can_publish and target_ds_ids:
        await _require_ds_access(db, current_user, organization, target_ds_ids)

    try:
        entity = await service.create_entity_from_step(
            db,
            step_id,
            current_user,
            organization,
            type_override=payload.type,
            title_override=payload.title,
            slug_override=payload.slug,
            description_override=payload.description,
            publish=bool(payload.publish or False),
            # Pass the RESOLVED target set (payload override or report/mention
            # fallback) so the service attaches exactly what was checked.
            data_source_ids_override=(payload.data_source_ids or target_ds_ids or None),
            creator_can_publish=can_publish,
        )
        return EntitySchema.model_validate(entity)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/{entity_id}/run", response_model=EntitySchema)
@requires_permission('view_reports', model=Entity)
async def run_entity(
    entity_id: str,
    payload: EntityRunPayload,
    db: AsyncSession = Depends(get_async_db),
    current_user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization),
):
    """Run/refresh an entity. `create_entities` holders on all its agents, org
    admins, or the entity's owner (with DS access) — execution always uses the
    CALLER's credentials, and the service only persists the result when the
    caller's identity may legitimately author the shared snapshot.

    A run carrying parameter VALUES (`params`) is viewer-mode: it never
    rewrites the shared snapshot, so it needs only what reading the entity
    needs — the entity visible to the caller and access to its data sources.
    That is the "load a saved query with my values" path for every member."""
    if payload.params:
        await _require_entity_view_access(db, entity_id, organization, current_user)
    else:
        await _require_entity_run_access(db, entity_id, organization, current_user)
    from app.ai.code_execution.query_params import ParamError
    try:
        entity = await service.run_entity_with_update(db, entity_id, payload, organization, current_user=current_user)
        return EntitySchema.model_validate(entity)
    except ParamError as e:
        # Bad parameter VALUES (unknown name, identity-locked, required
        # missing, wrong type) are a client error, not a missing entity.
        raise HTTPException(status_code=400, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/{entity_id}/preview")
@requires_permission(['manage_entities', 'create_entities'], model=Entity, resource_scoped=True)
async def preview_entity(
    entity_id: str,
    payload: EntityPreviewPayload,
    db: AsyncSession = Depends(get_async_db),
    current_user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization),
):
    """Preview (execute without persisting) — same access tier as run."""
    await _require_entity_run_access(db, entity_id, organization, current_user)
    try:
        result = await service.preview_entity(db, entity_id, payload, organization, current_user=current_user)
        return result
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


# Suggestion workflow endpoints
@router.post("/{entity_id}/suggest", response_model=EntitySchema)
@requires_permission('create_reports')
async def suggest_entity(
    entity_id: str,
    db: AsyncSession = Depends(get_async_db),
    current_user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization),
):
    """User promotes their private entity to suggestion - Private Published -> Suggested"""
    entity = await service.suggest_entity(db, entity_id, current_user, organization)
    return EntitySchema.model_validate(entity)


@router.post("/{entity_id}/withdraw", response_model=EntitySchema)
@requires_permission('create_reports')
async def withdraw_suggestion(
    entity_id: str,
    db: AsyncSession = Depends(get_async_db),
    current_user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization),
):
    """User withdraws their suggestion back to private - Suggested -> Private Published"""
    entity = await service.withdraw_suggestion(db, entity_id, current_user, organization)
    return EntitySchema.model_validate(entity)


@router.post("/{entity_id}/approve", response_model=EntitySchema)
@requires_permission('manage_entities')
async def approve_suggestion(
    entity_id: str,
    db: AsyncSession = Depends(get_async_db),
    current_user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization),
):
    """Admin approves suggestion, making it global - Suggested -> Global Published"""
    # Use update with status change to trigger approval
    entity = await service.update_entity(
        db, 
        entity_id, 
        EntityUpdate(status="published", is_admin_approval=True), 
        organization, 
        current_user
    )
    if not entity:
        raise AppError.not_found(ErrorCode.ENTITY_NOT_FOUND, "Entity not found")
    return EntitySchema.model_validate(entity)


@router.post("/{entity_id}/reject", response_model=EntitySchema)
@requires_permission('manage_entities')
async def reject_suggestion(
    entity_id: str,
    db: AsyncSession = Depends(get_async_db),
    current_user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization),
):
    """Admin rejects suggestion, returning it to private - Suggested -> Private Archived"""
    # Use update with status change to trigger rejection
    entity = await service.update_entity(
        db, 
        entity_id, 
        EntityUpdate(status="archived", is_admin_approval=True), 
        organization, 
        current_user
    )
    if not entity:
        raise AppError.not_found(ErrorCode.ENTITY_NOT_FOUND, "Entity not found")
    return EntitySchema.model_validate(entity)


