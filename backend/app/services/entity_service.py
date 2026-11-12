import logging
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
from app.services.query_service import QueryService, flush_usage
from app.schemas.entity_schema import EntityCreate, EntityUpdate
from datetime import datetime
from app.schemas.entity_schema import EntityRunPayload
from app.core.telemetry import telemetry
from app.ee.audit.service import audit_service
from app.dependencies import async_session_maker
from app.services.usage_policy_service import UsageLimitContext
from app.errors import AppError, ErrorCode
from app.errors.redact import describe_for_log

logger = logging.getLogger(__name__)


def is_run_refusal(exc: Optional[BaseException]) -> bool:
    """True when a run was refused rather than failed: the query guard or the
    code scan, in-process execution refused on this host, a host without the
    sandbox, a connection rate limit or a usage quota, a connection with no
    free query slot, raised in the parent or reported by the sandbox child by
    type name. The route answers each with its own status."""
    from app.ai.code_execution.code_execution import CodeSecurityError
    from app.data_sources.query_concurrency import ConnectionBusyError
    from app.ai.code_execution.sandbox.runner import (
        SandboxExecutionError,
        SandboxModeRefused,
        SandboxUnavailableError,
    )
    from app.services.connection_rate_limit_service import RateLimitExceeded
    from app.services.usage_policy_service import UsageLimitExceeded

    if isinstance(exc, (CodeSecurityError, SandboxModeRefused, SandboxUnavailableError,
                        UsageLimitExceeded, RateLimitExceeded, ConnectionBusyError)):
        return True
    return isinstance(exc, SandboxExecutionError) and exc.exc_type in (
        "RateLimitExceeded", "UsageLimitExceeded", "ConnectionBusyError")


def _result_not_saved(entity_id, err: BaseException) -> AppError:
    """The answer to a run whose result the app database did not store.

    Told apart by where it happens (the save), not by the error's type: a
    warehouse client raises the same SQLAlchemy errors for a builder's own
    query. The app database's error quotes its statement and bound values, so
    neither the answer nor the log line carries its text."""
    logger.error("Entity %s: the run's result was not saved: %s", entity_id, describe_for_log(err))
    return AppError(ErrorCode.INTERNAL, "The run's result could not be saved.", status_code=500)


async def _roll_back_failed_save(db: AsyncSession, entity_id: str) -> None:
    """Roll the session back after a failed save of a builder run's result.

    Only for the run route's own request session (run_entity_with_update has
    no other caller): until then every use of it fails with an error
    repeating the database's statement and bound values. A viewer's slice is
    saved on a session of its own instead, as its callers share theirs."""
    try:
        await db.rollback()
    except Exception as err:
        logger.error("Entity %s: the session was not rolled back after a failed save: %s",
                     entity_id, describe_for_log(err))


class CallerTransactionLost(AppError):
    """Saving a viewer's slice in the caller's transaction made the database
    roll that whole transaction back (SQLite answers a full disk, an I/O
    error or a trigger's RAISE(ROLLBACK) so), and the caller's uncommitted
    writes with it. Ends the caller's run: its session refuses every further
    use until it is rolled back, so it never commits without them."""

    # Regenerating or re-running the code cannot bring the writes back.
    terminal_execution_error = True

    def __init__(self) -> None:
        super().__init__(ErrorCode.INTERNAL, "The run's result could not be saved.", status_code=500)


# How long a slice save on a session of its own waits for a row lock. A lock
# held by the caller's own transaction is never released while the save
# waits: it fails fast instead of at the database's lock timeout.
_OWN_SAVE_LOCK_TIMEOUT = "5s"


async def _sqlite_write_lock_connection(db: AsyncSession):
    """The connection of `db` when it is on SQLite and has written without
    committing: it then holds the database's one write lock. (The driver opens
    a transaction only before a write.) None otherwise."""
    if db.get_bind().dialect.name != "sqlite" or not db.in_transaction():
        return None
    conn = await db.connection()
    raw = await conn.get_raw_connection()
    return conn if raw.driver_connection.in_transaction else None


async def _write_in_callers_transaction(db: AsyncSession, conn, write, entity_id: str) -> None:
    """`write(db)` in a savepoint of the caller's SQLite transaction on `conn`.

    A failure rolls back the savepoint alone and raises the database's own
    error. When the database answered with a rollback of the whole
    transaction instead, the caller's writes are gone while the session still
    takes it as open: its connection is invalidated, so the caller's next use
    of the session raises rather than commits without them, and
    CallerTransactionLost is raised. So too when whether the transaction
    survived cannot be read."""
    cause = None
    try:
        async with db.begin_nested():
            try:
                await write(db)
            except Exception as err:
                cause = err
                raise
    except Exception as err:
        cause = cause or err
        try:
            raw = await conn.get_raw_connection()
            survived = raw.driver_connection.in_transaction
        except Exception as probe_err:
            survived = False
            logger.error("Entity %s: whether the caller's transaction survived its slice's failed save "
                         "could not be read: %s", entity_id, describe_for_log(probe_err))
        if survived:
            raise cause from None
        try:
            await conn.invalidate()
        except Exception as invalidate_err:
            logger.error("Entity %s: the caller's connection was not invalidated: %s",
                         entity_id, describe_for_log(invalidate_err))
        logger.error("Entity %s: the caller's transaction was lost while its slice was saved: %s",
                     entity_id, describe_for_log(cause))
        raise CallerTransactionLost() from None


class EntityService:

    def __init__(self):
        self.step_service = StepService()
        self.query_service = QueryService()

    @staticmethod
    def _usage_context(organization, user, *, source: str,
                       entity_id: Optional[str]) -> Optional[UsageLimitContext]:
        """Rate limits, quotas and metering for one execution, labelled with
        the caller (`source`). Flushed by the caller when the execution ends,
        failed or not."""
        if organization is None or user is None:
            return None
        return UsageLimitContext(
            organization_id=str(organization.id),
            user_id=str(user.id),
            source=source,
            source_ref_id=str(entity_id) if entity_id else None,
            session_maker=async_session_maker,
        )

    # ------------------------------------------------------------------
    # Parameters (mirrors the step/query machinery)
    # ------------------------------------------------------------------

    async def _resolve_param_specs(
        self, db: AsyncSession, raw_specs, run_user, organization,
        request_values: Optional[dict] = None,
    ) -> dict:
        """Resolve declared params: defaults <- request values <- the run
        user's identity bindings. {} when nothing is declared.

        `raw_specs` are ParamSpec dicts (an entity's saved list, or the ones a
        form is about to save). `request_values` are caller-supplied VALUES
        ({name: value}). Unknown names and client values for identity-locked
        params are rejected (ParamError), exactly like a query viewer run."""
        from app.schemas.param_schema import parse_param_specs
        from app.ai.code_execution.query_params import ParamError, resolve_param_values
        specs = parse_param_specs(raw_specs)
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
        try:
            return resolve_param_values(specs, request_values or None, identity)
        except ParamError as e:
            # Name the declared parameters so a caller (a planner guessing a
            # column name, a script with a typo) can correct itself in one go.
            declared = ", ".join(s.name for s in specs)
            raise ParamError(f"{e} (declared parameters: {declared})") from e

    async def _resolve_entity_params(
        self, db: AsyncSession, entity, run_user, organization,
        request_values: Optional[dict] = None,
    ) -> dict:
        """`_resolve_param_specs` over the entity's saved declarations."""
        return await self._resolve_param_specs(
            db, getattr(entity, "parameters", None), run_user, organization, request_values,
        )

    @staticmethod
    def validate_param_specs(raw_specs) -> list:
        """Strictly validate declarations a form is about to SAVE — the lenient
        `parse_param_specs` used at run time skips bad rows, which would let a
        typo'd declaration vanish silently. Raises ParamError."""
        from app.schemas.param_schema import ParamSpec
        from app.ai.code_execution.query_params import ParamError
        out: list = []
        seen: set = set()
        for item in raw_specs or []:
            try:
                spec = ParamSpec.model_validate(item)
            except Exception as e:
                raise ParamError(f"invalid parameter declaration: {e}") from e
            if spec.name in seen:
                raise ParamError(f"duplicate parameter name: {spec.name}")
            seen.add(spec.name)
            out.append(spec.model_dump())
        return out

    @staticmethod
    async def _upsert_entity_user_result(
        db: AsyncSession, entity, ids: dict, resolved_params: dict, df: dict, *,
        entity_access: Optional[dict] = None,
    ) -> None:
        """Cache a viewer's slice per (entity, user, values fingerprint), on a
        session of its own.

        The caller's session `db` may be shared (an agent's, an MCP
        request's, a dashboard publish's): this write never commits it, and a
        failed one never rolls it back, so what it holds stays loaded and
        what it has not committed stays its own. One upsert on the slice's
        key: two requests saving the same slice both succeed, the later one's
        rows kept. `entity_access`: the monitoring marker the run gave
        `entity`, stored with the slice and set on `entity` as stored, never
        left for the caller to write (the caller's transaction would then
        hold the entity's row, which the next save of a slice of it waits
        for). A failure is the fixed 500; CallerTransactionLost when it cost
        the caller its transaction. `ids` holds the entity, user and
        organization ids."""
        from sqlalchemy import text, update
        from sqlalchemy.orm.attributes import set_committed_value

        from app.ai.code_execution.query_params import params_fingerprint
        from app.models.entity_user_result import EntityUserResult
        from app.services.viewer_result_store import upsert_viewer_row
        entity_id = ids["entity_id"]
        key = dict(entity_id=entity_id, user_id=ids["user_id"],
                   params_fingerprint=params_fingerprint(resolved_params))
        now = datetime.utcnow()
        values = dict(status="success", status_reason=None, data=df,
                      applied_params=dict(resolved_params) if resolved_params else None,
                      last_run_at=now, updated_at=now)

        async def write(session: AsyncSession) -> None:
            await upsert_viewer_row(session, EntityUserResult, key, values,
                                    organization_id=ids["organization_id"])
            if entity_access is not None:
                await session.execute(update(Entity).where(Entity.id == entity_id)
                                      .values(bow_source_access=entity_access))

        try:
            held = await _sqlite_write_lock_connection(db)
            if held is not None:
                # SQLite has one writer at a time, and it is the caller's
                # session (a publish's flushed monitoring marker): another
                # session's write would wait for it, then fail. The slice
                # joins the caller's transaction in a savepoint, committed by
                # the caller.
                await _write_in_callers_transaction(db, held, write, entity_id)
            else:
                # Closing the session rolls back what it did not commit.
                async with async_session_maker() as own:
                    if own.get_bind().dialect.name == "postgresql":
                        await own.execute(text(f"SET LOCAL lock_timeout = '{_OWN_SAVE_LOCK_TIMEOUT}'"))
                    await write(own)
                    await own.commit()
        except CallerTransactionLost:
            raise
        except Exception as err:
            raise _result_not_saved(entity_id, err) from None
        if entity_access is not None:
            set_committed_value(entity, "bow_source_access", entity_access)

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
        from app.services.bow_source_access import assert_read
        await assert_read(db, getattr(entity, "bow_source_access", None), user)
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
        Raises ParamError for bad values (also when the saved code refuses
        them while it runs) and ValueError when no data-source client can be
        built for the caller. A refusal and a typed error (a query timeout)
        keep their own error. Any other failure of the run, turning its result into rows
        included, is AppError entity.run_failed (422) without its text; a
        failed save of the slice is AppError generic (500).
        """
        from app.ai.code_execution.query_params import ParamError, params_fingerprint
        from app.models.entity_user_result import EntityUserResult
        from app.services.bow_source_access import assert_read
        await assert_read(db, getattr(entity, "bow_source_access", None), user)
        from app.services.identity_taint import entity_identity_scope

        if user is None:
            raise ParamError("running an entity with parameters requires an authenticated user")
        entity_id = str(entity.id)
        ids = {"entity_id": entity_id, "user_id": str(user.id), "organization_id": str(entity.organization_id)}

        resolved = await self._resolve_entity_params(
            db, entity, user, organization, request_values
        )
        fingerprint = params_fingerprint(resolved)
        # The slice is written on another session: read what is stored, even
        # over an instance this session already holds.
        from app.services.viewer_result_store import viewer_row
        row = (await db.execute(viewer_row(
            EntityUserResult, entity_id=entity_id, user_id=ids["user_id"], params_fingerprint=fingerprint,
        ))).scalars().first()
        # Stale once the entity OR any of its loadable upstreams refreshed
        # past the cached slice.
        _has_identity, upstream_refresh = await entity_identity_scope(db, entity)
        refreshed_at = getattr(entity, "last_refreshed_at", None)
        if upstream_refresh is not None and (
            refreshed_at is None or upstream_refresh > refreshed_at
        ):
            refreshed_at = upstream_refresh
        if (
            not force_refresh and not entity.bow_source_access
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
        if not ds_list and not entity.bow_source_access:
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
        from app.services.bow_source_access import install_entity_client
        await install_entity_client(db, organization, user, entity, ds_clients)
        source = "entity_viewer_run"
        usage_context = self._usage_context(organization, user, source=source, entity_id=entity.id)
        executor = StreamingCodeExecutor(organization_settings=org_settings, usage_context=usage_context)
        from app.data_sources.query_tag import query_tag_defaults
        entity_access = None
        try:
            with query_tag_defaults(source=source, entity_id=entity.id,
                                    org_id=getattr(organization, "id", None), user_id=getattr(user, "id", None)):
                exec_df, _log, _ = await executor.execute_code_async(
                    code=entity.code or "", ds_clients=ds_clients, excel_files=[],
                    params=resolved,
                )
            df = executor.format_df_for_widget(exec_df)
            if exec_df.attrs.get("bow_source"):
                from app.services.bow_source_access import merge_access
                entity_access = merge_access(entity.bow_source_access, exec_df.attrs["bow_source"])
        except Exception as e:
            # A refusal and a typed error (a query timeout: its params carry
            # only the budget) keep their own answer, and so does the reader's
            # own mistake: values the saved code refuses while it runs
            # (calendar_date_bounds), whose text is built from those values
            # and fixed strings. Any other failure (the saved code's, a data
            # source's, a swallowed query's, whose text and params quote the
            # data source's errors, the sandbox's own) is a 422 without its
            # text: it is the owner's code and query, not this reader's
            # request, and its message can quote SQL and bound values. The
            # log names the types, never the message.
            from app.ai.code_execution.code_execution import SwallowedQueryError
            from app.ai.code_execution.sandbox.runner import SandboxExecutionError
            if isinstance(e, ParamError) or is_run_refusal(e) or (
                    isinstance(e, AppError) and not isinstance(e, SwallowedQueryError)):
                raise
            if isinstance(e, SandboxExecutionError) and e.exc_type == "ParamError":
                raise ParamError(e.message) from None
            logger.warning("Entity %s: its saved code failed in a run with parameter values (%s): %s",
                           entity_id, getattr(e, "exc_type", None) or type(e).__name__, describe_for_log(e))
            raise AppError(ErrorCode.ENTITY_RUN_FAILED, "The saved query failed to run.",
                           status_code=422) from None
        finally:
            await flush_usage(usage_context)
        await self._upsert_entity_user_result(db, entity, ids, resolved, df, entity_access=entity_access)
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

        from app.services.bow_source_access import step_access, assert_read
        bow_access = await step_access(db, step)
        await assert_read(db, bow_access, current_user)
        entity = Entity(
            bow_source_access=bow_access,
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
        *,
        creator_can_publish: bool = True,
    ) -> Entity:
        slug = await self._unique_slug(db, organization, payload.slug or payload.title)
        entity = Entity(
            organization_id=str(organization.id),
            owner_id=str(current_user.id),
            type=payload.type,
            title=payload.title,
            slug=slug,
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
        # Same dual-status lifecycle as a from-step promotion (the route decided
        # the tier): an entity manager gets a catalog row, published or held as
        # a draft; anyone else gets a suggestion pending admin review.
        if creator_can_publish:
            entity.private_status = None
            entity.global_status = "approved"
            entity.published_at = (
                payload.published_at or datetime.utcnow()
            ) if payload.status == "published" else None
        else:
            entity.private_status = "published"
            entity.global_status = "suggested"
            entity.status = "draft"
            entity.published_at = None
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
        from app.services.bow_source_access import visible_entities_clause
        bow_visible = await visible_entities_clause(db, organization.id, current_user)
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
            .where(bow_visible)
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
        
        # Workshop rows (drafts, suggestions, archived) belong to their author
        # and whoever may approve them — see app.core.entity_scope. Applied in
        # SQL, before LIMIT, so other people's drafts can't consume the page.
        from app.core.entity_scope import entity_authority_scope, visibility_clause

        unscoped, authority_ids = await entity_authority_scope(
            db, str(current_user.id), str(organization.id)
        )
        stmt = stmt.where(
            visibility_clause(
                Entity, entity_data_source_association,
                str(current_user.id), unscoped, authority_ids,
            )
        )

        stmt = stmt.order_by(Entity.updated_at.desc()).offset(skip).limit(limit)
        result = await db.execute(stmt)
        return list(result.scalars().all())

    async def get_entity_counts(
        self,
        db: AsyncSession,
        organization: Organization,
        current_user: User,
    ) -> dict:
        """Per-agent counts for the /agents tree badges, without hydrating rows.

        Same visibility rules as ``list_entities`` so the badge agrees with the
        rows a lazy per-agent fetch returns — including the archived cut, which
        the tree applies to its rows.
        """
        from sqlalchemy import exists, and_, func
        from app.core.permission_resolver import get_accessible_data_source_ids
        from app.core.entity_scope import entity_authority_scope, visibility_clause

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

        # correlate(Entity): this query joins the association table in its own
        # FROM, and without it SQLAlchemy auto-correlates the table out of the
        # subquery, leaving it with no FROM clause.
        has_inaccessible_ds = exists(
            select(1)
            .select_from(entity_data_source_association)
            .where(
                and_(
                    entity_data_source_association.c.entity_id == Entity.id,
                    entity_data_source_association.c.data_source_id.notin_(accessible_ds_subquery),
                )
            )
            .correlate(Entity)
        )

        unscoped, authority_ids = await entity_authority_scope(
            db, str(current_user.id), str(organization.id)
        )

        from app.services.bow_source_access import visible_entities_clause
        bow_visible = await visible_entities_clause(db, organization.id, current_user)
        rows = await db.execute(
            select(
                entity_data_source_association.c.data_source_id,
                func.count(Entity.id),
            )
            .select_from(Entity)
            .join(
                entity_data_source_association,
                entity_data_source_association.c.entity_id == Entity.id,
            )
            .where(Entity.organization_id == str(organization.id))
            .where(Entity.deleted_at == None)
            .where(~has_inaccessible_ds)
            .where(bow_visible)
            # The tree hides archived rows, so the badge must not count them.
            .where(Entity.status != "archived")
            .where(or_(Entity.private_status == None, Entity.private_status != "archived"))
            .where(
                visibility_clause(
                    Entity, entity_data_source_association,
                    str(current_user.id), unscoped, authority_ids,
                )
            )
            .group_by(entity_data_source_association.c.data_source_id)
        )
        by_agent = {str(ds_id): int(n) for ds_id, n in rows.all()}
        return {"by_agent": by_agent, "total": sum(by_agent.values())}

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
        
        from app.services.bow_source_access import can_read
        if entity and not await can_read(db, entity.bow_source_access, current_user):
            return None
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

        # ...and the same workshop rule the list applies, so a draft that is
        # hidden from the tree cannot be read by guessing its id.
        from app.core.entity_scope import entity_authority_scope, can_view_entity

        unscoped, authority_ids = await entity_authority_scope(
            db, str(current_user.id), str(organization.id)
        )
        if not can_view_entity(entity, str(current_user.id), unscoped, authority_ids):
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
            raise AppError.not_found(ErrorCode.ENTITY_NOT_FOUND, "Entity not found")

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
        if not ds_list and not entity.bow_source_access:
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
        from app.services.bow_source_access import install_entity_client
        await install_entity_client(db, organization, current_user, entity, ds_clients)
        usage_context = self._usage_context(organization, current_user, source="entity_run", entity_id=entity.id)
        executor = StreamingCodeExecutor(organization_settings=org_settings, usage_context=usage_context)

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
            try:
                from app.data_sources.query_tag import query_tag_defaults
                with query_tag_defaults(source="entity_run", entity_id=entity.id,
                                        org_id=getattr(organization, "id", None),
                                        user_id=getattr(current_user, "id", None)):
                    exec_df, execution_log, _ = await executor.execute_code_async(
                        code=code_to_run, ds_clients=ds_clients, excel_files=excel_files,
                        params=resolved_params,
                    )
                df = executor.format_df_for_widget(exec_df)
                run_access = None
                if exec_df.attrs.get("bow_source"):
                    from app.services.bow_source_access import merge_access
                    run_access = merge_access(entity.bow_source_access, exec_df.attrs["bow_source"])
            except Exception as e:
                if persist_data:
                    # Persist last_refreshed_at but do not overwrite existing data
                    # on failure. Best effort: a failing save must not replace the
                    # run's own error, which is what the caller has to act on.
                    try:
                        entity.last_refreshed_at = datetime.utcnow()
                        await db.flush()
                        await db.commit()
                    except Exception as save_error:
                        logger.warning("Refresh time of entity %s was not saved after a failed run: %s",
                                       entity_id, describe_for_log(save_error))
                        await _roll_back_failed_save(db, entity_id)
                # Re-raise as ValueError for the route to map; the cause is kept
                # so a refusal (guard, quota) still maps to its own status.
                raise ValueError(str(e)) from e

            # The run succeeded; from here on a failure is the app database's
            # (saving the result), answered by _result_not_saved.
            if not persist_data:
                # Transient run: the caller's own slice. Cache it per
                # (entity, user, values) so load_entity and later reads reuse
                # it, then detach the instance so the shared snapshot is
                # never flushed.
                if current_user is not None:
                    ids = {"entity_id": str(entity_id), "user_id": str(current_user.id),
                           "organization_id": str(entity.organization_id)}
                    # The run's marker is stored with the slice and set on
                    # `entity` as stored, never left on it for `db` to write.
                    await self._upsert_entity_user_result(
                        db, entity, ids, resolved_params, df, entity_access=run_access)
                db.expunge(entity)
                if run_access is not None:
                    # Detached: the copy handed back, never flushed.
                    entity.bow_source_access = run_access
                entity.data = df
                return entity
            # Persist execution results
            if run_access is not None:
                entity.bow_source_access = run_access
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

            try:
                await db.flush()
                await db.commit()
            except Exception as err:
                failure = _result_not_saved(entity_id, err)
                await _roll_back_failed_save(db, entity_id)
                raise failure from None
            try:
                await db.refresh(entity)
            except Exception as err:
                # Saved; only reading the stored row back failed.
                logger.error("Entity %s: the run's result was saved but not read back: %s",
                             entity_id, describe_for_log(err))
                raise AppError(ErrorCode.INTERNAL, "The run's result was saved but could not be read back.",
                               status_code=500) from None
            return entity
        finally:
            await flush_usage(usage_context)

    @staticmethod
    def slugify(text: str) -> str:
        """`Monthly revenue (EU)` -> `monthly-revenue-eu`. Empty input -> `query`."""
        import re
        base = re.sub(r"[^a-z0-9]+", "-", (text or "").strip().lower()).strip("-")
        return base[:80] or "query"

    async def _unique_slug(self, db: AsyncSession, organization: Organization, text: str) -> str:
        """Slug from `text`, suffixed `-2`, `-3`, ... until unused in the org.
        The column has no unique constraint (comment says "unique per org"),
        so uniqueness is enforced here for the manual create path."""
        base = self.slugify(text)
        candidate = base
        n = 1
        while True:
            exists_stmt = select(Entity.id).where(
                Entity.organization_id == str(organization.id),
                Entity.slug == candidate,
            ).limit(1)
            if (await db.execute(exists_stmt)).scalar_one_or_none() is None:
                return candidate
            n += 1
            candidate = f"{base}-{n}"

    async def _execute_entity_code(
        self,
        db: AsyncSession,
        code: str,
        ds_list: list,
        organization: Organization,
        current_user: Optional[User],
        resolved_params: Optional[dict] = None,
        entity: Optional[Entity] = None,
        *,
        source: str,
    ) -> dict:
        """Run `code` against `ds_list` (or every org agent when empty) without
        persisting anything. Shared by the per-entity preview and the stateless
        one the "New query" form uses before a row exists.

        `entity` is the saved row when there is one: its BOW source access
        gates the run and is widened by what the run read. `source` labels the
        run's metered queries with its caller."""
        from app.ai.code_execution.code_execution import StreamingCodeExecutor
        from app.services.data_source_service import DataSourceService
        ds_service = DataSourceService()
        bow_access = getattr(entity, "bow_source_access", None) if entity is not None else None
        ds_list = list(ds_list or [])
        if not ds_list and not bow_access:
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
        if entity is not None:
            from app.services.bow_source_access import install_entity_client
            await install_entity_client(db, organization, current_user, entity, ds_clients)
        usage_context = self._usage_context(organization, current_user, source=source,
                                            entity_id=getattr(entity, "id", None))
        executor = StreamingCodeExecutor(organization_settings=org_settings, usage_context=usage_context)
        try:
            from app.data_sources.query_tag import query_tag_defaults
            with query_tag_defaults(source=source, entity_id=getattr(entity, "id", None),
                                    org_id=getattr(organization, "id", None),
                                    user_id=getattr(current_user, "id", None)):
                exec_df, execution_log, _ = await executor.execute_code_async(
                    code=code, ds_clients=ds_clients, excel_files=excel_files,
                    params=resolved_params or {},
                )
            df = executor.format_df_for_widget(exec_df)
            if entity is not None and exec_df.attrs.get("bow_source"):
                from app.services.bow_source_access import merge_access
                entity.bow_source_access = merge_access(entity.bow_source_access, exec_df.attrs["bow_source"])

            return {"data": df, "execution_log": execution_log, "applied_params": resolved_params or None}
        except Exception as e:
            return {"data": None, "error": str(e)}
        finally:
            await flush_usage(usage_context)

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
        # The form may send the declarations it is about to save (edited
        # params) and test values; otherwise the saved ones apply as-is.
        sent_specs = getattr(payload, "parameters", None) if payload else None
        raw_specs = sent_specs if sent_specs is not None else getattr(entity, "parameters", None)
        resolved_params = await self._resolve_param_specs(
            db, raw_specs, current_user, organization,
            (getattr(payload, "params", None) if payload else None) or None,
        )
        return await self._execute_entity_code(
            db, code_to_run, list(entity.data_sources or []), organization, current_user, resolved_params,
            entity=entity, source="entity_preview",
        )

    async def preview_code(
        self,
        db: AsyncSession,
        code: str,
        data_source_ids: list,
        organization: Organization,
        current_user: Optional[User] = None,
        parameters: Optional[list] = None,
        params: Optional[dict] = None,
    ) -> dict:
        """Stateless preview: run `code` against the given agents. No Entity row
        is read or written — this is "try before save" for a manual query.

        `parameters` are the ParamSpec dicts the form would save with the
        query; `params` are test values for the input ones. They resolve
        exactly as a saved entity's would (identity params bind to the
        caller), so what Run shows is what Save will run."""
        ds_list: list = []
        if data_source_ids:
            stmt = select(DataSource).where(
                DataSource.organization_id == str(organization.id),
                DataSource.deleted_at.is_(None),
                DataSource.id.in_([str(i) for i in data_source_ids]),
            )
            ds_list = list((await db.execute(stmt)).scalars().unique().all())
            if len(ds_list) != len({str(i) for i in data_source_ids}):
                raise ValueError("Agent not found")
        resolved_params = await self._resolve_param_specs(
            db, parameters, current_user, organization, params or None,
        )
        return await self._execute_entity_code(
            db, code or "", ds_list, organization, current_user, resolved_params, source="entity_code_preview",
        )

    async def _get_owned_entity(
        self, db: AsyncSession, entity_id: str, current_user: User, organization: Organization,
    ) -> Entity:
        from fastapi import HTTPException
        result = await db.execute(select(Entity).where(
            Entity.id == str(entity_id), Entity.organization_id == str(organization.id)
        ))
        entity = result.scalar_one_or_none()
        # Missing and not-yours answer identically so the endpoint is not an
        # existence oracle for other users' entity ids.
        if not entity or str(entity.owner_id) != str(current_user.id):
            raise HTTPException(status_code=404, detail="Entity not found")
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
        allowed_fields = ['title', 'description', 'type', 'code', 'tags', 'data', 'parameters']
        
        for field in allowed_fields:
            if hasattr(payload, field) and getattr(payload, field) is not None:
                setattr(entity, field, getattr(payload, field))
        
        if payload.view is not None:
            entity.view = payload.view.model_dump() if hasattr(payload.view, 'model_dump') else payload.view

