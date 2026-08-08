from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from typing import List, Optional

from app.dependencies import get_async_db, get_current_organization
from app.core.auth import current_user
from app.core.permissions_decorator import requires_permission, check_resource_permissions, require_org_permission
from app.models.organization import Organization
from app.models.user import User
from app.models.eval import TestSuite, TestCase, TestRun, TestResult
from app.models.report import Report
from app.schemas.test_suite_schema import (
    TestSuiteSchema,
    TestSuiteCreate,
    TestSuiteUpdate,
    TestCaseSchema,
    TestCaseCreate,
    TestCaseUpdate,
    TestCaseStatusUpdate,
    TestRunSchema,
    TestRunCreate,
)
from app.schemas.test_dashboard_schema import TestMetricsSchema, TestSuiteSummarySchema
from app.services.test_suite_service import TestSuiteService
from app.services.test_case_service import TestCaseService
from app.services.test_run_service import TestRunService
from app.schemas.test_run_schema import (
    TestRunBatchCreate,
)
from app.schemas.test_results_schema import TestRunStatusResponse, TestResultSchema
from app.schemas.test_expectations import (
    ExpectationsSpec,
    FieldRule,
    ToolCallsRule,
    OrderingRule,
    TestCatalog,
)
from app.ai.registry import ToolRegistry
from app.core.eval_scope import (
    eval_agent_scope, can_view_case, filter_cases,
)


router = APIRouter(prefix="/tests", tags=["tests"])

suite_service = TestSuiteService()
case_service = TestCaseService()
run_service = TestRunService()


# The canonical resolution lives in app.core.eval_scope so the agent tools and
# these routes cannot drift — they did, and the tools denied every per-agent
# eval manager while the tool catalog still advertised the tools to them.
#
# Listings FILTER rather than 403: an agent manager asking for eval cases is
# asking about their own agents, and answering with a blanket 403 is what made
# the per-agent Evals panel render permanently empty.
_eval_agent_scope_impl = eval_agent_scope


async def _eval_agent_scope(db: AsyncSession, user: User, organization: Organization):
    return await _eval_agent_scope_impl(db, str(user.id), str(organization.id))


def _can_view_case(case, unscoped: bool, agent_ids: set) -> bool:
    return can_view_case(case, unscoped, agent_ids)


def _filter_cases(cases, unscoped: bool, agent_ids: set):
    return filter_cases(cases, unscoped, agent_ids)


def _narrow_to_agent(cases, data_source_id: Optional[str], scope: Optional[str]):
    """In-Python twin of ``agent_scope_clause``, for the already-materialized
    single-suite branch. Presentation only — authority is applied separately."""
    if not data_source_id and scope != "global":
        return cases
    out = []
    for c in cases:
        ds = {str(x) for x in (getattr(c, "data_source_ids_json", None) or [])}
        if not ds or (data_source_id and str(data_source_id) in ds):
            out.append(c)
    return out


async def _require_case_authority(
    db: AsyncSession, user: User, organization: Organization, case,
) -> None:
    """``manage_evals`` on every agent the case targets; org-level when it
    targets none (an agent-less case runs against every agent)."""
    ds_ids = [str(x) for x in (getattr(case, "data_source_ids_json", None) or [])]
    if ds_ids:
        await check_resource_permissions(
            db, str(user.id), str(organization.id),
            "data_source", ds_ids, "manage_evals",
        )
    else:
        await require_org_permission(
            db, str(user.id), str(organization.id), "manage_evals",
        )


async def _run_cases(db: AsyncSession, run_id: str):
    """The TestCases a run actually executed, via its results.

    Distinct is taken over case_id in a subquery, NOT over the entity. Postgres
    SELECT DISTINCT compares every selected column, and TestCase carries eight
    ``json`` columns — a type with no equality operator — so an entity-level
    DISTINCT dies with "could not identify an equality operator for type json".
    SQLite compares json as text and never complains, which is why this only
    surfaced on the Postgres CI leg.
    """
    case_ids = (
        select(TestResult.case_id)
        .where(TestResult.run_id == str(run_id))
        .distinct()
    )
    rows = (await db.execute(
        select(TestCase).where(TestCase.id.in_(case_ids))
    )).scalars().all()
    return list(rows)


async def _require_run_read(
    db: AsyncSession, user: User, organization: Organization, run_id: str,
):
    """Read authority over a run — union over the cases it executed.

    Returns the scope tuple so callers can filter the run's contents with the
    same resolution instead of resolving twice.

    Runs, results and transcripts were previously scoped by ORGANIZATION only:
    the route decorator's ``resource_scoped=True`` is an admission test (holds
    ``manage_evals`` org-wide OR on any one agent), and nothing past it narrowed
    by agent. A grant on a single agent therefore read every run in the org —
    including ``/results/{id}/transcript``, which renders the same
    MessageContextBuilder view the agent sees internally, tool digests and row
    counts included. That is the actual query output of agents the caller does
    not manage.
    """
    unscoped, agent_ids = await _eval_agent_scope(db, user, organization)
    if unscoped:
        return unscoped, agent_ids
    cases = await _run_cases(db, run_id)
    # EVERY case, not any: starting a run takes authority over all of it, so
    # reading one does too. Anything weaker would let a partial manager read
    # results — and transcripts — for cases they could never have launched.
    if not cases or not all(_can_view_case(c, unscoped, agent_ids) for c in cases):
        raise HTTPException(status_code=404, detail="Test run not found")
    return unscoped, agent_ids


async def _require_run_authority(
    db: AsyncSession, user: User, organization: Organization, run_id: str,
) -> None:
    """Write authority over a run — authority over EVERY case it executed.

    Stopping a run halts work on all of its cases, so it takes the same
    intersection ``run_suite`` requires to start one. The user who launched it
    necessarily passed that gate already.
    """
    for case in await _run_cases(db, run_id):
        await _require_case_authority(db, user, organization, case)


async def _suite_cases(db: AsyncSession, suite_id: str):
    rows = (await db.execute(
        select(TestCase).where(
            TestCase.suite_id == str(suite_id), TestCase.deleted_at.is_(None)
        )
    )).scalars().all()
    return list(rows)


async def _require_suite_authority(
    db: AsyncSession, user: User, organization: Organization, suite_id: str,
) -> None:
    """Authority to rename or delete a suite — authority over every case in it.

    A suite is a folder, not an agent-owned container: its cases carry the agent
    scope, and one case can target several agents (a routing eval), so the suite
    cannot own a single scope of its own. Authority therefore derives from what
    it holds. An EMPTY suite needs nothing beyond the admission check — creating
    and removing an empty folder harms no one.

    Deleting cascades (``TestSuite.cases`` is ``all, delete-orphan``), which is
    why this is an intersection rather than a union: it destroys every case in
    the suite. Callers that reparent foreign cases out first can therefore pass
    a suite they could not have deleted whole.

    An ORG-WIDE suite (no home agent) is org-level regardless of what it holds.
    Deriving from contents alone would let a per-agent manager rename or delete
    an EMPTY org-wide shelf, since an empty suite has no cases to fail on.
    """
    suite = await suite_service.get_suite(db, str(organization.id), user, suite_id)
    if getattr(suite, "data_source_id", None) is None:
        await require_org_permission(
            db, str(user.id), str(organization.id), "manage_evals",
        )
        return
    for case in await _suite_cases(db, suite_id):
        await _require_case_authority(db, user, organization, case)


# Suites
@router.post("/suites", response_model=TestSuiteSchema)
@requires_permission('manage_evals', resource_scoped=True)
async def create_suite(payload: TestSuiteCreate, db: AsyncSession = Depends(get_async_db), organization: Organization = Depends(get_current_organization), current_user: User = Depends(current_user)):
    # A suite homed on an agent takes manage_evals on that agent; an org-wide
    # one (no home) is an org-level shelf, so it takes org-level authority.
    if payload.data_source_id:
        await check_resource_permissions(
            db, str(current_user.id), str(organization.id),
            "data_source", [payload.data_source_id], "manage_evals",
        )
    else:
        # An org-wide shelf holds the cases that run against EVERY agent, so
        # creating one is org-level — the same bar as authoring an agent-less
        # case. Without this the decorator's admission test (manage_evals on any
        # ONE agent) was the only gate, and a single-agent manager could add
        # shelves to the org-wide tree.
        await require_org_permission(
            db, str(current_user.id), str(organization.id), "manage_evals",
        )
    suite = await suite_service.create_suite(
        db, str(organization.id), current_user, payload.name, payload.description,
        data_source_id=payload.data_source_id,
    )
    return suite


@router.get("/suites", response_model=List[TestSuiteSchema])
@requires_permission('manage_evals', resource_scoped=True)
async def list_suites(
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    search: Optional[str] = None,
    data_source_id: Optional[str] = None,
    scope: Optional[str] = None,
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user)
):
    suites = await suite_service.list_suites(
        db, str(organization.id), current_user, page, limit, search,
        data_source_id=data_source_id, scope=scope,
    )
    return suites


# Dashboard (mock data for now) - place before dynamic {suite_id} routes to avoid conflicts
@router.get("/metrics", response_model=TestMetricsSchema)
@requires_permission('manage_evals')
async def get_test_metrics(db: AsyncSession = Depends(get_async_db), organization: Organization = Depends(get_current_organization), current_user: User = Depends(current_user)):
    return await run_service.get_dashboard_metrics(db, str(organization.id), current_user)


@router.get("/suites/summary", response_model=List[TestSuiteSummarySchema])
@requires_permission('manage_evals', resource_scoped=True)
async def get_suite_summaries(db: AsyncSession = Depends(get_async_db), organization: Organization = Depends(get_current_organization), current_user: User = Depends(current_user)):
    scope = await _eval_agent_scope(db, current_user, organization)
    # Suite NAMES stay visible to anyone with manage_evals — the authoring
    # dropdown reads this list, and a folder name discloses nothing. The COUNTS
    # do disclose: an unfiltered tests_count tells a single-agent manager how
    # many evals exist for agents they cannot see.
    return await run_service.get_suites_summary(
        db, str(organization.id), current_user,
        case_filter=lambda cases: _filter_cases(cases, *scope),
    )


@router.get("/suites/{suite_id}", response_model=TestSuiteSchema)
@requires_permission('manage_evals', resource_scoped=True)
async def get_suite(suite_id: str, db: AsyncSession = Depends(get_async_db), organization: Organization = Depends(get_current_organization), current_user: User = Depends(current_user)):
    return await suite_service.get_suite(db, str(organization.id), current_user, suite_id)


@router.patch("/suites/{suite_id}", response_model=TestSuiteSchema)
@requires_permission('manage_evals', resource_scoped=True)
async def update_suite(suite_id: str, payload: TestSuiteUpdate, db: AsyncSession = Depends(get_async_db), organization: Organization = Depends(get_current_organization), current_user: User = Depends(current_user)):
    # Renaming is not destructive, but it renames a folder other people file
    # into, so it takes the same authority as emptying it.
    await _require_suite_authority(db, current_user, organization, suite_id)
    return await suite_service.update_suite(db, str(organization.id), current_user, suite_id, payload.name, payload.description)


@router.delete("/suites/{suite_id}")
@requires_permission('manage_evals', resource_scoped=True)
async def delete_suite(suite_id: str, db: AsyncSession = Depends(get_async_db), organization: Organization = Depends(get_current_organization), current_user: User = Depends(current_user)):
    # Reparent what the caller may not destroy, then require authority over the
    # rest. Without this, dropping one case into someone else's suite would make
    # it permanently undeletable by its owner — a lock-out anyone holding a
    # single agent grant could inflict on any suite.
    reparented = await suite_service.reparent_unauthorized_cases(
        db, str(organization.id), current_user, organization, suite_id,
        _require_case_authority,
    )
    await _require_suite_authority(db, current_user, organization, suite_id)
    await suite_service.delete_suite(db, str(organization.id), current_user, suite_id)
    # Report the reparented count: the delete is PARTIAL whenever it is non-zero,
    # and a caller told only "deleted" would believe the suite's whole contents
    # went with it.
    return {"status": "deleted", "reparented": reparented}


# Suites — YAML import / export
@router.post("/suites/import")
@requires_permission('manage_evals')
async def import_suite_yaml(
    request: Request,
    strategy: str = Query("upsert", pattern="^(upsert|replace)$"),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user),
):
    """Upsert a suite and its cases from a YAML body.

    Body: raw YAML (``text/yaml`` or ``application/x-yaml``; body is read as
    bytes so any text content type works). ``strategy=replace`` hard-deletes
    cases absent from the YAML; default ``upsert`` leaves them intact.
    """
    body = await request.body()
    if not body:
        raise HTTPException(status_code=400, detail="Empty YAML body")
    try:
        yaml_text = body.decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="YAML body must be UTF-8")
    return await suite_service.import_yaml(
        db, str(organization.id), current_user, yaml_text, strategy=strategy,
    )


@router.get("/suites/{suite_id}/export", response_class=PlainTextResponse)
@requires_permission('manage_evals')
async def export_suite_yaml(
    suite_id: str,
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user),
):
    """Serialize a suite and its cases to YAML text."""
    return await suite_service.export_yaml(
        db, str(organization.id), current_user, suite_id,
    )


# Cases
@router.post("/suites/{suite_id}/cases", response_model=TestCaseSchema)
@requires_permission('manage_evals', resource_scoped=True)
async def create_case(suite_id: str, payload: TestCaseCreate, db: AsyncSession = Depends(get_async_db), organization: Organization = Depends(get_current_organization), current_user: User = Depends(current_user)):
    if payload.data_source_ids_json:
        await check_resource_permissions(
            db, str(current_user.id), str(organization.id),
            "data_source", payload.data_source_ids_json, "manage_evals",
        )
    else:
        # Truly org-wide (no data source) → stays an org-level capability.
        # An agent admin's per-DS `manage` (which implies manage_evals on that
        # agent) must NOT let them author a global eval that runs against every
        # agent. Mirrors the /instructions/global gate.
        await require_org_permission(
            db, str(current_user.id), str(organization.id), "manage_evals",
        )
    case = await case_service.create_case(db, str(organization.id), current_user, suite_id, payload.name, payload.prompt_json, payload.expectations_json, payload.data_source_ids_json)
    return case


@router.get("/suites/{suite_id}/cases", response_model=List[TestCaseSchema])
@requires_permission('manage_evals', resource_scoped=True)
async def list_cases(suite_id: str, db: AsyncSession = Depends(get_async_db), organization: Organization = Depends(get_current_organization), current_user: User = Depends(current_user)):
    cases = await case_service.list_cases(db, str(organization.id), current_user, suite_id)
    return _filter_cases(cases, *await _eval_agent_scope(db, current_user, organization))


@router.get("/cases", response_model=List[TestCaseSchema])
@requires_permission('manage_evals', resource_scoped=True)
async def list_cases_across_suites(
    suite_id: Optional[str] = None,
    suite_ids: Optional[List[str]] = Query(None),
    search: Optional[str] = None,
    page: int = Query(1, ge=1),
    limit: int = Query(100, ge=1, le=1000),
    data_source_id: Optional[str] = None,
    scope: Optional[str] = None,
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user),
):
    """List test cases across suites, optionally narrowed to one agent.

    ``data_source_id`` returns the cases that agent is tested by (its own, plus
    the agent-less ones that run against every agent); ``scope=global`` returns
    only the agent-less ones. Narrowing happens in SQL so ``limit`` bounds the
    agent's cases and not the organization's.

    ``suite_ids`` narrows by filing location instead of by target — what the
    agent tree needs for the cases sitting in this agent's suites, which may
    include one dragged in that targets someone else.
    """
    authority = await _eval_agent_scope(db, current_user, organization)
    if suite_id:
        cases = await case_service.list_cases(db, str(organization.id), current_user, suite_id)
        cases = _narrow_to_agent(cases, data_source_id, scope)
    else:
        cases = await case_service.list_cases_multi(
            db, str(organization.id), current_user, suite_ids=suite_ids or None, search=search,
            page=page, limit=limit, data_source_id=data_source_id, scope=scope,
        )
    return _filter_cases(cases, *authority)


@router.get("/cases/{case_id}", response_model=TestCaseSchema)
@requires_permission('manage_evals', resource_scoped=True)
async def get_case(case_id: str, db: AsyncSession = Depends(get_async_db), organization: Organization = Depends(get_current_organization), current_user: User = Depends(current_user)):
    case = await case_service.get_case(db, str(organization.id), current_user, case_id)
    await _require_case_authority(db, current_user, organization, case)
    return case


@router.patch("/cases/{case_id}", response_model=TestCaseSchema)
@requires_permission('manage_evals', resource_scoped=True)
async def update_case(case_id: str, payload: TestCaseUpdate, db: AsyncSession = Depends(get_async_db), organization: Organization = Depends(get_current_organization), current_user: User = Depends(current_user)):
    # Authority over the case as it stands today — otherwise a manager of agent
    # A could retarget someone else's case onto A and take it over.
    await _require_case_authority(
        db, current_user, organization,
        await case_service.get_case(db, str(organization.id), current_user, case_id),
    )
    # `is not None`: an empty list is falsy, so a truthiness check let a
    # per-agent evaluator clear the scope and turn their case into an org-wide
    # one, bypassing the create-time gate above.
    if payload.data_source_ids_json is not None:
        if payload.data_source_ids_json:
            await check_resource_permissions(
                db, str(current_user.id), str(organization.id),
                "data_source", payload.data_source_ids_json, "manage_evals",
            )
        else:
            await require_org_permission(
                db, str(current_user.id), str(organization.id), "manage_evals",
            )
    return await case_service.update_case(db, str(organization.id), current_user, case_id, payload.name, payload.prompt_json, payload.expectations_json, payload.data_source_ids_json, suite_id=payload.suite_id)


@router.patch("/cases/{case_id}/status", response_model=TestCaseSchema)
@requires_permission('manage_evals', resource_scoped=True)
async def update_case_status(
    case_id: str,
    payload: TestCaseStatusUpdate,
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user),
):
    """Promote a draft to ``active`` or archive any case. Used by the
    auto-suggest-evals review flow."""
    await _require_case_authority(
        db, current_user, organization,
        await case_service.get_case(db, str(organization.id), current_user, case_id),
    )
    return await case_service.update_case_status(
        db, str(organization.id), current_user, case_id, payload.status,
    )


@router.delete("/cases/{case_id}")
@requires_permission('manage_evals', resource_scoped=True)
async def delete_case(case_id: str, db: AsyncSession = Depends(get_async_db), organization: Organization = Depends(get_current_organization), current_user: User = Depends(current_user)):
    await _require_case_authority(
        db, current_user, organization,
        await case_service.get_case(db, str(organization.id), current_user, case_id),
    )
    await case_service.delete_case(db, str(organization.id), current_user, case_id)
    return {"status": "deleted"}


# Runs
@router.post("/suites/{suite_id}/runs", response_model=TestRunSchema)
@requires_permission('manage_evals', resource_scoped=True)
async def run_suite(suite_id: str, background: bool = Query(True), db: AsyncSession = Depends(get_async_db), organization: Organization = Depends(get_current_organization), current_user: User = Depends(current_user)):
    for _c in await case_service.list_cases(db, str(organization.id), current_user, suite_id):
        await _require_case_authority(db, current_user, organization, _c)
    run = await run_service.run_suite(db, organization, current_user, suite_id, background=background)
    return run


@router.post("/runs", response_model=TestRunSchema)
@requires_permission('manage_evals', resource_scoped=True)
async def create_run(payload: TestRunCreate, db: AsyncSession = Depends(get_async_db), organization: Organization = Depends(get_current_organization), current_user: User = Depends(current_user)):
    for _cid in (payload.case_ids or []):
        await _require_case_authority(
            db, current_user, organization,
            await case_service.get_case(db, str(organization.id), current_user, _cid),
        )
    run = await run_service.create_run(db, organization, current_user, case_ids=payload.case_ids, trigger_reason=payload.trigger_reason or "manual", build_id=payload.build_id)
    return run


@router.get("/runs", response_model=List[TestRunSchema])
@requires_permission('manage_evals', resource_scoped=True)
async def list_runs(
    suite_id: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    data_source_id: Optional[str] = None,
    scope: Optional[str] = None,
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user)
):
    """List runs, optionally narrowed to the ones that touched one agent.

    ``data_source_id`` keeps a run when any case it executed concerns that agent;
    ``scope=global`` keeps a run that executed at least one agent-less case. A
    run spanning both worlds appears under both, which is right — it happened in
    both. As with cases, the narrowing is in SQL so ``limit`` bounds one agent's
    history rather than the organization's.
    """
    runs = await run_service.list_runs(
        db, str(organization.id), current_user, suite_id=suite_id, status=status,
        page=page, limit=limit, data_source_id=data_source_id, scope=scope,
    )
    unscoped, agent_ids = await _eval_agent_scope(db, current_user, organization)
    if unscoped:
        return runs
    # A run is visible only when the caller may read EVERY case it executed.
    visible = []
    for r in runs:
        cases = await _run_cases(db, str(r.id))
        if cases and all(_can_view_case(c, unscoped, agent_ids) for c in cases):
            visible.append(r)
    return visible


@router.get("/runs/{run_id}", response_model=TestRunSchema)
@requires_permission('manage_evals', resource_scoped=True)
async def get_run(run_id: str, db: AsyncSession = Depends(get_async_db), organization: Organization = Depends(get_current_organization), current_user: User = Depends(current_user)):
    await _require_run_read(db, current_user, organization, run_id)
    return await run_service.get_run(db, str(organization.id), current_user, run_id)

@router.post("/runs/{run_id}/stop", response_model=TestRunSchema)
@requires_permission('manage_evals', resource_scoped=True)
async def stop_run(run_id: str, db: AsyncSession = Depends(get_async_db), organization: Organization = Depends(get_current_organization), current_user: User = Depends(current_user)):
    await _require_run_authority(db, current_user, organization, run_id)
    return await run_service.stop_run(db, str(organization.id), current_user, run_id)


@router.get("/runs/{run_id}/results", response_model=List[TestResultSchema])
@requires_permission('manage_evals', resource_scoped=True)
async def list_results(run_id: str, db: AsyncSession = Depends(get_async_db), organization: Organization = Depends(get_current_organization), current_user: User = Depends(current_user)):
    # The run gate already required authority over every case in this run, so
    # there is nothing left to filter out row by row.
    await _require_run_read(db, current_user, organization, run_id)
    return await run_service.list_results(db, str(organization.id), current_user, run_id)


async def _require_result_read(
    db: AsyncSession, user: User, organization: Organization, result_id: str,
) -> None:
    """Read authority over ONE result — the case it belongs to, union-scoped.

    The transcript route matters most here: it renders the same
    MessageContextBuilder view the agent sees internally, so it carries the run's
    actual data output. A 404 (not 403) keeps the existence of another agent's
    result unobservable.
    """
    unscoped, agent_ids = await _eval_agent_scope(db, user, organization)
    if unscoped:
        return
    case = (await db.execute(
        select(TestCase)
        .join(TestResult, TestResult.case_id == TestCase.id)
        .where(TestResult.id == str(result_id))
    )).scalar_one_or_none()
    if case is None or not _can_view_case(case, unscoped, agent_ids):
        raise HTTPException(status_code=404, detail="Test result not found")


@router.get("/results/{result_id}", response_model=TestResultSchema)
@requires_permission('manage_evals', resource_scoped=True)
async def get_result(result_id: str, db: AsyncSession = Depends(get_async_db), organization: Organization = Depends(get_current_organization), current_user: User = Depends(current_user)):
    await _require_result_read(db, current_user, organization, result_id)
    return await run_service.get_result(db, str(organization.id), current_user, result_id)


@router.get("/results/{result_id}/transcript", response_class=PlainTextResponse)
@requires_permission('manage_evals', resource_scoped=True)
async def get_result_transcript(
    result_id: str,
    max_messages: int = Query(40, ge=1, le=200),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user),
):
    """Plain-text transcript for a test result, rendered by
    ``MessageContextBuilder`` — same view the agent sees internally
    (user turns + assistant thinking/responses + tool digests)."""
    await _require_result_read(db, current_user, organization, result_id)
    return await run_service.get_result_transcript(
        db, organization, current_user, result_id, max_messages=max_messages,
    )


@router.get("/rules/catalog")
@requires_permission('manage_evals', resource_scoped=True)
async def get_rules_catalog(db: AsyncSession = Depends(get_async_db), organization: Organization = Depends(get_current_organization), current_user: User = Depends(current_user)):
    """Return a catalog describing available rule types, scopes/operators, and tool schemas.

    This powers the frontend rule builder; it is read-only and organization-agnostic aside
    from tool availability.
    """
    registry = ToolRegistry()

    def _schema_for(obj):
        try:
            if hasattr(obj, "model_json_schema"):
                return obj.model_json_schema()
            if hasattr(obj, "schema") and callable(getattr(obj, "schema")):
                return obj.schema()
        except Exception:
            return None
        return None

    tools_catalog = []
    try:
        for meta in registry.get_catalog(organization=str(organization.id)):
            input_schema = _schema_for(meta.get("schema"))  # metadata.input_schema if provided
            # Some registries expose input/output separately; attempt to fetch from a live instance
            output_schema = None
            tool_instance = registry.get(meta.get("name"))
            if tool_instance is not None:
                try:
                    if hasattr(tool_instance, "InputModel"):
                        input_schema = _schema_for(getattr(tool_instance, "InputModel")) or input_schema
                    if hasattr(tool_instance, "OutputModel"):
                        output_schema = _schema_for(getattr(tool_instance, "OutputModel"))
                except Exception:
                    pass

            tools_catalog.append({
                "name": meta.get("name"),
                "description": meta.get("description"),
                "category": meta.get("category"),
                "version": meta.get("version"),
                "input_schema": input_schema,
                "output_schema": output_schema,
                "probes": [],  # Optional curated probes; can be filled per tool later
            })
    except Exception:
        tools_catalog = []

    scopes = [
        {
            "id": "completion",
            "label": "Final answer",
            "default_selectors": ["$.content"],
            "probes": [
                {"label": "Answer contains", "selector": "$.content", "matcher_type": "contains"},
            ],
        },
        {
            "id": "agent_execution",
            "label": "Agent execution",
            "default_selectors": ["$.status", "$.stats.latency_ms", "$.tokens.output"],
            "probes": [
                {"label": "Latency <= ms", "selector": "$.stats.latency_ms", "matcher_type": "num_cmp", "op_suggestions": ["lte", "lt"]},
            ],
        },
        {"id": "tool_input", "label": "Tool input", "default_selectors": [], "probes": []},
        {"id": "tool_output", "label": "Tool output", "default_selectors": [], "probes": []},
        {"id": "result", "label": "Test result", "default_selectors": ["$.status"], "probes": []},
        {"id": "metric", "label": "Run metrics", "default_selectors": ["$.output_tokens", "$.input_tokens"], "probes": []},
    ]

    operators = {
        "text": ["contains", "equals", "not_contains", "regex", "starts_with", "ends_with"],
        "numeric": ["gt", "gte", "lt", "lte", "eq", "ne"],
        "matcher_types": ["contains", "equals", "regex", "jsonpath_contains", "num_cmp"],
    }

    return {
        "spec_version": 1,
        "rule_types": {
            "ExpectationsSpec": ExpectationsSpec.model_json_schema(),
            "FieldRule": FieldRule.model_json_schema(),
            "ToolCallsRule": ToolCallsRule.model_json_schema(),
            "OrderingRule": OrderingRule.model_json_schema(),
        },
        # Fixed curated selectors for the UI
        "selectors": [
            {"id": "create_widget.input.tables", "group": "Tools · create_widget", "label": "Input · tables includes", "scope": "tool_input", "valueType": "text"},
            {"id": "create_widget.output.data.columns", "group": "Tools · create_widget", "label": "Output · data.columns includes", "scope": "tool_output", "valueType": "text"},
            {"id": "create_widget.output.code", "group": "Tools · create_widget", "label": "Output · code includes", "scope": "tool_output", "valueType": "text"},
            {"id": "create_widget.output.data.rows", "group": "Tools · create_widget", "label": "Output · data.rows count", "scope": "tool_output", "valueType": "count"},
            {"id": "describe_table.input.name", "group": "Tools · describe_table", "label": "Input · name", "scope": "tool_input", "valueType": "text"},
            {"id": "create_dashboard.output.items.count", "group": "Tools · create_dashboard", "label": "Output · items count", "scope": "tool_output", "valueType": "count"},
            {"id": "clarify.exists", "group": "Tools", "label": "clarify called (exists)", "scope": "tool_calls", "valueType": "exists"},
            {"id": "metadata.total_duration_ms", "group": "Agent Execution", "label": "total_duration_ms", "scope": "agent_execution", "valueType": "numeric"},
            {"id": "completion.messages", "group": "Completion", "label": "Messages contains", "scope": "message", "valueType": "text"},
            {"id": "completion.reasoning", "group": "Completion", "label": "Reasoning contains", "scope": "completion", "valueType": "text"},
        ],
        "scopes": scopes,
        "operators": operators,
        "tools": tools_catalog,
    }


# New: simple catalog endpoint powering the UI pickers
# The expectation catalogs are STATIC descriptors — rule types, field names and
# the org's Judge model options — with nothing agent-specific in them. Gating
# them org-only meant a per-agent evaluator could create a case (those routes are
# resource-scoped) and then got a 403 the moment they tried to add an expectation
# to it, which is the whole point of authoring one.
@router.get("/catalog", response_model=TestCatalog)
@requires_permission('manage_evals', resource_scoped=True)
async def get_test_catalog(db: AsyncSession = Depends(get_async_db), organization: Organization = Depends(get_current_organization), current_user: User = Depends(current_user)):
    return await suite_service.get_test_catalog(db, str(organization.id), current_user)


# ---------------- New Run APIs ----------------

@router.post("/runs/batch", response_model=TestRunSchema)
@requires_permission('manage_evals', resource_scoped=True)
async def create_run_batch(payload: TestRunBatchCreate, db: AsyncSession = Depends(get_async_db), organization: Organization = Depends(get_current_organization), current_user: User = Depends(current_user)):
    # Executing a case is a write-shaped act, so this takes the same per-case
    # intersection as POST /runs and /suites/{id}/runs.
    for _cid in (payload.case_ids or []):
        await _require_case_authority(
            db, current_user, organization,
            await case_service.get_case(db, str(organization.id), current_user, _cid),
        )
    if payload.suite_id and not payload.case_ids:
        for _c in await _suite_cases(db, str(payload.suite_id)):
            await _require_case_authority(db, current_user, organization, _c)
    # The origin conversation is named by the client and finishing the run
    # POSTS A COMPLETION INTO IT, so this is a write check, not a read one.
    # Deliberately narrower than `view_reports`: the caller must own the
    # conversation, not merely be able to see it. The strip only ever fires
    # from the chat you are sitting in, so ownership costs nothing real, and
    # it makes the field useless as a way to inject a message into someone
    # else's thread. Unknown or someone else's report → no wake, and the run
    # still executes; failing closed here must not fail the run.
    origin_report = None
    if payload.origin_report_id:
        origin_report = (await db.execute(
            select(Report).where(
                Report.id == str(payload.origin_report_id),
                Report.organization_id == str(organization.id),
                Report.user_id == str(current_user.id),
                Report.deleted_at.is_(None),
            )
        )).scalar_one_or_none()
        if origin_report is None:
            raise HTTPException(status_code=404, detail="Report not found")
    origin_report_id = str(origin_report.id) if origin_report is not None else None
    run, _results = await run_service.create_and_execute_background(
        db, organization, current_user,
        case_ids=payload.case_ids,
        suite_id=payload.suite_id,
        trigger_reason=payload.trigger_reason or "manual",
        build_id=payload.build_id,
        origin_report_id=origin_report_id,
        origin_user_id=str(current_user.id) if origin_report_id else None,
        wake_on_finish=bool(payload.wake_on_finish and origin_report_id),
    )
    # Leave a mark in the conversation that a person kicked this off — the same
    # silent, ambient strip a model switch leaves. Deliberately a session event
    # and not a turn: it starts nothing, costs no LLM call, and must never fail
    # the run it is only annotating (hence emit_safe).
    if origin_report is not None:
        from app.services.session_event_service import SessionEventService
        from app.ai.context.session_events import EVAL_RUN_STARTED
        await SessionEventService.emit_safe(
            db, report=origin_report, kind=EVAL_RUN_STARTED, user=current_user,
            meta={
                "run_id": str(run.id),
                "total": len(_results or []),
                "build_id": str(payload.build_id) if payload.build_id else None,
            },
        )
    return run


@router.get("/runs/{run_id}/compare")
@requires_permission('manage_evals', resource_scoped=True)
async def compare_runs(run_id: str, against_run_id: Optional[str] = Query(None), db: AsyncSession = Depends(get_async_db), organization: Organization = Depends(get_current_organization), current_user: User = Depends(current_user)):
    """Per-case diff of this run vs. a baseline (default: latest prior terminal run sharing >=1 case)."""
    await _require_run_read(db, current_user, organization, run_id)
    if against_run_id:
        await _require_run_read(db, current_user, organization, against_run_id)
    return await run_service.compare_runs(db, str(organization.id), current_user, run_id, against_run_id=against_run_id)


@router.get("/runs/{run_id}/status", response_model=TestRunStatusResponse)
@requires_permission('manage_evals', resource_scoped=True)
async def get_run_status(run_id: str, limit: int = Query(50, ge=1, le=200), db: AsyncSession = Depends(get_async_db), organization: Organization = Depends(get_current_organization), current_user: User = Depends(current_user)):
    # A per-agent evaluator can start a run; without this they could not watch
    # the one they started.
    await _require_run_read(db, current_user, organization, run_id)
    run, items = await run_service.get_run_status_with_completions(db, organization, current_user, run_id, limit=limit)
    # Convert to pydantic response
    from app.schemas.test_results_schema import TestRunResultWithCompletions
    results = []
    for it in items:
        results.append(TestRunResultWithCompletions(
            result=it["result"],
            report_id=it["report_id"],
            completions=it["completions"],
        ))
    return TestRunStatusResponse(run=run, results=results)


@router.post("/runs/{run_id}/stream")
@requires_permission('manage_evals', resource_scoped=True)
async def stream_run(run_id: str, db: AsyncSession = Depends(get_async_db), organization: Organization = Depends(get_current_organization), current_user: User = Depends(current_user)):
    await _require_run_read(db, current_user, organization, run_id)
    return await run_service.stream_run(db, organization, current_user, run_id)



