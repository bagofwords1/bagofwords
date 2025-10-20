from typing import Optional, List, Tuple
import copy
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.models.query import Query
from app.models.widget import Widget
from app.models.report import Report
from app.schemas.query_schema import QueryCreate, QuerySchema, QueryRunRequest
from app.schemas.step_schema import StepSchema
from app.ai.code_execution.code_execution import StreamingCodeExecutor

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

    async def get_query(self, db: AsyncSession, query_id: str) -> Optional[Query]:
        stmt = select(Query).where(Query.id == str(query_id))
        return (await db.execute(stmt)).scalar_one_or_none()

    async def list_queries(
        self,
        db: AsyncSession,
        report_id: Optional[str] = None,
        organization_id: Optional[str] = None,
    ) -> List[Query]:
        stmt = select(Query)
        if report_id:
            stmt = stmt.where(Query.report_id == str(report_id))
        if organization_id:
            stmt = stmt.where(Query.organization_id == str(organization_id))
        res = await db.execute(stmt)
        return res.scalars().all()

    async def run_existing_step(self, db: AsyncSession, step_id: str) -> dict:
        """Execute code for an existing step and persist result, mirroring StepService.rerun_step."""
        # Lazy import to avoid circular dependency at module load time
        from app.services.step_service import StepService
        step_service = StepService()
        step_schema = await step_service.rerun_step(db, step_id)
        return step_schema.model_dump()

    async def run_query_new_step(
        self,
        db: AsyncSession,
        query_id: str,
        request: QueryRunRequest,
    ) -> Tuple[dict, str]:
        """Create a new Step under the query's widget, execute provided code, persist, and return step schema + step_id.

        This mirrors a lightweight fork-run flow: new step (draft) -> execute -> persist data.
        """
        # Load query & widget
        q = await self.get_query(db, query_id)
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

        ds_clients = {ds.name: ds.get_client() for ds in report.data_sources}
        excel_files = report.files
        executor = StreamingCodeExecutor()
        try:
            exec_df, execution_log = executor.execute_code(code=step.code, ds_clients=ds_clients, excel_files=excel_files)
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

    async def get_default_step_for_query(self, db: AsyncSession, query_id: str) -> Optional[StepSchema]:
        """Return the default step for a query, or a reasonable fallback.

        Priority:
        1) Query.default_step_id
        2) Latest successful step by widget
        3) Latest step by widget
        """
        q = await self.get_query(db, query_id)
        if not q:
            return None

        from app.models.step import Step

        # If default_step_id is set, use it
        if q.default_step_id:
            stmt = select(Step).where(Step.id == str(q.default_step_id))
            res = await db.execute(stmt)
            step = res.scalar_one_or_none()
            return _enrich_step_schema(step, StepSchema.from_orm(step)) if step else None

        # Latest successful step for the widget
        stmt_success = (
            select(Step)
            .where(Step.widget_id == str(q.widget_id), Step.status == "success")
            .order_by(Step.created_at.desc())
        )
        res_success = await db.execute(stmt_success)
        step_success = res_success.scalars().first()
        if step_success:
            return _enrich_step_schema(step_success, StepSchema.from_orm(step_success))

        # Fallback: latest step
        stmt_latest = (
            select(Step)
            .where(Step.widget_id == str(q.widget_id))
            .order_by(Step.created_at.desc())
        )
        res_latest = await db.execute(stmt_latest)
        step_latest = res_latest.scalars().first()
        return _enrich_step_schema(step_latest, StepSchema.from_orm(step_latest)) if step_latest else None

    async def preview_query_code(
        self,
        db: AsyncSession,
        query_id: str,
        request: QueryRunRequest,
    ) -> dict:
        """Execute provided code in the context of the query's widget/report without persisting a step."""
        # Load query & widget
        q = await self.get_query(db, query_id)
        if not q:
            raise ValueError("Query not found")

        # Load report context via widget relationship
        await db.refresh(q, attribute_names=["widget"])
        report = q.widget.report
        if not report:
            raise ValueError("Report not found for query's widget")

        ds_clients = {ds.name: ds.get_client() for ds in report.data_sources}
        excel_files = report.files
        executor = StreamingCodeExecutor()

        try:
            exec_df, execution_log = executor.execute_code(code=request.code or "", ds_clients=ds_clients, excel_files=excel_files)
            df = executor.format_df_for_widget(exec_df)
            return {"preview": df, "execution_log": execution_log}
        except Exception as e:
            # Surface error to client for preview display
            return {"preview": None, "error": str(e)}


