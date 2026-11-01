"""A reader runs a published dashboard (phase 4, part C).

The conversation's two viewer runs, asked of the dashboard:

- `viewer_run` re-executes the release's charts (and the copies they need
  without showing: filter option queries, loaded steps) for one reader, as
  ReportService.viewer_rerun_report_steps does for a conversation;
- `filter_run` executes one copy with filter values, as
  QueryService.run_query_viewer does, with the same per-reader cache.

What changes is where the settings come from: whose credentials run it is
the dashboard's "run on my behalf" (artifacts.shared_run_identity), the owner
is artifacts.owner_id while still in the organization, the sources and files
are the release's. Everything else is today's rule: monitoring and RLS
sources always run as the reader; the owner's credentials only for members
or people the dashboard is shared with; identity values bind the reader;
results go to the reader's own rows, never to the copied snapshot. An "Auto"
release (no sources) resolves like its conversation would: the viewer run
over the sources the credential user may use, the filter run over the
organization's.
"""
import contextlib
import logging
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import lazyload, selectinload

from app.errors import AppError, ErrorCode
from app.models.artifact import Artifact
from app.models.artifact_release import (
    ArtifactRelease,
    artifact_release_data_source_association,
    artifact_release_file_association,
)
from app.models.query import Query
from app.models.step import Step
from app.models.user import User

logger = logging.getLogger(__name__)


# ── what a release runs on ───────────────────────────────────────────────

async def release_data_sources(db, release: ArtifactRelease) -> list:
    from app.models.data_source import DataSource

    ids = select(artifact_release_data_source_association.c.data_source_id).where(
        artifact_release_data_source_association.c.artifact_release_id == str(release.id))
    # Model-default loaders: construct_clients reads connections eagerly.
    return list((await db.execute(
        select(DataSource).where(DataSource.id.in_(ids), DataSource.deleted_at.is_(None))
    )).scalars().unique().all())


async def release_files(db, release: ArtifactRelease) -> list:
    from app.models.file import File

    ids = select(artifact_release_file_association.c.file_id).where(
        artifact_release_file_association.c.artifact_release_id == str(release.id))
    return list((await db.execute(select(File).where(File.id.in_(ids)))).scalars().all())


async def _org_data_sources(db, organization_id) -> list:
    from app.models.data_source import DataSource

    return list((await db.execute(
        select(DataSource).where(DataSource.organization_id == str(organization_id),
                                 DataSource.deleted_at.is_(None))
    )).scalars().unique().all())


# ── whose credentials ────────────────────────────────────────────────────

async def _is_member(db, artifact: Artifact, user) -> bool:
    """A live member of the dashboard's organization. Only members may run
    with the owner's credentials (a public dashboard must not let any
    signed-in stranger spend them) or choose filter values. Everyone a
    dashboard is shared with is a member: grants name members only, and a
    grant stops counting when its holder leaves."""
    from app.services.dashboard_access import _is_member as is_member

    return await is_member(db, user, artifact.organization_id)


async def run_identity(db, artifact: Artifact, release: ArtifactRelease, caller,
                       *, refuse_strangers: bool) -> tuple[str, User]:
    """('viewer' | 'creator', the user whose credentials run it)."""
    from app.services.dashboard_access import counts_as_owner
    from app.services.dashboard_data import release_run_data_source_ids
    from app.services.viewer_data_policy import _any_rls_relation

    identity = artifact.shared_run_identity if artifact.shared_run_identity in ("viewer", "creator") else "viewer"
    if release.bow_source_access:
        identity = "viewer"
    if identity == "creator" and await _any_rls_relation(db, await release_run_data_source_ids(db, release)):
        identity = "viewer"
    if identity == "creator" and not await counts_as_owner(db, artifact, caller):
        if not await _is_member(db, artifact, caller):
            if refuse_strangers:
                raise AppError.forbidden(ErrorCode.ACCESS_DENIED,
                                         "Running on behalf of the owner requires organization membership")
            return "viewer", caller
        owner = await db.get(User, str(artifact.owner_id)) if artifact.owner_id else None
        if owner is None or not await counts_as_owner(db, artifact, owner):
            raise AppError.forbidden(ErrorCode.ACCESS_DENIED, "The dashboard's owner can no longer run it")
        return "creator", owner
    return "viewer", caller


def _source_error(name: str, ds_id: str, e) -> dict:
    """The same machine-readable causes the conversation's viewer run gives."""
    from app.services.access_errors import source_error_code

    detail = str(getattr(e, "detail", None) or str(e))
    return {"data_source": name, "data_source_id": ds_id, "code": source_error_code(e), "error": detail}


# ── the whole dashboard ──────────────────────────────────────────────────

async def _shown_query_ids(db, release: ArtifactRelease) -> list[str]:
    from app.models.visualization import Visualization

    viz_ids = (release.content or {}).get("visualization_ids") or []
    stmt = select(Visualization.query_id).where(Visualization.artifact_release_id == str(release.id),
                                                Visualization.deleted_at.is_(None))
    if viz_ids:
        stmt = stmt.where(Visualization.id.in_([str(v) for v in viz_ids]))
    return list(dict.fromkeys(str(q) for (q,) in (await db.execute(stmt)).all() if q))


async def _needed_query_ids(db, release: ArtifactRelease) -> list[str]:
    """The release's charts, then the copies they need without showing: a
    filter's option query (its options_source names the copy) and every step
    they load, transitively -- the release holds both (copy_dashboard). A
    reader whose snapshot is withheld sees only their own run's rows, so a
    filter whose option query has no chart would offer nothing."""
    from app.ai.code_execution.loadables import extract_loadable_refs
    from app.services.identity_taint import release_step_lookup

    shown = await _shown_query_ids(db, release)
    copies = {str(qid): (params, code) for qid, params, code in (await db.execute(
        select(Query.id, Query.parameters, Step.code)
        .outerjoin(Step, Step.id == Query.default_step_id)
        .where(Query.artifact_release_id == str(release.id))
    )).all()}
    lookup = await release_step_lookup(db, str(release.id))

    def reached(qid: str):
        params, code = copies.get(qid, (None, None))
        for spec in params if isinstance(params, list) else []:
            src = spec.get("options_source") if isinstance(spec, dict) else None
            if isinstance(src, dict) and src.get("query_id"):
                yield str(src["query_id"])
        for ref in extract_loadable_refs(code or "")[0]:
            step = lookup(ref)
            if step is not None:
                yield str(step.query_id)

    seen, out, todo = set(shown), list(shown), list(shown)
    while todo:
        for qid in reached(todo.pop()):
            if qid in copies and qid not in seen:
                seen.add(qid)
                out.append(qid)
                todo.append(qid)
    return out


async def in_dependency_order(db, release: ArtifactRelease, targets: list) -> list:
    """`targets` ((step_id, code) pairs) reordered so a step runs after the
    steps of the release it loads (load_step, resolved as the release
    resolves it); otherwise in the given order. A cycle runs in the given
    order -- its load_step fails at run time, as it would anyway."""
    from app.ai.code_execution.loadables import extract_loadable_refs
    from app.services.identity_taint import release_step_lookup

    codes = {str(sid): code for sid, code in targets}
    lookup = await release_step_lookup(db, str(release.id))
    ordered, done, visiting = [], set(), set()

    def visit(sid: str) -> None:
        if sid in done or sid in visiting:
            return
        visiting.add(sid)
        for ref in extract_loadable_refs(codes.get(sid) or "")[0]:
            dep = lookup(ref)
            if dep is not None and str(dep.id) in codes:
                visit(str(dep.id))
        visiting.discard(sid)
        done.add(sid)
        ordered.append(sid)

    for sid in codes:
        visit(sid)
    return [(sid, codes[sid]) for sid in ordered]


# ── a step that waits for the viewer's input ─────────────────────────────

def needs_input_frame(data: dict | None, names: list[str]) -> dict:
    """The rows of a step that waits for input: none. Columns from `data` (the copy's), or none."""
    columns = list(data.get("columns") or []) if isinstance(data, dict) else []
    return {"rows": [], "columns": columns,
            "info": {"total_rows": 0, "total_columns": len(columns), "needs_input": list(names)}}


def needs_input_names(data: dict | None) -> list[str]:
    """The inputs a waiting frame waits for; [] for any other data."""
    info = data.get("info") if isinstance(data, dict) else None
    names = info.get("needs_input") if isinstance(info, dict) else None
    return [str(n) for n in names] if isinstance(names, list) and names else []


def input_wait_blocked_reason(source_errors: list[dict]) -> str | None:
    """Why a waiting step stores an error instead: the fixed reason of the
    source errors it is blocked by (connect first, then no access), else the
    first error's text. None when nothing blocks it."""
    from app.services.access_errors import CREDENTIALS_REQUIRED_REASON, NO_ACCESS_REASON

    if not source_errors:
        return None
    codes = {e.get("code") for e in source_errors}
    if "credentials_required" in codes:
        return CREDENTIALS_REQUIRED_REASON
    if "no_access" in codes:
        return NO_ACCESS_REASON
    return (str(source_errors[0].get("error") or "") or "connection_failed")[:2000]


def loaded_input_wait(code: str | None, lookup, data_of) -> list[str]:
    """The inputs the steps `code` loads (load_step, resolved by `lookup`)
    wait for, per `data_of(step) -> their data`: a step that loads a waiting
    step waits for the same inputs -- it is never handed an empty frame as
    if it were the loaded step's rows."""
    from app.ai.code_execution.loadables import extract_loadable_refs

    names: list[str] = []
    for ref in extract_loadable_refs(code or "")[0]:
        dep = lookup(ref)
        if dep is None:
            continue
        names += [n for n in needs_input_names(data_of(dep)) if n not in names]
    return names


async def _reader_input_wait(db, step: Step, artifact, release, user, *, lookup, resolved_waits) -> list[str]:
    """loaded_input_wait for a reader's run: what each loaded step resolves
    to for them, as load_step reads it (resolve_release_step) -- their own
    waiting row, or a waiting copy."""
    from sqlalchemy.orm import undefer

    from app.ai.code_execution.loadables import extract_loadable_refs
    from app.services.dashboard_data import resolve_release_step

    refs = extract_loadable_refs(step.code or "")[0]
    if not refs:
        return []
    names: list[str] = []
    for dep_id in dict.fromkeys(str(d.id) for d in map(lookup, refs) if d is not None):
        if dep_id == str(step.id):
            continue
        if dep_id in resolved_waits:
            names += [n for n in resolved_waits[dep_id] if n not in names]
            continue
        dep = (await db.execute(
            select(Step).options(lazyload("*"), undefer(Step.data)).where(Step.id == dep_id)
            .execution_options(populate_existing=True)
        )).scalars().first()
        if dep is None:
            continue
        resolution = await resolve_release_step(db, dep, artifact, release, user)
        waiting = list((resolution.viewer_result or {}).get("needs_input") or []) \
            or needs_input_names(resolution.data)
        resolved_waits[dep_id] = waiting
        names += [n for n in waiting if n not in names]
    return names


async def _run_copy(db, step_id: str, artifact, release, *, run_user, credential_user, executed_as,
                    ctx, db_clients, sources: list, source_errors: list[dict], lookup, resolved_waits) -> str:
    """StepService.run_step_to_user_result for one copy: the reader's own row.

    A copy that needs a required input the reader never gave does not run,
    nor one that loads a step waiting for the reader (it waits for the same
    inputs): the reader's row waits for it ("needs_input"), unless a source
    the copy uses failed for them -- then that failure's row, so its gate
    shows -- or another of its params fails to resolve (D3), whose error is
    stored as on a run."""
    from app.ai.code_execution.query_params import missing_required_inputs, params_fingerprint
    from app.models.step_user_result import StepUserResult
    from app.services.access_errors import (CREDENTIALS_REQUIRED_REASON, NO_ACCESS_REASON, is_access_denied,
                                            needs_credentials)
    from app.services.step_service import StepService
    from app.services.viewer_result_store import store_viewer_result

    service = StepService()
    # A dependency checked earlier may now be rerun. Never reuse its prior
    # wait after a failed or interrupted write; resolve the stored row again.
    resolved_waits.pop(str(step_id), None)
    step = (await db.execute(
        select(Step).options(lazyload("*"), selectinload(Step.query).options(lazyload("*")))
        .join(Query, Query.id == Step.query_id)
        .where(Step.id == str(step_id), Query.artifact_release_id == str(release.id))
    )).scalar_one_or_none()
    if step is None:
        return "error"
    prior = (await db.execute(
        select(StepUserResult).options(lazyload("*")).where(
            StepUserResult.step_id == str(step.id), StepUserResult.user_id == str(run_user.id))
        .order_by(StepUserResult.last_run_at.desc().nullslast()).limit(1)
    )).scalars().first()
    stored = step.applied_params
    if prior is not None:
        # A failed run's row, or a wait, holds no params (a failed
        # resolution may be what failed; a wait is not a run): the reader's
        # values, if any, over the copy's -- never "no values", which would
        # read as a missing input.
        stored = ({**(step.applied_params or {}), **(prior.applied_params or {})}
                  if prior.status == "error" or needs_input_names(prior.data) else prior.applied_params)
    specs = service._step_param_specs(step)
    missing = missing_required_inputs(specs, stored)
    waiting = missing or await _reader_input_wait(
        db, step, artifact, release, run_user, lookup=lookup, resolved_waits=resolved_waits)
    if waiting:
        from app.services.dashboard_publish import needed_sources

        used = {str(ds.id) for ds in needed_sources(sources, [step.code])}
        blocking = [e for e in source_errors if str(e.get("data_source_id")) in used]
        reason = input_wait_blocked_reason(blocking)
        if reason is None:
            # Every other param still resolves as on a run (D3): a required
            # identity param that resolves to None is the run's real error,
            # stored as such -- a wait never hides (or replaces) it.
            try:
                await service._resolve_step_params(
                    db, step, stored=stored, run_user=run_user, organization_id=str(release.organization_id),
                    specs=[s for s in specs if s.name not in missing])
            except Exception as e:
                reason = str(e)[:2000] or e.__class__.__name__
        # No columns and no values: a wait is not a run of the reader's
        # (resolve_release_step never serves it as their data).
        await store_viewer_result(
            db, step_id=str(step.id), user_id=str(run_user.id), organization_id=str(release.organization_id),
            report_id=None, params_fingerprint=params_fingerprint({}),
            status="error" if reason else "success", status_reason=reason,
            data=None if reason else needs_input_frame(None, waiting),
            executed_as=executed_as, applied_params=None, last_run_at=datetime.utcnow(),
        )
        if reason is None:
            resolved_waits[str(step.id)] = waiting
        return "error" if reason else "needs_input"
    params: dict = {}
    status, reason, data = "success", None, None
    try:
        # Identity params bind the reader in both modes, as on the conversation.
        params = await service._resolve_step_params(db, step, stored=stored, run_user=run_user,
                                                    organization_id=str(release.organization_id), specs=specs)
        data = await service._execute_step_code(db, step, ctx, db_clients=db_clients, params=params,
                                                param_specs=specs)
    except Exception as e:
        status, reason = "error", (str(e)[:2000] or e.__class__.__name__)
        if is_access_denied(reason):
            reason = NO_ACCESS_REASON
        elif needs_credentials(e):
            reason = CREDENTIALS_REQUIRED_REASON
    await store_viewer_result(
        db, step_id=str(step.id), user_id=str(run_user.id), organization_id=str(release.organization_id),
        report_id=None, params_fingerprint=params_fingerprint(params), status=status, status_reason=reason,
        data=data, executed_as=executed_as, applied_params=dict(params) if params else None,
        last_run_at=datetime.utcnow(),
    )
    if status == "success":
        resolved_waits[str(step.id)] = needs_input_names(data)
    return status


async def viewer_run(db, artifact: Artifact, release: ArtifactRelease, caller) -> dict:
    from app.ai.tools.implementations.agent_focus_common import accessible_agents
    from app.ee.audit.service import audit_service
    from app.models.organization import Organization
    from app.services.dashboard_access import holds_snapshot
    from app.services.data_source_service import DataSourceService
    from app.services.exec_context import ExecContext
    from app.services.identity_taint import release_step_lookup
    from app.services.report_service import ReportService

    if caller is None:
        raise AppError.unauthorized()
    if await holds_snapshot(db, artifact, release, caller):
        # The stored rows are already this person's own run (collab01).
        raise AppError.bad_request(ErrorCode.VALIDATION,
                                   "The owner refreshes the dashboard itself, not as a reader")
    identity, credential_user = await run_identity(db, artifact, release, caller, refuse_strangers=True)
    organization = await db.get(Organization, str(release.organization_id))
    org_settings = await organization.get_settings(db) if organization else None

    sources = await release_data_sources(db, release)
    if not sources:
        sources = await accessible_agents(db, organization, credential_user)
    sources = [ds for ds in sources if DataSourceService.is_execution_live(ds)]
    ds_service = DataSourceService()
    db_clients: dict = {}
    errors: list[dict] = []
    connection_errors: list = []
    for ds in sources:
        try:
            db_clients.update(await ds_service.construct_clients(
                db, ds, current_user=credential_user, connection_errors=connection_errors))
        except Exception as e:
            # Its charts still get a client that fails with the cause, not a
            # bare missing key (construct_clients attaches the stand-ins).
            db_clients.update(getattr(e, "unavailable_clients", None) or {})
            errors.append(_source_error(ds.name, str(ds.id), e))
    for ce in connection_errors:
        errors.append({**_source_error(ce["data_source_name"], ce["data_source_id"], ce["error"]),
                       "connection_name": ce["connection_name"]})

    files = await release_files(db, release)
    ctx = ExecContext.from_release(release, data_sources=sources, files=files, credential_user=credential_user,
                                   reader=caller, organization=organization, organization_settings=org_settings)
    targets, unrunnable = await ReportService()._rerun_target_steps(db, await _needed_query_ids(db, release))
    # What a chart loads runs first, so it loads the reader's fresh rows.
    targets = await in_dependency_order(db, release, targets)
    # Both caches belong to this reader's run only. Each stored result above
    # replaces its wait, so downstream copies observe the freshly run value.
    lookup = await release_step_lookup(db, str(release.id))
    resolved_waits: dict[str, list[str]] = {}
    total, succeeded, failed, needing_input = 0, 0, 0, 0
    for step_id, code in targets:
        total += 1
        if not (code or "").strip():
            failed += 1
            continue
        try:
            status = await _run_copy(db, step_id, artifact, release, run_user=caller,
                                     credential_user=credential_user, executed_as=identity, ctx=ctx,
                                     db_clients=db_clients, sources=sources, source_errors=errors,
                                     lookup=lookup, resolved_waits=resolved_waits)
        except Exception as e:
            logger.warning("Dashboard viewer run failed for step %s: %s", step_id, e)
            status = "error"
        # Waiting for the reader's input is not a failure: the app gives it.
        needing_input += status == "needs_input"
        ok = status in ("success", "needs_input")
        succeeded, failed = (succeeded + 1, failed) if ok else (succeeded, failed + 1)
    total += unrunnable
    failed += unrunnable
    with contextlib.suppress(Exception):  # the audit trail never fails the run
        await audit_service.log(db=db, organization_id=str(release.organization_id), action="dashboard.viewer_run",
                                user_id=str(caller.id), resource_type="artifact", resource_id=str(artifact.id),
                                details={"executed_as": identity, "release_id": str(release.id),
                                         "steps_total": total, "steps_succeeded": succeeded,
                                         "steps_failed": failed, "steps_needing_input": needing_input})
    if total == 0:
        message = "No dashboard steps to run"
    elif failed == 0:
        message = f"Ran {succeeded} step{'s' if succeeded != 1 else ''}"
    else:
        message = f"Ran {succeeded}/{total} steps ({failed} failed)"
    return {"message": message, "steps_total": total, "steps_succeeded": succeeded, "steps_failed": failed,
            "steps_needing_input": needing_input, "executed_as": identity, "last_run_at": datetime.utcnow(),
            "data_source_errors": errors}


# ── one chart, with filter values ────────────────────────────────────────

async def filter_run(db, artifact: Artifact, release: ArtifactRelease, query: Query, caller, request) -> dict:
    from app.ai.code_execution.code_execution import StreamingCodeExecutor
    from app.ai.code_execution.query_params import (
        ParamError,
        params_fingerprint,
        resolve_param_values,
        verify_identity_binds_in_queries,
    )
    from app.models.organization import Organization
    from app.models.step_user_result import StepUserResult
    from app.schemas.param_schema import parse_param_specs
    from app.services.access_errors import (CREDENTIALS_REQUIRED_REASON, NO_ACCESS_REASON, is_access_denied,
                                            needs_credentials)
    from app.services.dashboard_data import default_step
    from app.services.data_source_service import DataSourceService
    from app.services.identity_taint import step_identity_scope
    from app.services.query_service import QueryService, _no_access_code
    from app.services.viewer_result_store import store_viewer_result

    if caller is None:
        raise AppError.unauthorized()
    if not await _is_member(db, artifact, caller):
        # The conversation's filter run is a member route; choosing values
        # reads slices beyond the published snapshot.
        raise AppError.forbidden(ErrorCode.ACCESS_DENIED, "Filters run for members of the organization")
    if getattr(request, "run_as_user_id", None):
        raise AppError.bad_request(ErrorCode.VALIDATION, "Viewing as another member is for the editor")
    step = await default_step(db, query)
    if step is None or not (step.code or "").strip():
        raise AppError.not_found(ErrorCode.ARTIFACT_NOT_FOUND)
    service = QueryService()
    specs = parse_param_specs(getattr(query, "parameters", None))
    try:
        identity = await service._resolve_identity_for(db, caller, str(release.organization_id))
        resolved = resolve_param_values(specs, request.params, identity)
    except ParamError as e:
        raise AppError.bad_request(ErrorCode.VALIDATION, str(e))
    fingerprint = params_fingerprint(resolved)

    existing = (await db.execute(
        select(StepUserResult).where(StepUserResult.step_id == str(step.id),
                                     StepUserResult.user_id == str(caller.id),
                                     StepUserResult.params_fingerprint == fingerprint)
    )).scalars().first()
    try:
        _, upstream = await step_identity_scope(db, step)
    except Exception:
        upstream = None
    floor = step.updated_at
    if upstream is not None and (floor is None or upstream > floor):
        floor = upstream
    fresh = (existing is not None and existing.status == "success" and not request.force_refresh
             and (floor is None or existing.last_run_at is None or existing.last_run_at >= floor))
    if fresh and not release.bow_source_access:
        return {"data": existing.data or {}, "applied_params": resolved, "cached": True, "status": "success",
                "step_id": str(step.id)}

    _, credential_user = await run_identity(db, artifact, release, caller, refuse_strangers=False)
    sources = await release_data_sources(db, release) or await _org_data_sources(db, release.organization_id)
    ds_service = DataSourceService()
    clients: dict = {}
    source_errors: list[str] = []
    for ds in sources:
        try:
            clients.update(await ds_service.construct_clients(db, ds, current_user=credential_user,
                                                              connection_errors=[]))
        except Exception as e:
            source_errors.append(str(getattr(e, "detail", None) or e))
            if needs_credentials(e):
                clients.update(getattr(e, "unavailable_clients", None) or {})
    if not clients and source_errors:
        raise AppError.bad_request(ErrorCode.VALIDATION, "; ".join(source_errors[:2]))

    organization = await db.get(Organization, str(release.organization_id))
    org_settings = await organization.get_settings(db) if organization else None
    usage = service._usage_context(str(release.organization_id), str(caller.id), source="dashboard_filter_run",
                                   source_ref_id=str(query.id))
    from app.ai.code_execution.loadables import load_step_settings, resolve_loadables_for_code

    from app.data_sources.clients.bow_client import install_bow_client

    enable_load_step, _ = load_step_settings(org_settings)
    # Entities resolve as the reader; load_step finds the release's copies.
    loadables = await resolve_loadables_for_code(db, organization, None, caller, step.code,
                                                 enable_load_step=enable_load_step, release=release)
    await install_bow_client(db, organization, credential_user, None, clients, release=release)
    from app.ai.code_execution.sandbox_helpers import rerun_sandbox_helpers
    executor = StreamingCodeExecutor(organization_settings=org_settings, usage_context=usage,
                                     sandbox_helpers=rerun_sandbox_helpers(step.code, sources, org_settings))
    captured: list = []
    try:
        exec_df, _, _ = await executor.execute_code_async(
            code=step.code, ds_clients=clients, excel_files=await release_files(db, release),
            loadables=loadables, captured_queries=captured, params=resolved)
        bind_error = verify_identity_binds_in_queries(captured, resolved, specs)
        if bind_error:
            raise ParamError(bind_error)
        df, status, reason = executor.format_df_for_widget(exec_df), "success", None
    except ParamError as e:
        raise AppError.bad_request(ErrorCode.VALIDATION, str(e))
    except Exception as e:
        df, status, reason = None, "error", str(e)
        if is_access_denied(reason):
            reason = NO_ACCESS_REASON
        elif needs_credentials(e):
            reason = CREDENTIALS_REQUIRED_REASON
    finally:
        if usage is not None:
            with contextlib.suppress(Exception):
                await usage.flush()

    if status == "success":
        await store_viewer_result(
            db, step_id=str(step.id), user_id=str(caller.id), organization_id=str(release.organization_id),
            report_id=None, params_fingerprint=fingerprint, status="success", status_reason=None, data=df,
            applied_params=dict(resolved) if resolved else None,
            executed_as="creator" if str(credential_user.id) != str(caller.id) else "viewer",
            last_run_at=datetime.utcnow())
        return {"data": df or {}, "applied_params": resolved, "cached": False, "status": "success",
                "step_id": str(step.id)}
    return {"data": {}, "applied_params": resolved, "cached": False, "status": "error", "error": reason,
            "error_code": _no_access_code(reason), "step_id": str(step.id)}
