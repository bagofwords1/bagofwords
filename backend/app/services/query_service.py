from typing import Optional, List, Tuple
from types import SimpleNamespace
import copy
from fastapi import HTTPException
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


class _PolicyProbe:
    """A read-only stand-in that answers policy questions from unsaved values.

    `preview_access_policy` must be able to try a policy the creator has not
    committed. Mutating the ORM row and rolling back would work right up until
    something else in the session flushed first; a shim cannot.
    """

    __slots__ = ("_row", "_over")

    def __init__(self, row, overrides: dict):
        self._row = row
        self._over = {k: v for k, v in (overrides or {}).items() if v is not None}

    def __getattr__(self, name):
        if name in self._over:
            return self._over[name]
        return getattr(self._row, name)


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

    async def overlay_viewer_on_query_schema(
        self,
        db: AsyncSession,
        q: Query,
        schema,
        viewer_user_id: Optional[str],
    ):
        """Apply the per-viewer step-data policy to a QuerySchema's embedded
        default_step before it is returned.

        list_queries / get_query serialize the query's default_step
        (lazy="selectin") straight from the ORM, which carries the shared
        Step.data snapshot. A non-owner reading a report whose snapshot is
        credential-differentiated (viewer-identity on a user-scoped source, or
        an RLS relation) must get their own result or nothing — never the
        creator's rows. Reuse the single resolve_step_data authority via
        _overlay_viewer_result; it is a no-op for owners and plain reports.
        """
        step_schema = getattr(schema, "default_step", None)
        if step_schema is None or getattr(q, "default_step", None) is None:
            return schema
        schema.default_step = await self._overlay_viewer_result(
            db, q, q.default_step, step_schema, viewer_user_id
        )
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
        if resolution.withheld:
            # No code either — SQL leaks schema/table/filter details.
            schema.code = ""
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

    # -- row/column security ------------------------------------------------

    @staticmethod
    def ensure_access_policy_licensed() -> None:
        """Enterprise gate on AUTHORING a policy — not on enforcing one.

        Editing or previewing a policy requires the license; a policy that is
        already saved keeps filtering even if the license lapses, because an
        expired license must fail closed and never widen anyone's visibility.
        Turning a policy OFF stays open too, so a lapsed org is not trapped
        behind a filter it can no longer administer.
        """
        from app.ee.license import has_feature

        if not has_feature("rls"):
            raise HTTPException(
                status_code=402,
                detail=(
                    "Row- and column-level security requires an enterprise license. "
                    "Add a license key in Settings → License (or set BOW_LICENSE_KEY)."
                ),
            )

    async def _result_columns(self, db: AsyncSession, query: Query) -> List[str]:
        """Column names of the query's most recent result.

        Read off the rendered step rather than the data model: the data model is
        what the agent intended, the step is what the source actually returned,
        and a policy has to name a column that really exists.
        """
        step = await self.get_default_step_for_query(
            db, str(query.id), organization_id=str(query.organization_id) if query.organization_id else None
        )
        if step is None:
            return []
        data = getattr(step, "data", None) or {}
        cols = data.get("columns") or []
        out: List[str] = []
        for c in cols:
            if isinstance(c, dict):
                name = c.get("field") or c.get("name") or c.get("headerName")
                if name:
                    out.append(str(name))
            elif c:
                out.append(str(c))
        return out

    async def access_policy_options(
        self, db: AsyncSession, query: Query, organization_id: str
    ) -> dict:
        """Everything the policy editor needs to offer real choices.

        Offering the attribute keys the org ACTUALLY syncs (rather than a
        hardcoded Entra list) is what stops a creator binding a policy to an
        attribute that will never resolve — which, with default-deny, is an
        empty widget and a support ticket.
        """
        from app.models.group import Group
        from app.models.membership import Membership
        from app.models.role import Role
        from app.models.user import User as UserModel
        from app.services.rls_identity_service import available_attribute_keys

        columns = await self._result_columns(db, query)
        attribute_keys = await available_attribute_keys(db, str(organization_id))

        members = []
        try:
            rows = (await db.execute(
                select(UserModel.id, UserModel.name, UserModel.email)
                .join(Membership, Membership.user_id == UserModel.id)
                .where(Membership.organization_id == str(organization_id))
            )).all()
            members = [
                {"id": str(uid), "name": name or email, "email": email}
                for uid, name, email in rows
            ]
        except Exception:
            members = []

        groups = []
        try:
            rows = (await db.execute(
                select(Group.id, Group.name).where(
                    Group.organization_id == str(organization_id)
                )
            )).all()
            groups = [{"id": str(gid), "name": name} for gid, name in rows]
        except Exception:
            groups = []

        roles = []
        try:
            rows = (await db.execute(
                select(Role.id, Role.name).where(
                    Role.organization_id == str(organization_id)
                )
            )).all()
            roles = [{"id": str(rid), "name": name} for rid, name in rows]
        except Exception:
            roles = []

        return {
            "columns": columns,
            "attribute_keys": attribute_keys,
            "members": members,
            "groups": groups,
            "roles": roles,
            "rls_enabled": bool(query.rls_enabled),
            "rls_mode": query.rls_mode,
            "rls_policy": query.rls_policy,
            "rls_default_deny": bool(query.rls_default_deny),
            "cls_enabled": bool(query.cls_enabled),
            "cls_policy": query.cls_policy,
        }

    async def set_access_policy(
        self,
        db: AsyncSession,
        query: Query,
        *,
        rls_enabled: bool = False,
        rls_mode: Optional[str] = None,
        rls_policy: Optional[dict] = None,
        rls_default_deny: bool = True,
        cls_enabled: bool = False,
        cls_policy: Optional[dict] = None,
    ) -> Query:
        """Store the policy, rejecting one that cannot mean what it says.

        Save-time validation is not the enforcement boundary — the query's
        result columns can change under a saved policy, and the compiler denies
        in that case — but a creator should learn about a typo when they press
        Save rather than by a colleague reporting an empty dashboard.
        """
        from app.data_sources.fast import rls
        from app.services.query_access_policy import validate_column_policy

        if rls_enabled or cls_enabled:
            self.ensure_access_policy_licensed()
            columns = await self._result_columns(db, query)
            if rls_enabled:
                try:
                    rls.validate_policy(
                        rls_policy, rls_mode or rls.MODE_ATTRIBUTE, columns
                    )
                except ValueError as e:
                    raise HTTPException(status_code=400, detail=str(e))
            if cls_enabled:
                try:
                    validate_column_policy(cls_policy, columns)
                except ValueError as e:
                    raise HTTPException(status_code=400, detail=str(e))

        query.rls_enabled = bool(rls_enabled)
        query.rls_mode = (rls_mode or "attribute") if rls_enabled else None
        query.rls_policy = rls_policy if rls_enabled else None
        query.rls_default_deny = bool(rls_default_deny)
        query.cls_enabled = bool(cls_enabled)
        query.cls_policy = cls_policy if cls_enabled else None
        await db.commit()
        await db.refresh(query)
        return query

    async def preview_access_policy(
        self,
        db: AsyncSession,
        query: Query,
        target_user_id: str,
        organization,
        *,
        overrides: Optional[dict] = None,
        current_user: Optional[User] = None,
    ) -> dict:
        """Show the rows and columns `target_user_id` would get, and why.

        The single best safeguard against a policy that reads correctly and
        behaves wrongly: the creator sees one specific person's slice before
        anyone depends on it. `overrides` applies an UNSAVED policy, so the
        check comes before the commit rather than after.

        This re-executes the step through the same executor and the same
        compiled policy an actual viewer's run would use. A separate
        "simulation" that applied the filter differently would be able to
        disagree with reality, which is the one thing a preview must not do.
        """
        from app.models.step import Step
        from app.models.user import User as UserModel
        from app.services.query_access_policy import compile_for_query
        from app.services.rls_identity_service import resolve_identity

        self.ensure_access_policy_licensed()

        target = (await db.execute(
            select(UserModel).where(UserModel.id == str(target_user_id))
        )).scalars().first()
        if target is None:
            raise HTTPException(status_code=404, detail="No such member")

        step_schema = await self.get_default_step_for_query(
            db, str(query.id),
            organization_id=str(organization.id) if organization else None,
        )
        if step_schema is None:
            raise HTTPException(
                status_code=400,
                detail="This query has no result yet — run it once first.",
            )
        step = (await db.execute(
            select(Step).where(Step.id == str(step_schema.id))
        )).scalar_one_or_none()
        if step is None:
            raise HTTPException(status_code=400, detail="This query has no runnable step.")

        report = (await db.execute(
            select(Report).where(Report.id == str(query.report_id))
        )).scalar_one_or_none()
        if report is None:
            raise HTTPException(status_code=400, detail="This query has no report.")

        identity = await resolve_identity(db, target, str(organization.id))
        # Apply unsaved edits to an in-memory stand-in, never to the row: a
        # mutate-and-rollback would hold until something else in the session
        # flushed first.
        probe = _PolicyProbe(query, overrides or {})
        policy = compile_for_query(probe, identity)

        from app.services.step_service import StepService

        try:
            data = await StepService()._execute_step_code(
                db, step, report,
                current_user=current_user,
                organization=organization,
                access_policy=policy,
            )
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Preview failed: {e}")

        rows = (data or {}).get("rows") or []
        columns = (data or {}).get("columns") or []
        row_filter = getattr(policy, "row_filter", None)
        mask = getattr(policy, "column_mask", None)
        return {
            "columns": columns,
            "rows": rows[:50],
            "row_count": len(rows),
            "filtered": bool(row_filter is not None and not row_filter.unrestricted),
            "denied": bool(row_filter is not None and row_filter.deny_all),
            "column": getattr(row_filter, "column", None),
            "allowed_values": list(getattr(row_filter, "allowed_values", None) or []),
            "reason": getattr(row_filter, "reason", "") or "no row policy",
            "masked_columns": list(getattr(mask, "masked", None) or []),
            "target_user": {
                "id": str(target.id),
                "name": getattr(target, "name", None) or target.email,
                "email": target.email,
            },
            "identity": {
                "email": identity.email,
                "groups": sorted(identity.group_names),
                "roles": sorted(identity.role_names),
                "profile_attributes": identity.profile_attributes or {},
            },
        }

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
