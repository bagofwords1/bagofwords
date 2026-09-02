from typing import List, Optional

from sqlalchemy import select, or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.entity import Entity, entity_data_source_association
from app.models.data_source import DataSource
from app.models.user import User
from app.models.organization import Organization
from app.models.step import Step
from app.models.query import Query
from app.services.step_service import StepService
from app.services.query_service import QueryService
from app.schemas.entity_schema import EntityCreate, EntityUpdate
from datetime import datetime
from app.schemas.entity_schema import EntityRunPayload
from app.core.telemetry import telemetry
from app.ee.audit.service import audit_service


class EntityService:

    def __init__(self):
        self.step_service = StepService()
        self.query_service = QueryService()

    # ------------------------------------------------------------------
    # Parameters (mirrors the step/query machinery)
    # ------------------------------------------------------------------

    async def _resolve_entity_params(
        self, db: AsyncSession, entity, run_user, organization,
        request_values: Optional[dict] = None,
    ) -> dict:
        """Resolve the entity's declared params: defaults <- request values <-
        the run user's identity bindings. {} when the entity declares none.

        `request_values` are caller-supplied VALUES ({name: value}). Unknown
        names and client values for identity-locked params are rejected
        (ParamError), exactly like a query viewer run."""
        from app.schemas.param_schema import parse_param_specs
        from app.ai.code_execution.query_params import ParamError, resolve_param_values
        specs = parse_param_specs(getattr(entity, "parameters", None))
        if not specs:
            if request_values:
                raise ParamError(
                    "this entity declares no parameters; "
                    f"unexpected: {', '.join(sorted(map(str, request_values)))}"
                )
            return {}
        identity = None
        if run_user is not None and organization is not None:
            from app.services.rls_identity_service import resolve_identity
            identity = await resolve_identity(db, run_user, str(organization.id))
        return resolve_param_values(specs, request_values or None, identity)

    async def _upsert_entity_user_result(
        self, db: AsyncSession, entity, user, resolved_params: dict, df: dict
    ) -> None:
        """Cache a viewer's slice per (entity, user, values fingerprint)."""
        from app.ai.code_execution.query_params import params_fingerprint
        from app.models.entity_user_result import EntityUserResult
        fingerprint = params_fingerprint(resolved_params)
        row = (await db.execute(
            select(EntityUserResult).where(
                EntityUserResult.entity_id == str(entity.id),
                EntityUserResult.user_id == str(user.id),
                EntityUserResult.params_fingerprint == fingerprint,
            )
        )).scalars().first()
        if row is None:
            row = EntityUserResult(
                entity_id=str(entity.id),
                user_id=str(user.id),
                organization_id=str(entity.organization_id),
                params_fingerprint=fingerprint,
            )
        row.status = "success"
        row.status_reason = None
        row.data = df
        row.applied_params = dict(resolved_params) if resolved_params else None
        row.last_run_at = datetime.utcnow()
        db.add(row)
        await db.commit()

    async def resolve_entity_data_for_user(
        self,
        db: AsyncSession,
        entity,
        organization,
        user,
    ) -> Optional[dict]:
        """The entity rows a given reader may consume (load_entity, previews).

        - Owner (or a non-identity, non-withheld entity): the shared snapshot.
        - Identity-scoped entity + another signed-in user: their cached slice
          (fresh = run after the shared snapshot's last refresh), else execute
          with THEIR identity binding and cache it.
        - Anonymous readers of identity-scoped entities: None (withheld).
        """
        from app.services.identity_taint import entity_identity_scope
        has_identity, _upstream = await entity_identity_scope(db, entity)
        owner_id = str(getattr(entity, "owner_id", "") or "")
        is_owner = user is not None and owner_id and str(user.id) == owner_id
        if not has_identity or is_owner:
            from app.services.viewer_data_policy import entity_data_withheld
            if await entity_data_withheld(db, entity, user):
                return None
            return entity.data or {}
        if user is None:
            return None
        result = await self.run_entity_for_user(db, entity, organization, user)
        return result["data"]

    async def run_entity_for_user(
        self,
        db: AsyncSession,
        entity,
        organization,
        user,
        request_values: Optional[dict] = None,
        *,
        force_refresh: bool = False,
    ) -> dict:
        """Viewer-execute a saved entity with parameter VALUES, as `user`.

        This is the no-LLM path for "load a saved query with parameters":
        the entity's stored code runs with defaults <- `request_values` <-
        the caller's identity bindings, and the result is cached per
        (entity, user, values fingerprint) in entity_user_results — a repeat
        request with the same values never re-executes until the shared
        snapshot (or a loadable upstream) refreshes past it, or
        `force_refresh` is set. The shared Entity.data snapshot is never
        touched here.

        Returns {"data": grid, "applied_params": resolved, "cached": bool}.
        Raises ParamError for bad values and ValueError when no data-source
        client can be built for the caller.
        """
        from app.ai.code_execution.query_params import ParamError, params_fingerprint
        from app.models.entity_user_result import EntityUserResult
        from app.services.identity_taint import entity_identity_scope

        if user is None:
            raise ParamError("running an entity with parameters requires an authenticated user")

        resolved = await self._resolve_entity_params(
            db, entity, user, organization, request_values
        )
        fingerprint = params_fingerprint(resolved)
        row = (await db.execute(
            select(EntityUserResult).where(
                EntityUserResult.entity_id == str(entity.id),
                EntityUserResult.user_id == str(user.id),
                EntityUserResult.params_fingerprint == fingerprint,
            )
        )).scalars().first()
        # Stale once the entity OR any of its loadable upstreams refreshed
        # past the cached slice.
        _has_identity, upstream_refresh = await entity_identity_scope(db, entity)
        refreshed_at = getattr(entity, "last_refreshed_at", None)
        if upstream_refresh is not None and (
            refreshed_at is None or upstream_refresh > refreshed_at
        ):
            refreshed_at = upstream_refresh
        if (
            not force_refresh
            and row is not None and row.status == "success"
            and (refreshed_at is None or row.last_run_at is None or row.last_run_at >= refreshed_at)
        ):
            return {"data": row.data or {}, "applied_params": resolved, "cached": True}

        # Execute with the caller's credentials and identity binding, then
        # cache the slice.
        from app.ai.code_execution.code_execution import StreamingCodeExecutor
        from app.services.data_source_service import DataSourceService
        ds_service = DataSourceService()
        ds_list = list(entity.data_sources or [])
        if not ds_list:
            # DS-less entities (promoted from chat-created reports whose data
            # sources were agent-level, not report-associated): construct the
            # org's data sources — generated code addresses clients by
            # "<data source name>:<connection>" keys, so this resolves them.
            ds_stmt = select(DataSource).where(
                DataSource.organization_id == str(entity.organization_id),
                DataSource.deleted_at.is_(None),
            )
            ds_list = list((await db.execute(ds_stmt)).scalars().unique().all())
        ds_clients: dict = {}
        ds_errors: list = []
        for ds in ds_list:
            try:
                ds_conns = await ds_service.construct_clients(db, ds, current_user=user)
                ds_clients.update(ds_conns)
            except Exception as e:
                ds_errors.append(str(getattr(e, "detail", None) or e))
        if not ds_clients and ds_errors:
            raise ValueError("; ".join(ds_errors[:2]))
        org_settings = await organization.get_settings(db) if organization else None
        executor = StreamingCodeExecutor(organization_settings=org_settings)
        exec_df, _log, _ = await executor.execute_code_async(
            code=entity.code or "", ds_clients=ds_clients, excel_files=[],
            params=resolved,
        )
        df = executor.format_df_for_widget(exec_df)
        await self._upsert_entity_user_result(db, entity, user, resolved, df)
        return {"data": df, "applied_params": resolved, "cached": False}


    async def step_report_data_source_ids(
        self, db: AsyncSession, step_id: str, organization: Organization,
    ) -> List[str]:
        """Data source ids the entity WOULD attach when created from this step
        with no explicit override: the step report's data sources, falling
        back to the data sources @-mentioned in the report's chat (a report
        whose agent was attached via mention has no report-level association
        rows, yet that agent is what the step's query ran against). Lets the
        route run its permission checks against the exact same set."""
        from app.models.report import Report

        result = await db.execute(
            select(Step)
            .options(selectinload(Step.query).selectinload(Query.report).selectinload(Report.data_sources))
            .where(Step.id == str(step_id))
        )
        step = result.scalar_one_or_none()
        if not step or not step.query or not step.query.report:
            return []
        report = step.query.report
        if str(report.organization_id) != str(organization.id):
            return []
        ds_ids = list({str(ds.id) for ds in (report.data_sources or [])})
        if ds_ids:
            return ds_ids
        from app.models.mention import Mention, MentionType
        rows = await db.execute(
            select(Mention.object_id).where(
                Mention.report_id == str(report.id),
                Mention.type == MentionType.DATA_SOURCE,
            ).distinct()
        )
        return [str(r[0]) for r in rows.all()]

    async def create_entity_from_step(
        self,
        db: AsyncSession,
        step_id: str,
        current_user: User,
        organization: Organization,
        *,
        type_override: Optional[str] = None,
        title_override: Optional[str] = None,
        slug_override: Optional[str] = None,
        description_override: Optional[str] = None,
        publish: bool = False,
        data_source_ids_override: Optional[List[str]] = None,
        creator_can_publish: Optional[bool] = None,
    ) -> Entity:
        """Create an Entity from a successful Step.

        `creator_can_publish` is the route's per-DS `create_entities` verdict:
        True → the dual-status admin branch (approved, optionally published);
        False → suggestion pending approval. None (legacy callers) falls back
        to the org-admin check.
        """
        # Load step with query -> report (for data sources)
        from sqlalchemy import select
        from sqlalchemy.orm import selectinload
        from app.models.report import Report
        from app.models.visualization import Visualization

        result = await db.execute(
            select(Step)
            .options(selectinload(Step.query).selectinload(Query.report).selectinload(Report.data_sources))
            .where(Step.id == str(step_id))
        )
        step = result.scalar_one_or_none()
        if not step:
            raise ValueError("Step not found")
        if not step.query or not step.query.report:
            raise ValueError("Step is not linked to a query/report")
        if str(step.query.report.organization_id) != str(organization.id):
            raise ValueError("Step does not belong to this organization")
        if step.status != "success":
            raise ValueError("Only successful steps can be saved as entities")

        # Prefer a visualization for the step's query to source view
        chosen_view = None
        if getattr(step, "query_id", None):
            viz_rows = await db.execute(
                select(Visualization).where(Visualization.query_id == str(step.query_id)).order_by(Visualization.created_at.asc())
            )
            vlist = viz_rows.scalars().all()
            if vlist:
                chosen = next((v for v in vlist if getattr(v, "status", None) == "success"), vlist[0])
                chosen_view = getattr(chosen, "view", None)

        # Compute fields with overrides
        title = (title_override or step.title or "Untitled").strip()
        slug = (slug_override or step.slug or title.lower().replace(" ", "-")).strip()
        description = description_override if description_override is not None else (step.description or None)
        ent_type = (type_override or ("metric" if (chosen_view or {}).get("type") == "count" else "model"))

        # Publish tier: the route's per-DS `create_entities` verdict when given,
        # else the org-admin fallback (legacy callers).
        if creator_can_publish is None:
            user_permissions = await self._get_user_permissions(db, current_user, organization)
            creator_can_publish = self._is_admin_permissions(user_permissions)

        # Copy the step rows THROUGH the viewer-data policy, not Step.data
        # directly: on a user-scoped (user_required/RLS) source the shared
        # snapshot is the report owner's row slice, and a non-owner saving the
        # step must not mint an entity carrying another identity's rows. The
        # resolution returns the creator's own slice, or {} when withheld.
        from app.services.viewer_data_policy import resolve_step_data
        resolution = await resolve_step_data(db, step, step.query.report, current_user)

        entity = Entity(
            organization_id=str(organization.id),
            owner_id=str(current_user.id),
            type=ent_type,
            title=title,
            slug=slug,
            description=description,
            tags=[],
            code=step.code or "",
            data=resolution.data or {},
            original_data_model=step.data_model or {},
            view=(chosen_view or getattr(step, "view", None) or {"type": "table"}),
            last_refreshed_at=step.updated_at,
            source_step_id=str(step_id),  # Link back to source step
            # Declarations travel with the promotion: the entity stays
            # parameterized (identity-scoped entities resolve per viewer),
            # and the promoted snapshot's values are recorded.
            parameters=list(getattr(step.query, "parameters", None) or []) or None,
            applied_params=getattr(step, "applied_params", None),
        )

        # Apply dual-status workflow based on the publish verdict
        if creator_can_publish:
            # Entity manager for these agents - respect their publish choice
            entity.private_status = None
            entity.global_status = "approved"
            entity.status = "published" if publish else "draft"
            entity.published_at = datetime.utcnow() if publish else None
        else:
            # Regular users create suggested entities (pending admin approval)
            entity.private_status = "published"
            entity.global_status = "suggested"
            entity.status = "draft"
            entity.published_at = None

        db.add(entity)
        # Link data sources - use override if provided, otherwise use report data sources
        ds_ids: list[str] = []
        if data_source_ids_override is not None and len(data_source_ids_override) > 0:
            ds_ids = list({str(i) for i in data_source_ids_override})
        else:
            # Fall back to report data sources
            report_ds = list((step.query.report.data_sources or []))
            if report_ds:
                ds_ids = list({str(ds.id) for ds in report_ds})

        if ds_ids:
            # Insert association rows explicitly to avoid async lazy-load on relationship set
            from sqlalchemy import insert
            # Ensure the entity has an ID before inserting into association table
            await db.flush()
            rows = [
                {"entity_id": str(entity.id), "data_source_id": ds_id}
                for ds_id in ds_ids
            ]
            if rows:
                await db.execute(insert(entity_data_source_association), rows)

        await db.flush()
        await db.commit()
        await db.refresh(entity)
        # Telemetry: entity created from step (minimal fields only)
        try:
            await telemetry.capture(
                "entity_created",
                {
                    "entity_id": str(entity.id),
                    "type": entity.type,
                    "status": entity.status,
                    "title_char_length": len((entity.title or "").strip()),
                    "num_data_sources": len(ds_ids or []),
                },
                user_id=current_user.id,
                org_id=organization.id,
            )
        except Exception:
            pass

        # Audit log
        try:
            await audit_service.log(
                db=db,
                organization_id=str(organization.id),
                action="entity.created",
                user_id=str(current_user.id),
                resource_type="entity",
                resource_id=str(entity.id),
                details={"type": entity.type, "title": entity.title, "status": entity.status},
            )
        except Exception:
            pass

        # Bidirectional relationship is automatically maintained by SQLAlchemy
        # through entity.source_step_id - no need to manually set step.created_entity_id
        
        return entity


    async def create_entity(
        self,
        db: AsyncSession,
        payload: EntityCreate,
        current_user: User,
        organization: Organization,
    ) -> Entity:
        entity = Entity(
            organization_id=str(organization.id),
            owner_id=str(current_user.id),
            type=payload.type,
            title=payload.title,
            slug=payload.slug,
            description=payload.description,
            tags=payload.tags,
            code=payload.code,
            data=payload.data,
            view=(payload.view.model_dump() if payload.view else None),
            status=payload.status,
            published_at=payload.published_at,
            last_refreshed_at=payload.last_refreshed_at,
            # Declared ParamSpecs + the values `data` was produced with; the
            # schema already carried them, the row just never stored them.
            parameters=list(payload.parameters or []) or None,
            applied_params=dict(payload.applied_params or {}) or None,
        )
        db.add(entity)
        if payload.data_source_ids:
            from sqlalchemy import insert
            await db.flush()  # Ensure entity has an ID
            rows = [
                {"entity_id": str(entity.id), "data_source_id": str(ds_id)}
                for ds_id in payload.data_source_ids
            ]
            if rows:
                await db.execute(insert(entity_data_source_association), rows)
        await db.flush()
        await db.commit()
        await db.refresh(entity)
        # Telemetry: entity created (payload)
        try:
            await telemetry.capture(
                "entity_created",
                {
                    "entity_id": str(entity.id),
                    "type": entity.type,
                    "status": entity.status,
                    "title_char_length": len((entity.title or "").strip()),
                    "num_data_sources": len(payload.data_source_ids or []),
                },
                user_id=current_user.id,
                org_id=organization.id,
            )
        except Exception:
            pass

        # Audit log
        try:
            await audit_service.log(
                db=db,
                organization_id=str(organization.id),
                action="entity.created",
                user_id=str(current_user.id),
                resource_type="entity",
                resource_id=str(entity.id),
                details={"type": entity.type, "title": entity.title, "status": entity.status},
            )
        except Exception:
            pass

        return entity

    async def list_entities(
        self,
        db: AsyncSession,
        organization: Organization,
        current_user: User,
        *,
        q: Optional[str] = None,
        type: Optional[str] = None,
        owner_id: Optional[str] = None,
        data_source_ids: Optional[List[str]] = None,
        skip: int = 0,
        limit: int = 100,
    ) -> List[Entity]:
        # Get user's accessible data sources
        from sqlalchemy import exists, and_
        from app.core.permission_resolver import get_accessible_data_source_ids

        is_admin, accessible_ids = await get_accessible_data_source_ids(
            db, str(current_user.id), str(organization.id)
        )
        accessible_ds_subquery = (
            select(DataSource.id).filter(DataSource.organization_id == organization.id)
        )
        if not is_admin:
            clauses = [DataSource.is_public == True]
            if accessible_ids:
                clauses.append(DataSource.id.in_(accessible_ids))
            accessible_ds_subquery = accessible_ds_subquery.filter(or_(*clauses))
        
        # Subquery to check if entity has any inaccessible data sources
        has_inaccessible_ds = exists(
            select(1)
            .select_from(entity_data_source_association)
            .where(
                and_(
                    entity_data_source_association.c.entity_id == Entity.id,
                    entity_data_source_association.c.data_source_id.notin_(accessible_ds_subquery)
                )
            )
        )
        
        # Base query: show entities where user has access to ALL data sources
        # (i.e., entities that don't have any inaccessible data sources)
        stmt = (
            select(Entity)
            .where(Entity.organization_id == str(organization.id))
            .where(Entity.deleted_at == None)
            .where(~has_inaccessible_ds)  # Exclude entities with any inaccessible data sources
        )
        
        if type:
            stmt = stmt.where(Entity.type == type)
        if owner_id:
            stmt = stmt.where(Entity.owner_id == owner_id)
        if q:
            like = f"%{q}%"
            stmt = stmt.where(or_(Entity.title.ilike(like), Entity.slug.ilike(like)))
        if data_source_ids:
            # Filter to entities that have any of the specified domain IDs
            stmt = stmt.where(
                exists(
                    select(1)
                    .select_from(entity_data_source_association)
                    .where(
                        and_(
                            entity_data_source_association.c.entity_id == Entity.id,
                            entity_data_source_association.c.data_source_id.in_(data_source_ids)
                        )
                    )
                )
            )
        
        stmt = stmt.order_by(Entity.updated_at.desc()).offset(skip).limit(limit)
        result = await db.execute(stmt)
        return list(result.scalars().all())

    async def get_entity(
        self,
        db: AsyncSession,
        entity_id: str,
        organization: Organization,
        current_user: Optional[User] = None,
    ) -> Optional[Entity]:
        result = await db.execute(
            select(Entity)
            .options(selectinload(Entity.data_sources))
            .where(Entity.id == entity_id, Entity.organization_id == str(organization.id))
        )
        entity = result.scalar_one_or_none()
        
        if not entity or not current_user:
            return entity
        
        # Check if user has access to all data sources of this entity
        if entity.data_sources:
            from app.core.permission_resolver import user_can_access_data_source
            for ds in entity.data_sources:
                if not await user_can_access_data_source(
                    db, str(current_user.id), str(organization.id), ds
                ):
                    return None
        
        return entity

    async def update_entity(
        self,
        db: AsyncSession,
        entity_id: str,
        payload: EntityUpdate,
        organization: Organization,
        current_user: User,
        *,
        resource_authorized: bool = False,
    ) -> Optional[Entity]:
        """`resource_authorized=True` means the route verified the caller holds
        per-DS `create_entities` on every attached data source (agent
        owner/manager tier) — they get full edit control like an org admin,
        but review transitions (approve/reject) stay org-admin only."""
        result = await db.execute(select(Entity).where(Entity.id == entity_id, Entity.organization_id == str(organization.id)))
        entity = result.scalar_one_or_none()
        if not entity:
            return None

        # Get user permissions
        user_permissions = await self._get_user_permissions(db, current_user, organization)
        from fastapi import HTTPException
        if not user_permissions:
            raise HTTPException(status_code=403, detail="Permission denied: not an organization member")

        # Determine what type of update this is and check permissions
        from app.models.entity import Entity as EntityModel
        update_type = self._determine_update_type(
            entity, payload, current_user, user_permissions,
            resource_authorized=resource_authorized,
        )
        
        # Handle the update based on type
        if update_type == "admin_review":
            await self._handle_admin_review(entity, payload, current_user)
        elif update_type == "admin_edit":
            await self._handle_admin_edit(entity, payload, current_user)
        elif update_type == "owner_edit":
            await self._handle_owner_edit(entity, payload)
        else:
            raise HTTPException(status_code=403, detail="Permission denied")

        # Handle data source associations
        if payload.data_source_ids is not None:
            if payload.data_source_ids:
                result = await db.execute(select(DataSource).where(DataSource.id.in_(payload.data_source_ids)))
                entity.data_sources = list(result.scalars().all())
            else:
                entity.data_sources = []

        await db.flush()
        await db.commit()
        await db.refresh(entity)

        # Audit log
        try:
            await audit_service.log(
                db=db,
                organization_id=str(organization.id),
                action="entity.updated",
                user_id=str(current_user.id),
                resource_type="entity",
                resource_id=str(entity.id),
                details={"title": entity.title, "status": entity.status, "update_type": update_type},
            )
        except Exception:
            pass

        return entity

    async def delete_entity(
        self,
        db: AsyncSession,
        entity_id: str,
        organization: Organization,
        current_user: User = None,
    ) -> bool:
        result = await db.execute(select(Entity).where(Entity.id == entity_id, Entity.organization_id == str(organization.id)))
        entity = result.scalar_one_or_none()
        if not entity:
            return False

        # Capture details before deletion for audit
        entity_title = entity.title

        await db.delete(entity)
        await db.commit()

        # Audit log
        try:
            await audit_service.log(
                db=db,
                organization_id=str(organization.id),
                action="entity.deleted",
                user_id=str(current_user.id) if current_user else None,
                resource_type="entity",
                resource_id=str(entity_id),
                details={"title": entity_title},
            )
        except Exception:
            pass

        return True

    async def run_entity_with_update(
        self,
        db: AsyncSession,
        entity_id: str,
        payload: EntityRunPayload,
        organization: Organization,
        current_user: Optional[User] = None,
    ) -> Entity:
        """Execute the entity's code, update its data/view/metadata, and persist."""
        # Load entity scoped to organization
        result = await db.execute(select(Entity).where(Entity.id == str(entity_id), Entity.organization_id == str(organization.id)))
        entity = result.scalar_one_or_none()
        if not entity:
            raise ValueError("Entity not found")

        # Parameter VALUES make this a viewer-mode run: execute the SAVED code
        # with those values as the caller, serve/cache per (entity, user,
        # values), and hand back a detached copy carrying that slice. The
        # shared snapshot (defaults + owner identity) is never rewritten by a
        # values run — that is what keeps load_entity/describe_entity stable.
        request_values = dict(getattr(payload, "params", None) or {}) if payload else {}
        if request_values:
            result = await self.run_entity_for_user(
                db, entity, organization, current_user, request_values,
                force_refresh=bool(getattr(payload, "force_refresh", False)),
            )
            db.expunge(entity)
            entity.data = result["data"]
            entity.applied_params = result["applied_params"]
            return entity

        # Determine code to run (payload override or stored)
        code_to_run = (payload.code if (payload and getattr(payload, "code", None) is not None) else entity.code) or ""

        # Resolve report/data sources context via any linked data sources on the entity
        # When entities are not tied to a report, we execute with all entity data sources
        from app.ai.code_execution.code_execution import StreamingCodeExecutor
        from app.services.data_source_service import DataSourceService
        ds_service = DataSourceService()
        ds_list = list(entity.data_sources or [])
        if not ds_list:
            # DS-less entities (promoted from chat-created reports): fall back
            # to the org's data sources — generated code addresses clients by
            # "<data source name>:<connection>" keys.
            _ds_stmt = select(DataSource).where(
                DataSource.organization_id == str(organization.id),
                DataSource.deleted_at.is_(None),
            )
            ds_list = list((await db.execute(_ds_stmt)).scalars().unique().all())
        ds_clients = {}
        for ds in ds_list:
            ds_conns = await ds_service.construct_clients(db, ds, current_user=current_user)
            ds_clients.update(ds_conns)
        excel_files = []

        # Pass organization_settings so widget serialization honors the org's
        # limit_row_count instead of falling back to the hardcoded 1000-row cap.
        org_settings = await organization.get_settings(db) if organization else None
        executor = StreamingCodeExecutor(organization_settings=org_settings)

        # Snapshot-identity guard: execution runs under the CALLER's
        # credentials, but Entity.data is a SHARED snapshot the policy treats
        # as the owner's identity. On a credential-differentiated source
        # (user_required/RLS) — or an identity-parameterized entity — a
        # non-owner refresh must not overwrite the shared snapshot with their
        # own row slice. entity_data_withheld is exactly that predicate
        # (False for the owner and for system-only, non-identity entities).
        from app.services.viewer_data_policy import entity_data_withheld
        persist_data = not await entity_data_withheld(db, entity, current_user)

        # Declared parameters resolve like a step run: defaults + the RUN
        # user's identity bindings. Legacy code (no `params` argument) is
        # unaffected — the executor only injects when the signature asks.
        resolved_params = await self._resolve_entity_params(
            db, entity, current_user, organization
        )

        try:
            exec_df, execution_log, _ = executor.execute_code(
                code=code_to_run, ds_clients=ds_clients, excel_files=excel_files,
                params=resolved_params,
            )
            df = executor.format_df_for_widget(exec_df)
            if not persist_data:
                # Transient run: the caller's own slice. Cache it per
                # (entity, user, values) so load_entity and later reads reuse
                # it, then detach the instance so the shared snapshot is
                # never flushed.
                if current_user is not None:
                    await self._upsert_entity_user_result(
                        db, entity, current_user, resolved_params, df
                    )
                db.expunge(entity)
                entity.data = df
                return entity
            # Persist execution results
            entity.data = df
            entity.applied_params = dict(resolved_params) if resolved_params else None
            entity.last_refreshed_at = datetime.utcnow()

            # Apply optional payload updates
            if payload:
                if getattr(payload, "title", None) is not None:
                    entity.title = payload.title  # type: ignore
                if getattr(payload, "description", None) is not None:
                    entity.description = payload.description  # type: ignore
                if getattr(payload, "type", None) is not None:
                    entity.type = payload.type  # type: ignore
                if getattr(payload, "code", None) is not None:
                    entity.code = payload.code  # type: ignore
                if getattr(payload, "view", None) is not None:
                    # view is a Pydantic model; store as dict
                    v = payload.view
                    entity.view = v.model_dump() if hasattr(v, "model_dump") else v  # type: ignore
                if getattr(payload, "status", None) is not None:
                    entity.status = payload.status  # type: ignore

            await db.flush()
            await db.commit()
            await db.refresh(entity)
            return entity
        except Exception as e:
            if persist_data:
                # Persist last_refreshed_at but do not overwrite existing data on failure
                entity.last_refreshed_at = datetime.utcnow()
                await db.flush()
                await db.commit()
            # Re-raise as ValueError for route to map to 404/400 as designed
            raise ValueError(str(e))

    async def preview_entity(
        self,
        db: AsyncSession,
        entity_id: str,
        payload,
        organization: Organization,
        current_user: Optional[User] = None,
    ) -> dict:
        """Execute provided code (or entity code) without persisting, return preview/result or error."""
        result = await db.execute(select(Entity).where(Entity.id == str(entity_id), Entity.organization_id == str(organization.id)))
        entity = result.scalar_one_or_none()
        if not entity:
            raise ValueError("Entity not found")

        code_to_run = (getattr(payload, "code", None) if payload else None) or entity.code or ""

        from app.ai.code_execution.code_execution import StreamingCodeExecutor
        from app.services.data_source_service import DataSourceService
        ds_service = DataSourceService()
        ds_list = list(entity.data_sources or [])
        if not ds_list:
            # DS-less entities (promoted from chat-created reports): fall back
            # to the org's data sources — generated code addresses clients by
            # "<data source name>:<connection>" keys.
            _ds_stmt = select(DataSource).where(
                DataSource.organization_id == str(organization.id),
                DataSource.deleted_at.is_(None),
            )
            ds_list = list((await db.execute(_ds_stmt)).scalars().unique().all())
        ds_clients = {}
        for ds in ds_list:
            ds_conns = await ds_service.construct_clients(db, ds, current_user=current_user)
            ds_clients.update(ds_conns)
        excel_files = []

        # Pass organization_settings so widget serialization honors the org's
        # limit_row_count instead of falling back to the hardcoded 1000-row cap.
        org_settings = await organization.get_settings(db) if organization else None
        executor = StreamingCodeExecutor(organization_settings=org_settings)
        resolved_params = await self._resolve_entity_params(
            db, entity, current_user, organization
        )
        try:
            exec_df, execution_log, _ = executor.execute_code(
                code=code_to_run, ds_clients=ds_clients, excel_files=excel_files,
                params=resolved_params,
            )
            df = executor.format_df_for_widget(exec_df)
            return {"data": df, "execution_log": execution_log, "applied_params": resolved_params or None}
        except Exception as e:
            return {"data": None, "error": str(e)}

    async def _get_owned_entity(
        self, db: AsyncSession, entity_id: str, current_user: User, organization: Organization,
    ) -> Entity:
        from fastapi import HTTPException
        result = await db.execute(select(Entity).where(
            Entity.id == str(entity_id), Entity.organization_id == str(organization.id)
        ))
        entity = result.scalar_one_or_none()
        if not entity:
            raise HTTPException(status_code=404, detail="Entity not found")
        if str(entity.owner_id) != str(current_user.id):
            raise HTTPException(status_code=403, detail="Only the entity owner can do this")
        return entity

    async def suggest_entity(
        self, db: AsyncSession, entity_id: str, current_user: User, organization: Organization,
    ) -> Entity:
        """Owner promotes their private entity to a suggestion:
        Private Published -> Suggested (draft pending admin review)."""
        from fastapi import HTTPException
        entity = await self._get_owned_entity(db, entity_id, current_user, organization)
        if entity.global_status == "approved":
            raise HTTPException(status_code=400, detail="Entity is already approved")
        if entity.global_status == "suggested":
            return entity  # idempotent
        entity.private_status = entity.private_status or "published"
        entity.global_status = "suggested"
        entity.status = "draft"
        await db.flush()
        await db.commit()
        await db.refresh(entity)
        return entity

    async def withdraw_suggestion(
        self, db: AsyncSession, entity_id: str, current_user: User, organization: Organization,
    ) -> Entity:
        """Owner withdraws their suggestion back to private:
        Suggested -> Private Published."""
        from fastapi import HTTPException
        entity = await self._get_owned_entity(db, entity_id, current_user, organization)
        if entity.global_status != "suggested":
            raise HTTPException(status_code=400, detail="Entity is not a pending suggestion")
        entity.global_status = None
        entity.private_status = "published"
        entity.status = "draft"
        await db.flush()
        await db.commit()
        await db.refresh(entity)
        return entity

    def _is_admin_permissions(self, user_permissions: set) -> bool:
        """Entity admin = full_admin_access or org-level manage_entities.
        Per-DS create is checked at the route layer."""
        return bool({'full_admin_access', 'manage_entities'} & set(user_permissions))

    async def _get_user_permissions(self, db: AsyncSession, user: User, organization: Organization) -> set:
        """Get user's org-level permissions via the RBAC resolver."""
        from app.core.permission_resolver import resolve_permissions

        resolved = await resolve_permissions(db, str(user.id), str(organization.id))
        return set(resolved.org_permissions)

    def _determine_update_type(
        self, entity: Entity, payload: EntityUpdate, current_user: User,
        user_permissions: set, *, resource_authorized: bool = False,
    ) -> str:
        """Determine what type of update this is based on permissions and changes.

        `resource_authorized` = the caller holds per-DS `create_entities` on
        every attached data source (verified at the route). They edit with
        admin-level field control, but the review transition (approve/reject a
        suggestion) stays an org-admin capability.
        """
        is_admin = self._is_admin_permissions(user_permissions)
        is_owner = entity.owner_id == current_user.id
        is_suggested = entity.global_status == "suggested"
        has_status_change = payload.status and payload.status != entity.status

        # Admin reviewing a suggested entity (approve or reject)
        if is_admin and is_suggested and has_status_change:
            if payload.status in ["published", "archived"]:
                return "admin_review"

        # Admin or per-DS entity manager editing any entity (not review)
        if is_admin or resource_authorized:
            return "admin_edit"

        # Owner editing their own entity when not globally approved (suggested or private)
        if is_owner and (entity.global_status != "approved") and user_permissions:
            return "owner_edit"

        # No permission
        return "no_permission"

    async def _handle_admin_review(self, entity: Entity, payload: EntityUpdate, admin_user: User):
        """Handle admin reviewing a suggested entity (approve or reject)"""
        if payload.status == "published":
            # APPROVAL: Suggested -> Global Published
            # From: published, suggested, draft
            # To: null, approved, published
            entity.private_status = None
            entity.global_status = "approved"
            entity.status = "published"
            entity.reviewed_by_user_id = admin_user.id
            
        elif payload.status == "archived":
            # REJECTION: Suggested -> Private Archived
            # From: published, suggested, draft
            # To: published, rejected, archived
            entity.private_status = "published"
            entity.global_status = "rejected"
            entity.status = "archived"
            entity.reviewed_by_user_id = admin_user.id
        
        # Apply other changes from the form
        allowed_fields = ['title', 'description', 'type', 'code', 'tags']
        for field in allowed_fields:
            if hasattr(payload, field) and getattr(payload, field) is not None:
                setattr(entity, field, getattr(payload, field))
        
        if payload.view is not None:
            entity.view = payload.view.model_dump()

    async def _handle_admin_edit(self, entity: Entity, payload: EntityUpdate, admin_user: User):
        """Handle admin editing any entity (not review)"""
        # Admin can change status and gets credited as reviewer for status changes
        if payload.status and payload.status != entity.status:
            if payload.status in ["published", "archived"]:
                entity.reviewed_by_user_id = admin_user.id
        
        # Apply all changes (admin has full control)
        update_data = payload.model_dump(exclude_unset=True, exclude={'data_source_ids'})
        for field, value in update_data.items():
            if field == 'view' and value is not None:
                entity.view = value.model_dump() if hasattr(value, 'model_dump') else value
            else:
                setattr(entity, field, value)

    async def _handle_owner_edit(self, entity: Entity, payload: EntityUpdate):
        """Handle owner editing their own private entity"""
        # Owner can edit most fields except status changes
        allowed_fields = ['title', 'description', 'type', 'code', 'tags', 'data']
        
        for field in allowed_fields:
            if hasattr(payload, field) and getattr(payload, field) is not None:
                setattr(entity, field, getattr(payload, field))
        
        if payload.view is not None:
            entity.view = payload.view.model_dump() if hasattr(payload.view, 'model_dump') else payload.view


