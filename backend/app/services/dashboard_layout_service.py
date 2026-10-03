from typing import Optional

from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.dashboard_layout_version import DashboardLayoutVersion
from app.schemas.dashboard_layout_version_schema import (
    DashboardLayoutVersionCreate,
    DashboardLayoutVersionSchema,
    DashboardLayoutBlocksPatch,
)
from app.core.telemetry import telemetry
from app.models.user import User
from app.models.organization import Organization


class DashboardLayoutService:
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

    async def patch_layout_blocks(self, db: AsyncSession, report_id: str, layout_id: str, payload: DashboardLayoutBlocksPatch, current_user: User = None, organization: Organization = None) -> DashboardLayoutVersionSchema:
        layout = await self.get_layout(db, layout_id)
        if layout.report_id != report_id:
            raise HTTPException(status_code=404, detail="Layout not found for report")

        def _serialize_view_overrides(vo):
            if vo is None:
                return None
            return vo.model_dump() if hasattr(vo, 'model_dump') else vo

        def _serialize_chrome(chrome):
            if chrome is None:
                return None
            return chrome.model_dump() if hasattr(chrome, 'model_dump') else chrome

        def _serialize_columns(columns):
            if not columns:
                return []
            result = []
            for col in columns:
                if hasattr(col, 'model_dump'):
                    result.append(col.model_dump())
                elif isinstance(col, dict):
                    result.append(col)
                else:
                    result.append({'span': getattr(col, 'span', 6), 'children': getattr(col, 'children', [])})
            return result

        blocks = list(layout.blocks or [])
        for patch in payload.blocks:
            updated = False
            for b in blocks:
                if b.get('type') == 'widget' and patch.type == 'widget' and patch.widget_id and b.get('widget_id') == patch.widget_id:
                    b['x'] = patch.x; b['y'] = patch.y; b['width'] = patch.width; b['height'] = patch.height
                    # Apply optional view_overrides if provided (dashboard layout wins)
                    if getattr(patch, 'view_overrides', None) is not None:
                        b['view_overrides'] = _serialize_view_overrides(patch.view_overrides)
                    updated = True
                    break
                if b.get('type') == 'visualization' and patch.type == 'visualization' and patch.visualization_id and b.get('visualization_id') == patch.visualization_id:
                    b['x'] = patch.x; b['y'] = patch.y; b['width'] = patch.width; b['height'] = patch.height
                    if getattr(patch, 'view_overrides', None) is not None:
                        b['view_overrides'] = _serialize_view_overrides(patch.view_overrides)
                    updated = True
                    break
                if b.get('type') == 'text_widget' and patch.type == 'text_widget' and patch.text_widget_id and b.get('text_widget_id') == patch.text_widget_id:
                    b['x'] = patch.x; b['y'] = patch.y; b['width'] = patch.width; b['height'] = patch.height
                    if getattr(patch, 'view_overrides', None) is not None:
                        b['view_overrides'] = _serialize_view_overrides(patch.view_overrides)
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
                        **({'view_overrides': _serialize_view_overrides(patch.view_overrides)} if getattr(patch, 'view_overrides', None) is not None else {})
                    })
                elif patch.type == 'visualization' and patch.visualization_id:
                    # Wrap visualization in a card for consistent styling
                    blocks.append({
                        'type': 'card',
                        'chrome': {'border': 'soft'},
                        'children': [{
                            'type': 'visualization',
                            'visualization_id': patch.visualization_id,
                            'x': 0, 'y': 0,
                            'width': 12, 'height': patch.height,
                            **({'view_overrides': _serialize_view_overrides(patch.view_overrides)} if getattr(patch, 'view_overrides', None) is not None else {})
                        }],
                        'x': patch.x, 'y': patch.y,
                        'width': patch.width, 'height': patch.height,
                    })
                elif patch.type == 'text_widget' and patch.text_widget_id:
                    blocks.append({
                        'type': 'text_widget',
                        'text_widget_id': patch.text_widget_id,
                        'x': patch.x, 'y': patch.y,
                        'width': patch.width, 'height': patch.height,
                        **({'view_overrides': _serialize_view_overrides(patch.view_overrides)} if getattr(patch, 'view_overrides', None) is not None else {})
                    })
                # Inline text blocks (AI-generated, no DB reference)
                elif patch.type == 'text':
                    blocks.append({
                        'type': 'text',
                        'content': patch.content or '',
                        'variant': patch.variant,
                        'x': patch.x, 'y': patch.y,
                        'width': patch.width, 'height': patch.height,
                        **({'view_overrides': _serialize_view_overrides(patch.view_overrides)} if getattr(patch, 'view_overrides', None) is not None else {})
                    })
                # Card blocks with children
                elif patch.type == 'card':
                    blocks.append({
                        'type': 'card',
                        'chrome': _serialize_chrome(patch.chrome),
                        'children': patch.children or [],
                        'x': patch.x, 'y': patch.y,
                        'width': patch.width, 'height': patch.height,
                        **({'view_overrides': _serialize_view_overrides(patch.view_overrides)} if getattr(patch, 'view_overrides', None) is not None else {})
                    })
                # Column layout blocks
                elif patch.type == 'column_layout':
                    blocks.append({
                        'type': 'column_layout',
                        'columns': _serialize_columns(patch.columns),
                        'x': patch.x, 'y': patch.y,
                        'width': patch.width, 'height': patch.height,
                        **({'view_overrides': _serialize_view_overrides(patch.view_overrides)} if getattr(patch, 'view_overrides', None) is not None else {})
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
        # Telemetry: dashboard layout blocks patched
        try:
            await telemetry.capture(
                "dashboard_layout_blocks_patched",
                {
                    "layout_id": str(layout.id),
                    "report_id": str(report_id),
                    "blocks_count": len(layout.blocks or [])
                },
                user_id=current_user.id if current_user else None,
                org_id=organization.id if organization else None,
            )
        except Exception:
            pass
        return DashboardLayoutVersionSchema.from_orm(layout)

    async def patch_active_layout_blocks(self, db: AsyncSession, report_id: str, payload: DashboardLayoutBlocksPatch, current_user: User = None, organization: Organization = None) -> DashboardLayoutVersionSchema:
        active_layout = await self.get_or_create_active_layout(db, report_id)
        return await self.patch_layout_blocks(db, report_id, active_layout.id, payload, current_user, organization)


    async def remove_blocks_for_text_widget(self, db: AsyncSession, report_id: str, text_widget_id: str) -> None:
        """Remove any blocks referencing the given text_widget_id from ALL layouts for the report.

        This is used to keep the dashboard layout JSON consistent when a text widget
        is deleted (or was already deleted) so subsequent operations don't fail
        due to dangling references.
        """
        # Load all layouts for report
        result = await db.execute(
            select(DashboardLayoutVersion).where(DashboardLayoutVersion.report_id == report_id)
        )
        layouts = list(result.scalars().all())

        for layout in layouts:
            original_blocks = list(layout.blocks or [])
            filtered_blocks = [
                b for b in original_blocks
                if not (isinstance(b, dict) and b.get("type") == "text_widget" and b.get("text_widget_id") == text_widget_id)
            ]

            if filtered_blocks != original_blocks:
                # Persist via explicit UPDATE to avoid JSON change detection edge cases
                await db.execute(
                    update(DashboardLayoutVersion)
                    .where(DashboardLayoutVersion.id == layout.id)
                    .values(blocks=filtered_blocks)
                )

        # Commit once for all updates
        await db.commit()

