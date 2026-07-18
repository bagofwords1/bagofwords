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

    async def export_step_authorized(
        self, db: AsyncSession, step_id: str, current_user: User, organization
    ) -> tuple[pd.DataFrame, Step]:
        """Authorize and resolve a step export for a specific user.

        The raw export route historically had NO object-level check — any
        authenticated user could pull any step's rows by id (IDOR). Gate it
        like the read surface: the step's report must be in the caller's org
        and visible to them (owner / org-admin / artifact visibility), and the
        rows they get are what resolve_step_data grants — their own run, the
        shared snapshot, or (withheld) a 403. Never the raw creator snapshot
        for a strict-mode viewer.
        """
        from app.errors import AppError, ErrorCode
        from app.services.viewer_data_policy import resolve_step_data

        step = await self.get_step_by_id(db, step_id)
        if not step:
            raise AppError.not_found(ErrorCode.REPORT_NOT_FOUND, "Step not found")

        report = step.widget.report if step.widget else None
        if report is None or str(report.organization_id) != str(organization.id):
            # Cross-org / orphan → indistinguishable from not-found.
            raise AppError.not_found(ErrorCode.REPORT_NOT_FOUND, "Step not found")

        await self._authorize_report_view(db, report, current_user, organization)

        resolution = await resolve_step_data(db, step, report, current_user)
        if resolution.withheld:
            raise AppError.forbidden(
                ErrorCode.ACCESS_DENIED,
                "This dashboard runs with your credentials — run it to export your own data",
            )
        return self._df_from_step_data(resolution.data), step

    async def _authorize_report_view(self, db: AsyncSession, report, current_user: User, organization) -> None:
        """Raise unless current_user may view `report` (owner / org full-admin /
        artifact visibility). Mirrors the artifact GET gate."""
        from app.errors import AppError, ErrorCode
        from app.core.permission_resolver import resolve_permissions, FULL_ADMIN
        from app.services.report_service import ReportService

        if current_user is not None and str(report.user_id) == str(current_user.id):
            return
        # Org full-admins keep read-any (same bypass as the permissions decorator).
        if current_user is not None:
            resolved = await resolve_permissions(db, str(current_user.id), str(organization.id))
            if FULL_ADMIN in resolved.org_permissions:
                return
        try:
            await ReportService()._check_visibility(db, report, 'artifact_visibility', current_user)
        except Exception:
            raise AppError.forbidden(ErrorCode.ACCESS_DENIED, "You don't have access to this report")

    def _df_from_step_data(self, data) -> pd.DataFrame:
        if not data or 'rows' not in data or 'columns' not in data:
            return pd.DataFrame()
        rows = data.get('rows', [])
        columns = data.get('columns', [])
        if not rows or not columns:
            return pd.DataFrame()
        headers = [col.get('headerName', col.get('field', '')) for col in columns if 'field' in col]
        fields = [col['field'] for col in columns if 'field' in col]
        return pd.DataFrame([[row.get(f) for f in fields] for row in rows], columns=headers)

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
    
    async def _load_step_for_rerun(
        self,
        db: AsyncSession,
        step_id: str,
        report: Optional[Report] = None,
    ) -> tuple[Step, Report]:
        """Load a step (and its report) for re-execution."""
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
        return step, report

    async def _execute_step_code(
        self,
        db: AsyncSession,
        step: Step,
        report: Report,
        current_user: Optional[User] = None,
        db_clients: Optional[dict] = None,
        organization=None,
        organization_settings=None,
    ) -> dict:
        """Execute a step's saved code and return the formatted result frame.

        Pure execution — persists nothing. `current_user` decides whose
        data-source credentials are used when `db_clients` isn't prebuilt.
        """
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
        from app.ai.code_execution.loadables import resolve_loadables_for_code, load_step_settings
        from app.models.organization import Organization
        org = organization
        if org is None and getattr(report, "organization_id", None):
            org = await db.get(Organization, str(report.organization_id))

        # Resolve org settings first: they gate load_step and (below) the row
        # limit. Without them format_df_for_widget falls back to a hardcoded
        # 1000-row cap regardless of the configured limit.
        org_settings = organization_settings
        if org_settings is None and org is not None:
            org_settings = await org.get_settings(db)
        _ls_enabled, _ = load_step_settings(org_settings)
        loadables = await resolve_loadables_for_code(
            db, org, report, current_user, code, enable_load_step=_ls_enabled
        )
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
        return df

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
        step, report = await self._load_step_for_rerun(db, step_id, report)

        df = await self._execute_step_code(
            db, step, report,
            current_user=current_user, db_clients=db_clients,
            organization=organization, organization_settings=organization_settings,
        )

        # Update existing step instead of creating new one
        step.data = df

        # The shared snapshot changed — per-viewer cached results for this
        # step are now stale. They are a cache of derived data, so hard-delete.
        from sqlalchemy import delete as sa_delete
        from app.models.step_user_result import StepUserResult
        await db.execute(sa_delete(StepUserResult).where(StepUserResult.step_id == str(step_id)))

        await db.commit()
        await db.refresh(step)

        return StepSchema.from_orm(step)

    async def run_step_to_user_result(
        self,
        db: AsyncSession,
        step_id: str,
        run_user: User,
        credential_user: User,
        executed_as: str,
        report: Optional[Report] = None,
        db_clients: Optional[dict] = None,
        organization=None,
        organization_settings=None,
    ):
        """Re-execute a step's saved code for a shared-artifact viewer.

        The result is persisted to the viewer's own StepUserResult row —
        never to the shared Step.data snapshot — so one viewer's run cannot
        change what the owner or other viewers see. `credential_user` decides
        whose data-source credentials execute the query ('viewer' mode: the
        viewer; 'creator' mode: the report owner); `run_user` is always the
        viewer the result row belongs to. Execution errors are persisted on
        the row (status='error') instead of raised, so the viewer sees why
        their run failed.
        """
        from app.models.step_user_result import StepUserResult

        step, report = await self._load_step_for_rerun(db, step_id, report)

        status = 'success'
        status_reason = None
        data = None
        try:
            data = await self._execute_step_code(
                db, step, report,
                current_user=credential_user, db_clients=db_clients,
                organization=organization, organization_settings=organization_settings,
            )
        except Exception as e:
            status = 'error'
            status_reason = str(e)[:2000] or e.__class__.__name__

        existing = await db.execute(
            select(StepUserResult).where(
                StepUserResult.step_id == str(step_id),
                StepUserResult.user_id == str(run_user.id),
            )
        )
        row = existing.scalar_one_or_none()
        if row is None:
            row = StepUserResult(
                step_id=str(step_id),
                user_id=str(run_user.id),
                organization_id=str(report.organization_id),
                report_id=str(report.id),
            )
            db.add(row)

        row.status = status
        row.status_reason = status_reason
        row.data = data
        row.executed_as = executed_as
        row.last_run_at = datetime.utcnow()
        await db.commit()
        await db.refresh(row)
        return row

    async def get_steps_by_widget(self, db: AsyncSession, widget_id: str):
        steps = await db.execute(select(Step).filter(Step.widget_id == widget_id))
        steps = steps.scalars().all()
        return steps