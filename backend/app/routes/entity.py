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
)
from app.services.entity_service import EntityService

router = APIRouter(prefix="/entities", tags=["entities"])
service = EntityService()


@router.post("", response_model=EntitySchema)
@requires_permission('create_entities')
async def create_entity(
    payload: EntityCreate,
    db: AsyncSession = Depends(get_async_db),
    current_user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization),
):
    entity = await service.create_entity(db, payload, current_user, organization)
    return EntitySchema.model_validate(entity)


@router.get("", response_model=List[EntityListSchema])
@requires_permission('view_entities')
async def list_entities(
    q: Optional[str] = Query(None),
    type: Optional[str] = Query(None),
    owner_id: Optional[str] = Query(None),
    data_source_id: Optional[str] = Query(None),
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=1000),
    db: AsyncSession = Depends(get_async_db),
    current_user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization),
):
    entities = await service.list_entities(
        db,
        organization,
        q=q,
        type=type,
        owner_id=owner_id,
        data_source_id=data_source_id,
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
    entity = await service.get_entity(db, entity_id, organization)
    if not entity:
        raise HTTPException(status_code=404, detail="Entity not found")
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
    entity = await service.update_entity(db, entity_id, payload, organization)
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
@requires_permission('create_entities')
async def create_entity_from_step(
    step_id: str,
    payload: EntityFromStepCreate,
    db: AsyncSession = Depends(get_async_db),
    current_user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization),
):
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
        )
        return EntitySchema.model_validate(entity)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/{entity_id}/run", response_model=EntitySchema)
@requires_permission('update_entities', model=Entity)
async def run_entity(
    entity_id: str,
    db: AsyncSession = Depends(get_async_db),
    current_user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization),
):
    try:
        entity = await service.run_entity(db, entity_id, organization)
        return EntitySchema.model_validate(entity)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


