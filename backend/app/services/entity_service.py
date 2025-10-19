from typing import List, Optional

from sqlalchemy import select, or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.entity import Entity, entity_data_source_association
from app.models.data_source import DataSource
from app.models.user import User
from app.models.organization import Organization
from app.schemas.entity_schema import EntityCreate, EntityUpdate


class EntityService:

    async def create_entity(
        self,
        db: AsyncSession,
        payload: EntityCreate,
        current_user: User,
        organization: Organization,
    ) -> Entity:
        entity = Entity(
            organization_id=str(organization.id),
            owner_id=str(current_user.id),
            type=payload.type,
            title=payload.title,
            slug=payload.slug,
            description=payload.description,
            tags=payload.tags,
            code=payload.code,
            data=payload.data,
            view=(payload.view.model_dump() if payload.view else None),
            status=payload.status,
            published_at=payload.published_at,
            last_refreshed_at=payload.last_refreshed_at,
        )
        db.add(entity)
        if payload.data_source_ids:
            result = await db.execute(select(DataSource).where(DataSource.id.in_(payload.data_source_ids)))
            entity.data_sources = list(result.scalars().all())
        await db.flush()
        await db.commit()
        await db.refresh(entity)
        return entity

    async def list_entities(
        self,
        db: AsyncSession,
        organization: Organization,
        *,
        q: Optional[str] = None,
        type: Optional[str] = None,
        owner_id: Optional[str] = None,
        data_source_id: Optional[str] = None,
        skip: int = 0,
        limit: int = 100,
    ) -> List[Entity]:
        stmt = select(Entity).where(Entity.organization_id == str(organization.id))
        if type:
            stmt = stmt.where(Entity.type == type)
        if owner_id:
            stmt = stmt.where(Entity.owner_id == owner_id)
        if q:
            like = f"%{q}%"
            stmt = stmt.where(or_(Entity.title.ilike(like), Entity.slug.ilike(like)))
        if data_source_id:
            stmt = stmt.join(entity_data_source_association, entity_data_source_association.c.entity_id == Entity.id)
            stmt = stmt.where(entity_data_source_association.c.data_source_id == data_source_id)
        stmt = stmt.order_by(Entity.updated_at.desc()).offset(skip).limit(limit)
        result = await db.execute(stmt)
        return list(result.scalars().all())

    async def get_entity(
        self,
        db: AsyncSession,
        entity_id: str,
        organization: Organization,
    ) -> Optional[Entity]:
        result = await db.execute(
            select(Entity)
            .options(selectinload(Entity.data_sources))
            .where(Entity.id == entity_id, Entity.organization_id == str(organization.id))
        )
        return result.scalar_one_or_none()

    async def update_entity(
        self,
        db: AsyncSession,
        entity_id: str,
        payload: EntityUpdate,
        organization: Organization,
    ) -> Optional[Entity]:
        result = await db.execute(select(Entity).where(Entity.id == entity_id, Entity.organization_id == str(organization.id)))
        entity = result.scalar_one_or_none()
        if not entity:
            return None

        for field in ["type", "title", "slug", "description", "tags", "code", "data", "status", "published_at", "last_refreshed_at"]:
            value = getattr(payload, field, None)
            if value is not None:
                setattr(entity, field, value)
        if payload.view is not None:
            entity.view = payload.view.model_dump()
        if payload.data_source_ids is not None:
            if payload.data_source_ids:
                result = await db.execute(select(DataSource).where(DataSource.id.in_(payload.data_source_ids)))
                entity.data_sources = list(result.scalars().all())
            else:
                entity.data_sources = []

        await db.flush()
        await db.commit()
        await db.refresh(entity)
        return entity

    async def delete_entity(
        self,
        db: AsyncSession,
        entity_id: str,
        organization: Organization,
    ) -> bool:
        result = await db.execute(select(Entity).where(Entity.id == entity_id, Entity.organization_id == str(organization.id)))
        entity = result.scalar_one_or_none()
        if not entity:
            return False
        await db.delete(entity)
        await db.commit()
        return True


