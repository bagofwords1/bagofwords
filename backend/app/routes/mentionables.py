from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from typing import List, Optional

from app.dependencies import get_async_db, get_current_organization
from app.models.user import User
from app.core.auth import current_user
from app.core.permissions_decorator import requires_permission
from app.models.organization import Organization
from app.models.metadata_resource import MetadataResource
from app.models.datasource_table import DataSourceTable
from app.models.memory import Memory

router = APIRouter(tags=["mentionables"])


@router.get("/mentionables", response_model=List[dict])
@requires_permission('view_instructions')
async def list_mentionables(
    q: Optional[str] = Query(None, description="search text"),
    types: Optional[str] = Query(None, description="comma-separated types: metadata_resource,datasource_table,memory"),
    data_source_id: Optional[str] = Query(None),
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    wanted = set((types or "metadata_resource,datasource_table,memory").split(","))
    items: List[dict] = []

    if "metadata_resource" in wanted:
        stmt = select(MetadataResource)
        if data_source_id:
            stmt = stmt.where(MetadataResource.data_source_id == data_source_id)
        res = await db.execute(stmt)
        for r in res.scalars().all():
            if q and q.lower() not in (r.name or "").lower():
                continue
            items.append({
                "id": r.id,
                "type": "metadata_resource",
                "name": r.name,
                "data_source_id": r.data_source_id,
            })

    if "datasource_table" in wanted:
        stmt = select(DataSourceTable)
        if data_source_id:
            stmt = stmt.where(DataSourceTable.datasource_id == data_source_id)
        res = await db.execute(stmt)
        for t in res.scalars().all():
            if q and q.lower() not in (t.name or "").lower():
                continue
            items.append({
                "id": t.id,
                "type": "datasource_table",
                "name": t.name,
                "data_source_id": t.datasource_id,
            })

    if "memory" in wanted:
        stmt = select(Memory).where(Memory.organization_id == organization.id)
        res = await db.execute(stmt)
        for m in res.scalars().all():
            if q and q.lower() not in (m.title or "").lower():
                continue
            items.append({
                "id": m.id,
                "type": "memory",
                "name": m.title,
            })

    return items

