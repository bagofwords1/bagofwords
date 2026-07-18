from typing import Optional, List, Tuple
from types import SimpleNamespace
import copy
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import lazyload

from app.models.query import Query
from app.models.widget import Widget
from app.models.report import Report
from app.models.user import User
from app.schemas.query_schema import QueryCreate, QuerySchema, QueryRunRequest
from app.schemas.step_schema import StepSchema
from app.ai.code_execution.code_execution import StreamingCodeExecutor
from app.dependencies import async_session_maker
from app.services.usage_policy_service import UsageLimitContext

from sqlalchemy import and_


def _enrich_step_schema(step_orm, step_schema: StepSchema) -> StepSchema:
    """Enrich StepSchema with relationship data from ORM"""
    if hasattr(step_orm, 'created_entity') and step_orm.created_entity:
        step_schema.created_entity_id = str(step_orm.created_entity.id)
    return step_schema


class QueryService:

    def __init__(self) -> None:
        pass

    async def create_query(
        self,
        db: AsyncSession,
        payload: QueryCreate,
        organization_id: Optional[str],
        user_id: Optional[str],
    ) -> Query:
        """Create a Query. If widget_id is not provided, create a widget under the given report_id.

        Note: For now, a Query always anchors to a Widget to avoid orphan Steps. If neither
        widget_id nor report_id is provided, this will raise a ValueError.
        """
        widget_id = payload.widget_id
        report_id = payload.report_id

        if not widget_id and not report_id:
            raise ValueError("widget_id or report_id is required to create a query")

        if not widget_id:
            # Validate report exists before creating a widget
            stmt = select(Report).where(Report.id == str(report_id))
            report = (await db.execute(stmt)).scalar_one_or_none()
            if report is None:
                raise ValueError("Report not found for creating widget")

            # Create a lightweight widget to anchor steps
            import uuid
            slug = str(uuid.uuid4())

            w = Widget(
                title=payload.title,
                slug=slug,
                report_id=str(report.id),
                status="draft",
            )
            db.add(w)
            await db.flush()
            widget_id = str(w.id)

        q = Query(
            title=payload.title,
            description=getattr(payload, "description", None),
            report_id=report_id,
            widget_id=widget_id,
            organization_id=organization_id,
            user_id=user_id,
            default_step_id=None,
        )
        db.add(q)
        await db.commit()
        await db.refresh(q)
        return q

    async def get_query(
        self,
        db: AsyncSession,
        query_id: str,
        organization_id: Optional[str] = None,
    ) -> Optional[Query]:
        """Fetch a Query by id, scoped to the caller's organization.

        When ``organization_id`` is provided the read is constrained to that
        org so a query owned by a different organization is never returned
        (defense in depth — the route decorator also enforces this binding).
        """
        stmt = select(Query).where(Query.id == str(query_id))
        if organization_id:
            stmt = stmt.where(Query.organization_id == str(organization_id))
        return (await db.execute(stmt)).scalar_one_or_none()

    async def list_queries(
        self,
        db: AsyncSession,
        report_id: Optional[str] = None,
        artifact_id: Optional[str] = None,
        organization_id: Optional[str] = None,
    ) -> List[Query]:
        """List queries, optionally filtered by report_id, artifact_id, and/or organization_id.

        If artifact_id is provided, only returns queries for visualizations used by that artifact.
        """
        stmt = select(Query)
        if report_id:
            stmt = stmt.where(Query.report_id == str(report_id))
        if organization_id:
            stmt = stmt.where(Query.organization_id == str(organization_id))

        # If artifact_id provided, filter to only queries used by that artifact
        if artifact_id:
            from app.models.artifact import Artifact
            from app.models.visualization import Visualization

            artifact_result = await db.execute(
                select(Artifact).where(
                    Artifact.id == artifact_id,
                    Artifact.deleted_at.is_(None)
                )
            )
            artifact = artifact_result.scalar_one_or_none()
            if artifact and artifact.content:
                visualization_ids = artifact.content.get("visualization_ids", [])
                if visualization_ids:
                    # Get query_ids from visualizations
                    viz_result = await db.execute(
                        select(Visualization.query_id).where(Visualization.id.in_(visualization_ids))
                    )
                    query_ids_filter = [row[0] for row in viz_result.all() if row[0]]
                    if query_ids_filter:
                        stmt = stmt.where(Query.id.in_(query_ids_filter))
                    else:
                        # No matching queries, return empty
                        return []
                else:
                    # No visualization_ids in artifact, return empty
                    return []

        res = await db.execute(stmt)
        return res.scalars().all()

    async def run_existing_step(
        self,
        db: AsyncSession,
        step_id: str,
        current_user: Optional[User] = None,
        organization_id: Optional[str] = None,
    ) -> dict:
        """Execute code for an existing step and persist result, mirroring StepService.rerun_step.

        The step is bound to the caller's organization (via its widget's
        report) before any execution so a step owned by a different
        organization cannot be rerun. Raises ValueError on a cross-org or
        missing step, which the route surfaces as 404.
        """
        # Lazy import to avoid circular dependency at module load time
        from app.services.step_service import StepService
        step_service = StepService()
        if organization_id:
            step = await step_service.get_step_by_id(db, step_id)
            if step is None:
                raise ValueError("Step not found")
            report = step.widget.report if step.widget else None
            if report is None or str(report.organization_id) != str(organization_id):
                raise ValueError("Step not found")
        step_schema = await step_service.rerun_step(db, step_id, current_user=current_user)
        return step_schema.model_dump()

    async def run_query_new_step(
        self,
        db: AsyncSession,
        query_id: str,
        request: QueryRunRequest,
        organization_id: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> Tuple[dict, str]:
        """Create a new Step under the query's widget, execute provided code, persist, and return step schema + step_id.

        This mirrors a lightweight fork-run flow: new step (draft) -> execute -> persist data.
        """
        # Load query & widget (scoped to the caller's org)
        q = await self.get_query(db, query_id, organization_id=organization_id)
        if not q:
            raise ValueError("Query not found")

        # Create a new step under the widget
        from app.models.step import Step
        import uuid
        title = (request.title or q.title or "Untitled Query").strip()
        slug = f"step-{str(uuid.uuid4())[:8]}"

        # Clone previous step's data_model (and type) if available
        previous_step = None
        try:
            if getattr(q, "default_step_id", None):
                previous_step = await db.get(Step, str(q.default_step_id))
            if previous_step is None:
                prev_stmt = (
                    select(Step)
                    .where(Step.widget_id == str(q.widget_id))
                    .order_by(Step.created_at.desc())
                )
                res_prev = await db.execute(prev_stmt)
                previous_step = res_prev.scalars().first()
        except Exception:
            previous_step = None

        cloned_data_model = {}
        cloned_type: Optional[str] = None
        if previous_step is not None:
            try:
                cloned_data_model = copy.deepcopy(getattr(previous_step, "data_model", {}) or {})
            except Exception:
                cloned_data_model = (getattr(previous_step, "data_model", {}) or {})
            cloned_type = getattr(previous_step, "type", None)

        step = Step(
            title=title,
            slug=slug,
            status="draft",
            prompt="",
            code=request.code or "",
            description="",
            type=request.type or cloned_type or "table",
            data_model=(request.data_model or cloned_data_model or {}),
            widget_id=str(q.widget_id),
            query_id=str(q.id),
        )
        db.add(step)
        await db.commit()
        await db.refresh(step)

        # Execute code
        # Load report context via widget relationship
        await db.refresh(step, attribute_names=["widget"])
        report = step.widget.report
        if not report:
            raise ValueError("Report not found for step's widget")

        # Build ds_clients using construct_clients for multi-connection support.
        # Run as the user who triggered the run so user_required connections use
        # their credentials (or owner/admin → system-cred fallback).
        from app.services.data_source_service import DataSourceService
        ds_service = DataSourceService()
        run_user = await db.get(User, user_id) if user_id else None
        ds_clients = {}
        for ds in report.data_sources:
            ds_conns = await ds_service.construct_clients(db, ds, current_user=run_user)
            ds_clients.update(ds_conns)
        excel_files = report.files
        # Pre-resolve any load_step()/load_entity() refs in the saved code so
        # reruns of code that reuses prior results keep working.
        from app.ai.code_execution.loadables import resolve_loadables_for_code, load_step_settings
        from app.models.organization import Organization
        org = await db.get(Organization, str(organization_id or report.organization_id)) if (organization_id or getattr(report, "organization_id", None)) else None
        usage_context = self._usage_context(organization_id, user_id, source="query_run", source_ref_id=query_id)
        # Pass organization_settings so widget serialization honors the org's
        # limit_row_count instead of falling back to the hardcoded 1000-row cap.
        org_settings = await org.get_settings(db) if org else None
        _ls_enabled, _ = load_step_settings(org_settings)
        loadables = await resolve_loadables_for_code(
            db, org, report, run_user, step.code, enable_load_step=_ls_enabled
        )
        executor = StreamingCodeExecutor(organization_settings=org_settings, usage_context=usage_context)
        try:
            exec_df, execution_log, _ = await executor.execute_code_async(
                code=step.code,
                ds_clients=ds_clients,
                excel_files=excel_files,
                loadables=loadables,
            )
            df = executor.format_df_for_widget(exec_df)
            # Persist results on the new step
            step.data = df
            step.status = "success"
        except Exception as e:
            # Mark step as error and surface message to client
            step.status = "error"
            try:
                step.status_reason = str(e)
            except Exception:
                step.status_reason = "Execution failed"
        finally:
            db.add(step)
            await db.commit()
            await db.refresh(step)
            # Persist buffered data-plane metering (queries/bytes are enqueued
            # by the execute_query wrapper, not written synchronously).
            if usage_context is not None:
                try:
                    await usage_context.flush()
                except Exception:
                    pass

        # If this save originated from a tool execution, update it to point to the latest step
        try:
            if getattr(request, "tool_execution_id", None):
                from app.models.tool_execution import ToolExecution  # lazy import to avoid circulars at import time
                te = await db.get(ToolExecution, str(request.tool_execution_id))
                if te is not None:
                    te.created_step_id = str(step.id)
                    if not getattr(te, "created_widget_id", None):
                        te.created_widget_id = str(q.widget_id)
                    db.add(te)
                    await db.commit()
                    await db.refresh(te)
        except Exception:
            # best-effort; do not block the main response on TE update
            pass

        # If execution succeeded, set this step as the query's default step
        if step.status == "success":
            try:
                # Refresh query instance to ensure it's attached
                await db.refresh(q)
                q.default_step_id = str(step.id)
                db.add(q)
                await db.commit()
                await db.refresh(q)
            except Exception:
                # If we fail to update default step, do not block the main response
                pass

        step_schema = _enrich_step_schema(step, StepSchema.from_orm(step))
        return (QuerySchema.model_validate(q).model_dump(), step_schema.model_dump() if hasattr(step_schema, 'model_dump') else step_schema.dict())

    async def get_default_step_for_query(
        self,
        db: AsyncSession,
        query_id: str,
        organization_id: Optional[str] = None,
        viewer_user_id: Optional[str] = None,
    ) -> Optional[StepSchema]:
        """Return the default step for a query, or a reasonable fallback.

        Priority:
        1) Query.default_step_id
        2) Latest successful step by widget
        3) Latest step by widget

        report_service._rerun_target_steps mirrors this resolution so report
        reruns re-execute exactly what dashboards render — keep them in sync.

        Scoped to the caller's organization: a query owned by a different
        org returns None (and the route decorator returns 404 first).

        When `viewer_user_id` is set and that user is not the report owner,
        a per-viewer result row (step_user_results, written by the shared-
        artifact "Run" flow) overlays the shared Step.data snapshot.
        """
        q = await self.get_query(db, query_id, organization_id=organization_id)
        if not q:
            return None

        from app.models.step import Step

        step = None
        # If default_step_id is set, use it
        if q.default_step_id:
            stmt = select(Step).where(Step.id == str(q.default_step_id))
            res = await db.execute(stmt)
            step = res.scalar_one_or_none()
        if step is None and not q.default_step_id:
            # Latest successful step for the widget
            stmt_success = (
                select(Step)
                .where(Step.widget_id == str(q.widget_id), Step.status == "success")
                .order_by(Step.created_at.desc())
            )
            res_success = await db.execute(stmt_success)
            step = res_success.scalars().first()
            if step is None:
                # Fallback: latest step
                stmt_latest = (
                    select(Step)
                    .where(Step.widget_id == str(q.widget_id))
                    .order_by(Step.created_at.desc())
                )
                res_latest = await db.execute(stmt_latest)
                step = res_latest.scalars().first()
        if step is None:
            return None

        schema = _enrich_step_schema(step, StepSchema.from_orm(step))
        schema = await self._overlay_viewer_result(db, q, step, schema, viewer_user_id)
        return schema

    async def _overlay_viewer_result(
        self,
        db: AsyncSession,
        q: Query,
        step,
        schema: StepSchema,
        viewer_user_id: Optional[str],
    ) -> StepSchema:
        """Overlay a non-owner viewer's own step result over the shared
        snapshot, via the single resolve_step_data authority."""
        if not viewer_user_id or not getattr(q, 'report_id', None):
            return schema

        owner_row = (await db.execute(
            select(Report.user_id, Report.shared_run_identity).where(Report.id == str(q.report_id))
        )).first()
        if not owner_row or str(owner_row[0]) == str(viewer_user_id):
            return schema

        # Minimal report context for the accessor (avoids re-loading the row).
        report_ctx = SimpleNamespace(
            id=str(q.report_id), user_id=str(owner_row[0]), shared_run_identity=owner_row[1],
        )
        viewer = SimpleNamespace(id=str(viewer_user_id))
        from app.services.viewer_data_policy import resolve_step_data
        resolution = await resolve_step_data(db, step, report_ctx, viewer)

        schema.viewer_result = resolution.viewer_result
        schema.data = resolution.data
        schema.snapshot_withheld = resolution.withheld
        return schema

    async def preview_query_code(
        self,
        db: AsyncSession,
        query_id: str,
        request: QueryRunRequest,
        organization_id: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> dict:
        """Execute provided code in the context of the query's widget/report without persisting a step."""
        # Load query & widget (scoped to the caller's org)
        q = await self.get_query(db, query_id, organization_id=organization_id)
        if not q:
            raise ValueError("Query not found")

        # Load report context via widget relationship
        await db.refresh(q, attribute_names=["widget"])
        report = q.widget.report
        if not report:
            raise ValueError("Report not found for query's widget")

        # Build ds_clients using construct_clients for multi-connection support.
        # Run as the user who triggered the preview so user_required connections
        # use their credentials (or owner/admin → system-cred fallback).
        from app.services.data_source_service import DataSourceService
        ds_service = DataSourceService()
        run_user = await db.get(User, user_id) if user_id else None
        ds_clients = {}
        for ds in report.data_sources:
            ds_conns = await ds_service.construct_clients(db, ds, current_user=run_user)
            ds_clients.update(ds_conns)
        excel_files = report.files
        usage_context = self._usage_context(organization_id, user_id, source="query_preview", source_ref_id=query_id)
        # Pass organization_settings so widget serialization honors the org's
        # limit_row_count instead of falling back to the hardcoded 1000-row cap.
        from app.models.organization import Organization
        org = await db.get(Organization, str(organization_id or report.organization_id)) if (organization_id or getattr(report, "organization_id", None)) else None
        org_settings = await org.get_settings(db) if org else None
        executor = StreamingCodeExecutor(organization_settings=org_settings, usage_context=usage_context)

        try:
            exec_df, execution_log, _ = await executor.execute_code_async(
                code=request.code or "",
                ds_clients=ds_clients,
                excel_files=excel_files,
            )
            df = executor.format_df_for_widget(exec_df)
            return {"preview": df, "execution_log": execution_log}
        except Exception as e:
            # Surface error to client for preview display
            return {"preview": None, "error": str(e)}
        finally:
            # Persist buffered data-plane metering (queries/bytes are enqueued
            # by the execute_query wrapper, not written synchronously).
            if usage_context is not None:
                try:
                    await usage_context.flush()
                except Exception:
                    pass

    def _usage_context(
        self,
        organization_id: Optional[str],
        user_id: Optional[str],
        *,
        source: str,
        source_ref_id: Optional[str] = None,
    ) -> Optional[UsageLimitContext]:
        if not organization_id or not user_id:
            return None
        return UsageLimitContext(
            organization_id=str(organization_id),
            user_id=str(user_id),
            source=source,
            source_ref_id=source_ref_id,
            session_maker=async_session_maker,
        )
