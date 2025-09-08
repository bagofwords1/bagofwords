from typing import Optional, List

from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.dashboard_layout_version import DashboardLayoutVersion
from app.schemas.dashboard_layout_version_schema import (
    DashboardLayoutVersionCreate,
    DashboardLayoutVersionUpdate,
    DashboardLayoutVersionSchema,
    DashboardLayoutBlocksPatch,
)


class DashboardLayoutService:
    async def get_layouts_for_report(self, db: AsyncSession, report_id: str) -> List[DashboardLayoutVersionSchema]:
        result = await db.execute(
            select(DashboardLayoutVersion).where(DashboardLayoutVersion.report_id == report_id).order_by(
                DashboardLayoutVersion.created_at.asc()
            )
        )
        rows = result.scalars().all()
        return [DashboardLayoutVersionSchema.from_orm(r) for r in rows]

    async def get_layout(self, db: AsyncSession, layout_id: str) -> DashboardLayoutVersion:
        result = await db.execute(select(DashboardLayoutVersion).where(DashboardLayoutVersion.id == layout_id))
        layout = result.scalar_one_or_none()
        if not layout:
            raise HTTPException(status_code=404, detail="Dashboard layout not found")
        return layout

    async def create_layout(self, db: AsyncSession, payload: DashboardLayoutVersionCreate) -> DashboardLayoutVersionSchema:
        layout = DashboardLayoutVersion(
            report_id=payload.report_id,
            name=payload.name or "",
            version=payload.version or 1,
            is_active=payload.is_active or False,
            theme_name=payload.theme_name,
            theme_overrides=payload.theme_overrides or {},
            blocks=[b.model_dump() for b in payload.blocks] if payload.blocks else [],
        )
        db.add(layout)
        await db.commit()
        await db.refresh(layout)
        return DashboardLayoutVersionSchema.from_orm(layout)

    async def update_layout(self, db: AsyncSession, layout_id: str, payload: DashboardLayoutVersionUpdate) -> DashboardLayoutVersionSchema:
        layout = await self.get_layout(db, layout_id)

        if payload.name is not None:
            layout.name = payload.name
        if payload.is_active is not None:
            layout.is_active = payload.is_active
        if payload.theme_name is not None:
            layout.theme_name = payload.theme_name
        if payload.theme_overrides is not None:
            layout.theme_overrides = payload.theme_overrides
        if payload.blocks is not None:
            layout.blocks = [b.model_dump() for b in payload.blocks]

        await db.commit()
        await db.refresh(layout)
        return DashboardLayoutVersionSchema.from_orm(layout)

    async def set_active_layout(self, db: AsyncSession, report_id: str, layout_id: str) -> DashboardLayoutVersionSchema:
        # Deactivate others
        await db.execute(
            update(DashboardLayoutVersion)
            .where(DashboardLayoutVersion.report_id == report_id)
            .values(is_active=False)
        )
        # Activate chosen
        await db.execute(
            update(DashboardLayoutVersion)
            .where(DashboardLayoutVersion.id == layout_id)
            .values(is_active=True)
        )
        await db.commit()

        layout = await self.get_layout(db, layout_id)
        return DashboardLayoutVersionSchema.from_orm(layout)

    async def _get_active_layout(self, db: AsyncSession, report_id: str) -> Optional[DashboardLayoutVersion]:
        result = await db.execute(
            select(DashboardLayoutVersion).where(
                DashboardLayoutVersion.report_id == report_id,
                DashboardLayoutVersion.is_active == True  # noqa: E712
            )
        )
        return result.scalar_one_or_none()

    async def get_or_create_active_layout(self, db: AsyncSession, report_id: str) -> DashboardLayoutVersion:
        layout = await self._get_active_layout(db, report_id)
        if layout:
            return layout
        # Create a minimal active layout for legacy reports
        created_schema = await self.create_layout(db, DashboardLayoutVersionCreate(
            report_id=report_id,
            name="",
            version=1,
            is_active=True,
            theme_name=None,
            theme_overrides={},
            blocks=[],
        ))
        # Reload ORM instance
        result = await db.execute(select(DashboardLayoutVersion).where(DashboardLayoutVersion.id == created_schema.id))
        layout = result.scalar_one()
        return layout

    async def patch_layout_blocks(self, db: AsyncSession, report_id: str, layout_id: str, payload: DashboardLayoutBlocksPatch) -> DashboardLayoutVersionSchema:
        layout = await self.get_layout(db, layout_id)
        if layout.report_id != report_id:
            raise HTTPException(status_code=404, detail="Layout not found for report")

        blocks = list(layout.blocks or [])
        for patch in payload.blocks:
            updated = False
            for b in blocks:
                if b.get('type') == 'widget' and patch.type == 'widget' and patch.widget_id and b.get('widget_id') == patch.widget_id:
                    b['x'] = patch.x; b['y'] = patch.y; b['width'] = patch.width; b['height'] = patch.height
                    updated = True
                    break
                if b.get('type') == 'text_widget' and patch.type == 'text_widget' and patch.text_widget_id and b.get('text_widget_id') == patch.text_widget_id:
                    b['x'] = patch.x; b['y'] = patch.y; b['width'] = patch.width; b['height'] = patch.height
                    updated = True
                    break
                # Skipping filter identification until stable id
            if not updated:
                # Append new block when not existing yet
                if patch.type == 'widget' and patch.widget_id:
                    blocks.append({
                        'type': 'widget',
                        'widget_id': patch.widget_id,
                        'x': patch.x, 'y': patch.y,
                        'width': patch.width, 'height': patch.height,
                    })
                elif patch.type == 'text_widget' and patch.text_widget_id:
                    blocks.append({
                        'type': 'text_widget',
                        'text_widget_id': patch.text_widget_id,
                        'x': patch.x, 'y': patch.y,
                        'width': patch.width, 'height': patch.height,
                    })
        # Persist using explicit UPDATE to avoid JSON change detection edge cases
        await db.execute(
            update(DashboardLayoutVersion)
            .where(DashboardLayoutVersion.id == layout_id)
            .values(blocks=blocks)
        )
        await db.commit()
        # Reload fresh instance
        result = await db.execute(select(DashboardLayoutVersion).where(DashboardLayoutVersion.id == layout_id))
        layout = result.scalar_one()
        return DashboardLayoutVersionSchema.from_orm(layout)

    async def patch_active_layout_blocks(self, db: AsyncSession, report_id: str, payload: DashboardLayoutBlocksPatch) -> DashboardLayoutVersionSchema:
        active_layout = await self.get_or_create_active_layout(db, report_id)
        return await self.patch_layout_blocks(db, report_id, active_layout.id, payload)


