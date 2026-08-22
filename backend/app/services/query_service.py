from typing import Optional, List, Tuple
from types import SimpleNamespace
import copy
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import lazyload, selectinload

from app.models.query import Query
from app.models.widget import Widget
from app.models.report import Report
from app.models.user import User
from app.models.step import Step
from app.schemas.query_schema import QueryCreate, QuerySchema, QueryRunRequest
from app.schemas.step_schema import StepSchema
from app.schemas.param_schema import ParamSpec, parse_param_specs
from app.ai.code_execution.code_execution import StreamingCodeExecutor
from app.ai.code_execution.query_params import (
    ParamError,
    check_declarations_vs_code,
    params_fingerprint,
    resolve_param_values,
    verify_identity_binds_in_queries,
)
from app.dependencies import async_session_maker
from app.services.usage_policy_service import UsageLimitContext

def _enrich_step_schema(step_orm, step_schema: StepSchema) -> StepSchema:
    """Enrich StepSchema with relationship data from ORM"""
    if hasattr(step_orm, 'created_entity') and step_orm.created_entity:
        step_schema.created_entity_id = str(step_orm.created_entity.id)
    return step_schema


class QueryService:

    def __init__(self) -> None:
        pass

    # ------------------------------------------------------------------
    # Parameter helpers
    # ------------------------------------------------------------------

    async def _report_data_sources(self, db: AsyncSession, report, organization_id: Optional[str]):
        """Data sources whose clients a query run should construct.

        Prefer the report's own association; fall back to the org's active
        data sources — chat-created reports don't associate data sources with
        the report row (the agent works org-level), and generated code
        addresses clients by "<data source name>:<connection>" keys, so
        constructing the org set resolves the same keys."""
        ds_list = list(getattr(report, "data_sources", None) or [])
        if ds_list:
            return ds_list
        org_id = organization_id or getattr(report, "organization_id", None)
        if not org_id:
            return []
        from app.models.data_source import DataSource
        # Model-default loader options: construct_clients touches relationship
        # attributes (connections/credentials), which must arrive eagerly —
        # lazy attribute IO dies in async.
        stmt = select(DataSource).where(
            DataSource.organization_id == str(org_id),
            DataSource.deleted_at.is_(None),
        )
        return list((await db.execute(stmt)).scalars().unique().all())

    async def _resolve_identity_for(self, db: AsyncSession, user, organization_id: Optional[str]):
        """Full identity resolution (email, groups, profile attributes) via the
        single rls_identity_service authority. ANONYMOUS when no user."""
        from app.services.rls_identity_service import resolve_identity
        if user is None or not organization_id:
            from app.data_sources.fast.rls import ANONYMOUS
            return ANONYMOUS
        return await resolve_identity(db, user, str(organization_id))

    async def _resolve_run_as_user(
        self,
        db: AsyncSession,
        query: Query,
        caller: Optional[User],
        organization_id: Optional[str],
        run_as_user_id: Optional[str],
    ) -> Optional[User]:
        """'View as' authorization: only the report owner or an org admin may
        resolve identity params as another member. Returns the target User or
        raises ParamError."""
        if not run_as_user_id or (caller and str(run_as_user_id) == str(caller.id)):
            return None
        if caller is None:
            raise ParamError("view-as requires an authenticated caller")
        allowed = False
        if getattr(query, "report_id", None):
            owner_row = (await db.execute(
                select(Report.user_id).where(Report.id == str(query.report_id))
            )).first()
            if owner_row and str(owner_row[0]) == str(caller.id):
                allowed = True
        if not allowed and organization_id:
            from app.models.membership import Membership
            m = (await db.execute(
                select(Membership).where(
                    Membership.user_id == str(caller.id),
                    Membership.organization_id == str(organization_id),
                )
            )).scalars().first()
            if m and str(getattr(m, "role", "")) in ("admin", "owner"):
                allowed = True
        if not allowed:
            raise ParamError("view-as is limited to the report owner or an org admin")
        target = await db.get(User, str(run_as_user_id))
        if target is None:
            raise ParamError("view-as target user not found")
        # Target must be a member of the same org.
        if organization_id:
            from app.models.membership import Membership
            tm = (await db.execute(
                select(Membership.id).where(
                    Membership.user_id == str(target.id),
                    Membership.organization_id == str(organization_id),
                )
            )).first()
            if tm is None:
                raise ParamError("view-as target is not a member of this organization")
        return target

    async def _resolve_params_for_run(
        self,
        db: AsyncSession,
        specs: list[ParamSpec],
        request_params: Optional[dict],
        run_user: Optional[User],
        organization_id: Optional[str],
    ) -> dict:
        if not specs:
            if request_params:
                raise ParamError(
                    "this query declares no parameters but values were submitted"
                )
            return {}
        identity = await self._resolve_identity_for(db, run_user, organization_id)
        return resolve_param_values(specs, request_params, identity)

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
        stmt = (
            select(Query)
            .options(
                lazyload("*"),
                selectinload(Query.visualizations).options(lazyload("*")),
                selectinload(Query.default_step).options(
                    lazyload("*"),
                    selectinload(Step.created_entity).options(lazyload("*")),
                ),
            )
            .where(Query.id == str(query_id))
        )
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
        stmt = select(Query).options(
            lazyload("*"),
            selectinload(Query.visualizations).options(lazyload("*")),
            selectinload(Query.default_step).options(
                lazyload("*"),
                selectinload(Step.created_entity).options(lazyload("*")),
            ),
        )
        if report_id:
            stmt = stmt.where(Query.report_id == str(report_id))
        if organization_id:
            stmt = stmt.where(Query.organization_id == str(organization_id))

        # If artifact_id provided, filter to only queries used by that artifact
        if artifact_id:
            from app.models.artifact import Artifact
            from app.models.visualization import Visualization

            artifact_result = await db.execute(
                select(Artifact.content).where(
                    Artifact.id == artifact_id,
                    Artifact.deleted_at.is_(None)
                )
            )
            artifact_content = artifact_result.scalar_one_or_none()
            if artifact_content:
                visualization_ids = artifact_content.get("visualization_ids", [])
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
        if not (request.code or "").strip():
            raise ValueError("code is required for a builder run")
        # Load query & widget (scoped to the caller's org)
        q = await self.get_query(db, query_id, organization_id=organization_id)
        if not q:
            raise ValueError("Query not found")

        run_user_for_params = await db.get(User, user_id) if user_id else None

        # Parameter declarations: an explicit `parameters` list on the request
        # replaces the query's declared set (builder save from the Params
        # panel); otherwise the existing declarations apply. Declarations and
        # code must stay consistent — a declared-but-dead param would render a
        # control that does nothing.
        if request.parameters is not None:
            new_specs = [ParamSpec.model_validate(p) for p in (request.parameters or [])]
            consistency = check_declarations_vs_code(request.code or "", new_specs)
            if consistency:
                raise ParamError("; ".join(consistency))
            q.parameters = [s.model_dump() for s in new_specs]
            db.add(q)
            await db.commit()
            await db.refresh(q)
            specs = new_specs
        else:
            specs = parse_param_specs(getattr(q, "parameters", None))

        resolved_params = await self._resolve_params_for_run(
            db, specs, request.params, run_user_for_params, organization_id
        )

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
            applied_params=(resolved_params or None),
        )
        db.add(step)
        await db.commit()
        await db.refresh(step)

        # Execute code — load the report graph explicitly (lazy attribute
        # access on widget.report dies in async for freshly created widgets).
        report_stmt = (
            select(Report)
            .options(
                lazyload("*"),
                selectinload(Report.data_sources).options(lazyload("*")),
                selectinload(Report.files).options(lazyload("*")),
            )
            .join(Widget, Widget.report_id == Report.id)
            .where(Widget.id == str(q.widget_id))
        )
        report = (await db.execute(report_stmt)).scalar_one_or_none()
        if not report:
            raise ValueError("Report not found for step's widget")

        # Build ds_clients using construct_clients for multi-connection support.
        # Run as the user who triggered the run so user_required connections use
        # their credentials (or owner/admin → system-cred fallback).
        from app.services.data_source_service import DataSourceService
        ds_service = DataSourceService()
        run_user = await db.get(User, user_id) if user_id else None
        ds_clients = {}
        for ds in await self._report_data_sources(db, report, organization_id):
            try:
                ds_conns = await ds_service.construct_clients(db, ds, current_user=run_user)
                ds_clients.update(ds_conns)
            except Exception:
                continue
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
            captured_queries: list = []
            exec_df, execution_log, _ = await executor.execute_code_async(
                code=step.code,
                ds_clients=ds_clients,
                excel_files=excel_files,
                loadables=loadables,
                captured_queries=captured_queries,
                params=resolved_params,
            )
            identity_err = verify_identity_binds_in_queries(
                captured_queries, resolved_params, specs
            )
            if identity_err:
                raise ParamError(identity_err)
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

    async def run_query_viewer(
        self,
        db: AsyncSession,
        query_id: str,
        request: QueryRunRequest,
        organization_id: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> dict:
        """Viewer-execute: run the query's default step code with param values.

        Creates NO new Step. The result is cached per
        (step, viewer, params_fingerprint) on step_user_results, so a repeat
        request with the same values never re-executes. Identity params are
        resolved server-side from the caller's session (or the audited
        view-as target); identity-locked values submitted by the client are
        rejected upstream in resolve_param_values.
        """
        q = await self.get_query(db, query_id, organization_id=organization_id)
        if not q:
            raise ValueError("Query not found")

        # Resolve the default step (the version dashboards render).
        step = None
        if q.default_step_id:
            step = await db.get(Step, str(q.default_step_id))
        if step is None:
            res = await db.execute(
                select(Step)
                .where(Step.widget_id == str(q.widget_id), Step.status == "success")
                .order_by(Step.created_at.desc())
            )
            step = res.scalars().first()
        if step is None or not (step.code or "").strip():
            raise ValueError("Query has no runnable step")

        caller = await db.get(User, user_id) if user_id else None
        if caller is None:
            raise ParamError("viewer run requires an authenticated user")

        specs = parse_param_specs(getattr(q, "parameters", None))

        # View-as: identity params resolve as the target member; results are
        # cached under the CALLER's user_id so the target's own cache is never
        # touched by a preview.
        run_as = await self._resolve_run_as_user(
            db, q, caller, organization_id, request.run_as_user_id
        )
        identity_user = run_as or caller

        identity = await self._resolve_identity_for(db, identity_user, organization_id)
        resolved = resolve_param_values(specs, request.params, identity)
        fingerprint = params_fingerprint(resolved)
        if run_as is not None:
            # Distinct cache slot per impersonated identity, still keyed to
            # the caller.
            fingerprint = params_fingerprint({**resolved, "__view_as__": str(run_as.id)})

        from app.models.step_user_result import StepUserResult
        from datetime import datetime as _dt

        existing = (await db.execute(
            select(StepUserResult).where(
                StepUserResult.step_id == str(step.id),
                StepUserResult.user_id == str(caller.id),
                StepUserResult.params_fingerprint == fingerprint,
            )
        )).scalars().first()

        step_updated = getattr(step, "updated_at", None)
        cache_fresh = (
            existing is not None
            and existing.status == "success"
            and not request.force_refresh
            and (
                step_updated is None
                or existing.last_run_at is None
                or existing.last_run_at >= step_updated
            )
        )
        if cache_fresh:
            return {
                "data": existing.data or {},
                "applied_params": resolved,
                "cached": True,
                "status": "success",
                "step_id": str(step.id),
            }

        # Execute the default step's code with the resolved values, as the
        # caller (their credentials on user-scoped connections). Load the
        # report graph explicitly — lazy attribute access dies in async.
        report_stmt = (
            select(Report)
            .options(
                lazyload("*"),
                selectinload(Report.data_sources).options(lazyload("*")),
                selectinload(Report.files).options(lazyload("*")),
            )
            .join(Widget, Widget.report_id == Report.id)
            .where(Widget.id == str(step.widget_id))
        )
        report = (await db.execute(report_stmt)).scalar_one_or_none()
        if report is None:
            raise ValueError("Report not found for step's widget")

        # Whose CREDENTIALS execute: mirror the viewer-rerun policy.
        # 'creator' share mode runs under the report owner's credentials (the
        # personalization tier: identity params still bind the CALLER); RLS
        # relations force viewer credentials; owners always run as themselves.
        credential_user = caller
        identity_mode = report.shared_run_identity if report.shared_run_identity in ('viewer', 'creator') else 'viewer'
        if identity_mode == 'creator' and str(caller.id) != str(report.user_id):
            from app.services.viewer_data_policy import has_rls_relations
            if await has_rls_relations(db, str(report.id)):
                identity_mode = 'viewer'
        if identity_mode == 'creator' and str(caller.id) != str(report.user_id):
            from app.models.membership import Membership
            member = (await db.execute(
                select(Membership).where(
                    Membership.user_id == str(caller.id),
                    Membership.organization_id == str(report.organization_id),
                )
            )).scalar_one_or_none()
            if member is not None:
                owner = await db.get(User, str(report.user_id))
                if owner is not None:
                    credential_user = owner

        from app.services.data_source_service import DataSourceService
        ds_service = DataSourceService()
        ds_clients = {}
        ds_errors = []
        for ds in await self._report_data_sources(db, report, organization_id):
            try:
                ds_conns = await ds_service.construct_clients(db, ds, current_user=credential_user)
                ds_clients.update(ds_conns)
            except Exception as e:
                ds_errors.append(str(getattr(e, "detail", None) or e))
                continue
        if not ds_clients and ds_errors:
            raise ParamError("; ".join(ds_errors[:2]))

        from app.ai.code_execution.loadables import resolve_loadables_for_code, load_step_settings
        from app.models.organization import Organization
        org = await db.get(Organization, str(organization_id or report.organization_id)) \
            if (organization_id or getattr(report, "organization_id", None)) else None
        org_settings = await org.get_settings(db) if org else None
        _ls_enabled, _ = load_step_settings(org_settings)
        loadables = await resolve_loadables_for_code(
            db, org, report, caller, step.code, enable_load_step=_ls_enabled
        )
        usage_context = self._usage_context(
            organization_id, str(caller.id), source="query_viewer_run", source_ref_id=query_id
        )
        executor = StreamingCodeExecutor(
            organization_settings=org_settings, usage_context=usage_context
        )
        captured_queries: list = []
        try:
            exec_df, execution_log, _ = await executor.execute_code_async(
                code=step.code,
                ds_clients=ds_clients,
                excel_files=report.files,
                loadables=loadables,
                captured_queries=captured_queries,
                params=resolved,
            )
            identity_err = verify_identity_binds_in_queries(
                captured_queries, resolved, specs
            )
            if identity_err:
                raise ParamError(identity_err)
            df = executor.format_df_for_widget(exec_df)
            status, status_reason = "success", None
        except ParamError:
            raise
        except Exception as e:
            df, status, status_reason = None, "error", str(e)
        finally:
            if usage_context is not None:
                try:
                    await usage_context.flush()
                except Exception:
                    pass

        # Upsert the per-viewer cached result. Errors are not cached — a
        # failed run must not pin a viewer to an error until force_refresh.
        if status == "success":
            if existing is None:
                existing = StepUserResult(
                    step_id=str(step.id),
                    user_id=str(caller.id),
                    organization_id=str(organization_id or report.organization_id),
                    report_id=str(report.id),
                    params_fingerprint=fingerprint,
                )
            existing.status = "success"
            existing.status_reason = None
            existing.data = df
            existing.applied_params = dict(resolved) if resolved else None
            existing.executed_as = "viewer"
            existing.last_run_at = _dt.utcnow()
            db.add(existing)
            await db.commit()
            return {
                "data": df or {},
                "applied_params": resolved,
                "cached": False,
                "status": "success",
                "step_id": str(step.id),
            }
        return {
            "data": {},
            "applied_params": resolved,
            "cached": False,
            "status": "error",
            "error": status_reason,
            "step_id": str(step.id),
        }

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
        # This path needs the widget's report plus its execution inputs. Keep
        # the general query read projection narrow, and opt into only that
        # relationship graph here so async code never falls back to lazy IO.
        stmt = select(Query).options(
            lazyload("*"),
            selectinload(Query.widget).options(
                lazyload("*"),
                selectinload(Widget.report).options(
                    lazyload("*"),
                    selectinload(Report.data_sources).options(lazyload("*")),
                    selectinload(Report.files).options(lazyload("*")),
                ),
            ),
        ).where(Query.id == str(query_id))
        if organization_id:
            stmt = stmt.where(Query.organization_id == str(organization_id))
        q = (await db.execute(stmt)).scalar_one_or_none()
        if not q:
            raise ValueError("Query not found")

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
        for ds in await self._report_data_sources(db, report, organization_id):
            try:
                ds_conns = await ds_service.construct_clients(db, ds, current_user=run_user)
                ds_clients.update(ds_conns)
            except Exception:
                continue
        excel_files = report.files
        usage_context = self._usage_context(organization_id, user_id, source="query_preview", source_ref_id=query_id)
        # Pass organization_settings so widget serialization honors the org's
        # limit_row_count instead of falling back to the hardcoded 1000-row cap.
        from app.models.organization import Organization
        org = await db.get(Organization, str(organization_id or report.organization_id)) if (organization_id or getattr(report, "organization_id", None)) else None
        org_settings = await org.get_settings(db) if org else None
        executor = StreamingCodeExecutor(organization_settings=org_settings, usage_context=usage_context)

        # Params: an explicit declarations list on the request is validated
        # against the code (same rule as a builder save); otherwise the
        # query's stored declarations apply.
        if request.parameters is not None:
            preview_specs = [ParamSpec.model_validate(p) for p in (request.parameters or [])]
            consistency = check_declarations_vs_code(request.code or "", preview_specs)
            if consistency:
                raise ParamError("; ".join(consistency))
        else:
            preview_specs = parse_param_specs(getattr(q, "parameters", None))
        resolved_params = await self._resolve_params_for_run(
            db, preview_specs, request.params, run_user, organization_id
        )

        try:
            exec_df, execution_log, _ = await executor.execute_code_async(
                code=request.code or "",
                ds_clients=ds_clients,
                excel_files=excel_files,
                params=resolved_params,
            )
            df = executor.format_df_for_widget(exec_df)
            return {"preview": df, "execution_log": execution_log, "applied_params": resolved_params}
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
