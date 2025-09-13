from typing import Optional, List
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.models.visualization import Visualization
from app.schemas.visualization_schema import (
    VisualizationCreate,
    VisualizationUpdate,
)


class VisualizationService:

    async def create(self, db: AsyncSession, payload: VisualizationCreate) -> Visualization:
        # Use exclude_none to avoid persisting irrelevant fields dropped by schema sanitization
        v = Visualization(
            title=payload.title,
            status=payload.status,
            report_id=str(payload.report_id),
            query_id=str(payload.query_id),
            view=(payload.view.model_dump(exclude_none=True) if hasattr(payload.view, 'model_dump') else payload.view) or {},
        )
        db.add(v)
        await db.commit()
        await db.refresh(v)
        return v

    async def get(self, db: AsyncSession, visualization_id: str) -> Optional[Visualization]:
        stmt = select(Visualization).where(Visualization.id == str(visualization_id))
        res = await db.execute(stmt)
        return res.scalar_one_or_none()

    async def list_by_report(self, db: AsyncSession, report_id: str) -> List[Visualization]:
        stmt = select(Visualization).where(Visualization.report_id == str(report_id)).order_by(Visualization.created_at.asc())
        res = await db.execute(stmt)
        return res.scalars().all()

    async def list_by_query(self, db: AsyncSession, query_id: str) -> List[Visualization]:
        stmt = select(Visualization).where(Visualization.query_id == str(query_id)).order_by(Visualization.created_at.asc())
        res = await db.execute(stmt)
        return res.scalars().all()

    async def update(self, db: AsyncSession, visualization_id: str, patch: VisualizationUpdate) -> Optional[Visualization]:
        v = await self.get(db, visualization_id)
        if not v:
            return None
        if patch.title is not None:
            v.title = patch.title
        if patch.status is not None:
            v.status = patch.status
        if patch.view is not None:
            v.view = patch.view.model_dump(exclude_none=True) if hasattr(patch.view, 'model_dump') else patch.view
        db.add(v)
        await db.commit()
        await db.refresh(v)
        return v

    async def delete(self, db: AsyncSession, visualization_id: str) -> bool:
        v = await self.get(db, visualization_id)
        if not v:
            return False
        await db.delete(v)
        await db.commit()
        return True


