from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from typing import List, Optional

from app.dependencies import get_async_db, get_current_organization
from app.models.user import User
from app.models.organization import Organization
from app.core.auth import current_user
from app.core.permissions_decorator import requires_permission

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


@router.post("", response_model=EntitySchema)
@requires_permission('create_entities')
async def create_private_entity(
    payload: EntityCreate,
    db: AsyncSession = Depends(get_async_db),
    current_user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization),
):
    """Create a new private entity (auto-published) - Private Published: published, null, published"""
    entity = await service.create_entity(db, payload, current_user, organization, force_global=False)
    return EntitySchema.model_validate(entity)


@router.post("/global", response_model=EntitySchema)
@requires_permission('create_entities')
async def create_global_entity(
    payload: EntityCreate,
    db: AsyncSession = Depends(get_async_db),
    current_user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization),
):
    """Create a new global entity (admin only) - Global Draft/Published: null, approved, draft/published"""
    entity = await service.create_entity(db, payload, current_user, organization, force_global=True)
    return EntitySchema.model_validate(entity)


@router.get("", response_model=List[EntityListSchema])
@requires_permission('view_entities')
async def list_entities(
    q: Optional[str] = Query(None),
    type: Optional[str] = Query(None),
    owner_id: Optional[str] = Query(None),
    data_source_id: Optional[str] = Query(None, description="Filter by single domain ID (deprecated, use data_source_ids)"),
    data_source_ids: Optional[str] = Query(None, description="Comma-separated domain IDs to filter by"),
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
@requires_permission('view_entities', model=Entity)
async def get_entity(
    entity_id: str,
    db: AsyncSession = Depends(get_async_db),
    current_user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization),
):
    entity = await service.get_entity(db, entity_id, organization, current_user)
    if not entity:
        raise HTTPException(status_code=404, detail="Entity not found or access denied")
    return EntitySchema.model_validate(entity)


@router.put("/{entity_id}", response_model=EntitySchema)
@requires_permission('update_entities', model=Entity)
async def update_entity(
    entity_id: str,
    payload: EntityUpdate,
    db: AsyncSession = Depends(get_async_db),
    current_user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization),
):
    """Update an entity (permission-based: owner can edit private, admin can edit all)"""
    entity = await service.update_entity(db, entity_id, payload, organization, current_user)
    if not entity:
        raise HTTPException(status_code=404, detail="Entity not found")
    return EntitySchema.model_validate(entity)


@router.delete("/{entity_id}")
@requires_permission('delete_entities', model=Entity)
async def delete_entity(
    entity_id: str,
    db: AsyncSession = Depends(get_async_db),
    current_user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization),
):
    ok = await service.delete_entity(db, entity_id, organization)
    if not ok:
        raise HTTPException(status_code=404, detail="Entity not found")
    return {"message": "Entity deleted successfully"}


@router.post("/from_step/{step_id}", response_model=EntitySchema)
async def create_entity_from_step(
    step_id: str,
    payload: EntityFromStepCreate,
    db: AsyncSession = Depends(get_async_db),
    current_user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization),
):
    """
    Create entity from step. Accessible to both admins (create_entities) and 
    regular users (suggest_entities). Permission checking happens at service level.
    """
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
            data_source_ids_override=payload.data_source_ids,
        )
        return EntitySchema.model_validate(entity)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/{entity_id}/run", response_model=EntitySchema)
@requires_permission('update_entities', model=Entity)
async def run_entity(
    entity_id: str,
    payload: EntityRunPayload,
    db: AsyncSession = Depends(get_async_db),
    current_user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization),
):
    try:
        entity = await service.run_entity_with_update(db, entity_id, payload, organization)
        return EntitySchema.model_validate(entity)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/{entity_id}/preview")
@requires_permission('update_entities', model=Entity)
async def preview_entity(
    entity_id: str,
    payload: EntityPreviewPayload,
    db: AsyncSession = Depends(get_async_db),
    current_user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization),
):
    try:
        result = await service.preview_entity(db, entity_id, payload, organization)
        return result
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


# Suggestion workflow endpoints
@router.post("/{entity_id}/suggest", response_model=EntitySchema)
@requires_permission('suggest_entities')
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
@requires_permission('withdraw_entities')
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
@requires_permission('approve_entities')
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
        raise HTTPException(status_code=404, detail="Entity not found")
    return EntitySchema.model_validate(entity)


@router.post("/{entity_id}/reject", response_model=EntitySchema)
@requires_permission('reject_entities')
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
        raise HTTPException(status_code=404, detail="Entity not found")
    return EntitySchema.model_validate(entity)


