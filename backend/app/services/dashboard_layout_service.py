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
    async def get_layouts_for_report(self, db: AsyncSession, report_id: str, hydrate: bool = False) -> List[DashboardLayoutVersionSchema]:
        result = await db.execute(
            select(DashboardLayoutVersion).where(DashboardLayoutVersion.report_id == report_id).order_by(
                DashboardLayoutVersion.created_at.asc()
            )
        )
        rows = result.scalars().all()
        schemas = [DashboardLayoutVersionSchema.from_orm(r) for r in rows]
        if not hydrate:
            return schemas

        # Hydrate blocks with embedded widget/visualization/text_widget payloads
        try:
            from app.models.widget import Widget
            from app.models.visualization import Visualization
            from app.models.text_widget import TextWidget
            # Preload all referenced ids for this report in one shot
            result_widgets = await db.execute(select(Widget).where(Widget.report_id == report_id))
            widgets = {str(w.id): w for w in result_widgets.scalars().all()}
            result_visualizations = await db.execute(select(Visualization))
            visualizations = {str(v.id): v for v in result_visualizations.scalars().all()}
            result_text = await db.execute(select(TextWidget).where(TextWidget.report_id == report_id))
            text_widgets = {str(t.id): t for t in result_text.scalars().all()}

            for s in schemas:
                blocks = []
                for b in (s.blocks or []):
                    b_dict = b.model_dump()
                    if b_dict.get('type') == 'widget':
                        wid = b_dict.get('widget_id')
                        if wid and wid in widgets:
                            from app.schemas.widget_schema import WidgetSchema
                            b_dict['widget'] = WidgetSchema.from_orm(widgets[wid]).model_dump()
                    elif b_dict.get('type') == 'visualization':
                        vid = b_dict.get('visualization_id')
                        if vid and vid in visualizations:
                            from app.schemas.visualization_schema import VisualizationSchema
                            b_dict['visualization'] = VisualizationSchema.from_orm(visualizations[vid]).model_dump()
                    elif b_dict.get('type') == 'text_widget':
                        tid = b_dict.get('text_widget_id')
                        if tid and tid in text_widgets:
                            from app.schemas.text_widget_schema import TextWidgetSchema
                            b_dict['text_widget'] = TextWidgetSchema.from_orm(text_widgets[tid]).model_dump()
                    blocks.append(b_dict)
                # Replace blocks with hydrated dicts; pydantic will coerce on response
                s.blocks = blocks  # type: ignore
        except Exception:
            # Fail open: return unhydrated if anything goes wrong
            return schemas

        return schemas

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
        """Fetch the most recent active layout; tolerate multiple actives by picking latest."""
        result = await db.execute(
            select(DashboardLayoutVersion)
            .where(
                DashboardLayoutVersion.report_id == report_id,
                DashboardLayoutVersion.is_active == True  # noqa: E712
            )
            .order_by(DashboardLayoutVersion.created_at.desc())
        )
        return result.scalars().first()

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
                    # Apply optional view_overrides if provided (dashboard layout wins)
                    if getattr(patch, 'view_overrides', None) is not None:
                        b['view_overrides'] = (patch.view_overrides.model_dump() if hasattr(patch.view_overrides, 'model_dump') else patch.view_overrides) or None
                    updated = True
                    break
                if b.get('type') == 'visualization' and patch.type == 'visualization' and patch.visualization_id and b.get('visualization_id') == patch.visualization_id:
                    b['x'] = patch.x; b['y'] = patch.y; b['width'] = patch.width; b['height'] = patch.height
                    if getattr(patch, 'view_overrides', None) is not None:
                        b['view_overrides'] = (patch.view_overrides.model_dump() if hasattr(patch.view_overrides, 'model_dump') else patch.view_overrides) or None
                    updated = True
                    break
                if b.get('type') == 'text_widget' and patch.type == 'text_widget' and patch.text_widget_id and b.get('text_widget_id') == patch.text_widget_id:
                    b['x'] = patch.x; b['y'] = patch.y; b['width'] = patch.width; b['height'] = patch.height
                    if getattr(patch, 'view_overrides', None) is not None:
                        b['view_overrides'] = (patch.view_overrides.model_dump() if hasattr(patch.view_overrides, 'model_dump') else patch.view_overrides) or None
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
                        **({'view_overrides': (patch.view_overrides.model_dump() if hasattr(patch.view_overrides, 'model_dump') else patch.view_overrides)} if getattr(patch, 'view_overrides', None) is not None else {})
                    })
                elif patch.type == 'visualization' and patch.visualization_id:
                    blocks.append({
                        'type': 'visualization',
                        'visualization_id': patch.visualization_id,
                        'x': patch.x, 'y': patch.y,
                        'width': patch.width, 'height': patch.height,
                        **({'view_overrides': (patch.view_overrides.model_dump() if hasattr(patch.view_overrides, 'model_dump') else patch.view_overrides)} if getattr(patch, 'view_overrides', None) is not None else {})
                    })
                elif patch.type == 'text_widget' and patch.text_widget_id:
                    blocks.append({
                        'type': 'text_widget',
                        'text_widget_id': patch.text_widget_id,
                        'x': patch.x, 'y': patch.y,
                        'width': patch.width, 'height': patch.height,
                        **({'view_overrides': (patch.view_overrides.model_dump() if hasattr(patch.view_overrides, 'model_dump') else patch.view_overrides)} if getattr(patch, 'view_overrides', None) is not None else {})
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


