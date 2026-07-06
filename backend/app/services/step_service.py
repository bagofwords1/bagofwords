from datetime import datetime
from app.models.step import Step

from sqlalchemy.orm import Session
from app.schemas.step_schema import StepCreate, StepSchema, StepUpdate
from app.models.widget import Widget
import uuid
import json
import pandas as pd
import numpy as np
from sqlalchemy.orm import selectinload

from app.ai.prompt_formatters import TableFormatter
from app.models.completion import Completion
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from typing import Optional
from app.models.report import Report
from app.models.user import User




class StepService:

    def __init__(self):
        pass

    async def get_step_by_id(self, db: AsyncSession, step_id: str):
        result = await db.execute(
            select(Step).options(
                selectinload(Step.widget)
                .selectinload(Widget.report)
                .selectinload(Report.data_sources),
                selectinload(Step.widget)
                .selectinload(Widget.report)
                .selectinload(Report.files)
            ).filter(Step.id == step_id)
        )
        step = result.scalar_one_or_none()
        return step

    async def export_step_to_csv(self, db: AsyncSession, step_id: str) -> tuple[pd.DataFrame, Step]:
        step = await self.get_step_by_id(db, step_id)
        if not step:
            raise ValueError(f"Step {step_id} not found")

        data = step.data
        if not data or 'rows' not in data or 'columns' not in data:
            return pd.DataFrame(), step

        rows = data.get('rows', [])
        columns = data.get('columns', [])

        if not rows or not columns:
            return pd.DataFrame(), step

        headers = [col.get('headerName', col.get('field', '')) for col in columns if 'field' in col]
        fields = [col['field'] for col in columns if 'field' in col]

        data_for_df = []
        for row in rows:
            data_for_df.append([row.get(field) for field in fields])

        df = pd.DataFrame(data_for_df, columns=headers)
        return df, step

    async def create_step(self, db: AsyncSession, widget_id: str, completion_id: str) -> StepSchema:

        widget = await db.execute(select(Widget).filter(Widget.id == widget_id))
        widget = widget.scalar_one_or_none()
        if not widget:
            raise ValueError("Widget not found")
        

        # code_context = self._build_code_context(completion)
        completion = await db.execute(select(Completion).filter(Completion.id == completion_id))
        completion = completion.scalar_one_or_none()
        if not completion:
            raise ValueError("Completion not found")
        
        # Ensure the completion is fully loaded
        if completion.report is None:
            await db.refresh(completion)  # Refresh to load the report if it's lazy-loaded
        
        step_data = StepCreate(
            prompt=completion.prompt["content"],
            widget_id=widget.id,
            code="hello world",
            title="Step {uuid}".format(uuid=uuid.uuid4().hex[:4]),
            slug="step-{uuid}".format(uuid=uuid.uuid4().hex[:4]),
            status="published",
            data={"key": "value"},
            type="table",
            data_model={"key": "value"},
        )

        try:
            step = Step(**step_data.dict())
            
            db.add(step)
            await db.commit()
            await db.refresh(step)

        except Exception as e:
            print(f"Error creating Step object: {e}")
            raise
        
        step_schema = StepSchema.from_orm(step)

        return step_schema
    
    async def rerun_step(
        self,
        db: AsyncSession,
        step_id: str,
        current_user: Optional[User] = None,
        report: Optional[Report] = None,
        db_clients: Optional[dict] = None,
        organization=None,
        organization_settings=None,
    ):
        """Re-execute a step's saved code and persist the result in place.

        A report-level rerun passes `report` (with data_sources/files loaded),
        prebuilt `db_clients`, and the org context so N steps don't each
        re-hydrate the report graph, re-construct data-source clients, and
        re-read organization settings.
        """
        if report is not None:
            from sqlalchemy.orm import lazyload
            result = await db.execute(
                select(Step).options(lazyload("*")).filter(Step.id == step_id)
            )
            step = result.scalar_one_or_none()
        else:
            step = await self.get_step_by_id(db, step_id)
        if not step:
            raise ValueError("Step not found")

        if report is None:
            report = step.widget.report
        if not report:
            raise ValueError("Report not found")

        if db_clients is None:
            # Build db_clients using construct_clients for multi-connection support.
            # Run as the user who triggered the rerun so user_required connections use
            # their credentials (or owner/admin → system-cred fallback). Background
            # callers that pass no user still get None here (handled in case B).
            from app.services.data_source_service import DataSourceService
            ds_service = DataSourceService()
            db_clients = {}
            for data_source in report.data_sources:
                ds_clients = await ds_service.construct_clients(db, data_source, current_user=current_user)
                db_clients.update(ds_clients)

        excel_files = report.files
        code = step.code

        # Pre-resolve any load_step()/load_entity() refs in the saved code.
        from app.ai.code_execution.loadables import resolve_loadables_for_code
        from app.models.organization import Organization
        org = organization
        if org is None and getattr(report, "organization_id", None):
            org = await db.get(Organization, str(report.organization_id))
        loadables = await resolve_loadables_for_code(db, org, report, current_user, code)

        # Pass organization_settings so format_df_for_widget honors the org's
        # limit_row_count; without it the executor falls back to a hardcoded
        # 1000-row cap regardless of the configured limit.
        org_settings = organization_settings
        if org_settings is None and org is not None:
            org_settings = await org.get_settings(db)
        from app.ai.code_execution.code_execution import StreamingCodeExecutor
        executor = StreamingCodeExecutor(organization_settings=org_settings)

        # Execution is fully synchronous (DB drivers + pandas):
        # execute_code_async runs it on the bounded code-exec pool (same cap
        # and contextvar propagation as the agent path), and the result
        # formatting — also pandas-heavy for large frames — goes off-loop too.
        import asyncio
        df, output_log, _ = await executor.execute_code_async(
            code=code, ds_clients=db_clients, excel_files=excel_files, loadables=loadables,
        )
        df = await asyncio.to_thread(executor.format_df_for_widget, df)

        # Update existing step instead of creating new one
        step.data = df
        await db.commit()
        await db.refresh(step)

        return StepSchema.from_orm(step)

    async def get_steps_by_widget(self, db: AsyncSession, widget_id: str):
        steps = await db.execute(select(Step).filter(Step.widget_id == widget_id))
        steps = steps.scalars().all()
        return steps