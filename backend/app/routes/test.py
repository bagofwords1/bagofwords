from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from typing import List, Optional

from app.dependencies import get_async_db, get_current_organization
from app.core.auth import current_user
from app.core.permissions_decorator import requires_permission
from app.models.organization import Organization
from app.models.user import User
from app.models.eval import TestSuite, TestCase, TestRun, TestResult
from app.schemas.test_suite_schema import (
    TestSuiteSchema,
    TestSuiteCreate,
    TestSuiteUpdate,
    TestCaseSchema,
    TestCaseCreate,
    TestCaseUpdate,
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


router = APIRouter(prefix="/tests", tags=["tests"])

suite_service = TestSuiteService()
case_service = TestCaseService()
run_service = TestRunService()


# Suites
@router.post("/suites", response_model=TestSuiteSchema)
@requires_permission('manage_tests')
async def create_suite(payload: TestSuiteCreate, db: AsyncSession = Depends(get_async_db), organization: Organization = Depends(get_current_organization), current_user: User = Depends(current_user)):
    suite = await suite_service.create_suite(db, str(organization.id), current_user, payload.name, payload.description)
    return suite


@router.get("/suites", response_model=List[TestSuiteSchema])
@requires_permission('manage_tests')
async def list_suites(
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    search: Optional[str] = None,
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user)
):
    suites = await suite_service.list_suites(db, str(organization.id), current_user, page, limit, search)
    return suites


# Dashboard (mock data for now) - place before dynamic {suite_id} routes to avoid conflicts
@router.get("/metrics", response_model=TestMetricsSchema)
@requires_permission('manage_tests')
async def get_test_metrics(db: AsyncSession = Depends(get_async_db), organization: Organization = Depends(get_current_organization), current_user: User = Depends(current_user)):
    return await run_service.get_dashboard_metrics(db, str(organization.id), current_user)


@router.get("/suites/summary", response_model=List[TestSuiteSummarySchema])
@requires_permission('manage_tests')
async def get_suite_summaries(db: AsyncSession = Depends(get_async_db), organization: Organization = Depends(get_current_organization), current_user: User = Depends(current_user)):
    return await run_service.get_suites_summary(db, str(organization.id), current_user)


@router.get("/suites/{suite_id}", response_model=TestSuiteSchema)
@requires_permission('manage_tests')
async def get_suite(suite_id: str, db: AsyncSession = Depends(get_async_db), organization: Organization = Depends(get_current_organization), current_user: User = Depends(current_user)):
    return await suite_service.get_suite(db, str(organization.id), current_user, suite_id)


@router.patch("/suites/{suite_id}", response_model=TestSuiteSchema)
@requires_permission('manage_tests')
async def update_suite(suite_id: str, payload: TestSuiteUpdate, db: AsyncSession = Depends(get_async_db), organization: Organization = Depends(get_current_organization), current_user: User = Depends(current_user)):
    return await suite_service.update_suite(db, str(organization.id), current_user, suite_id, payload.name, payload.description)


@router.delete("/suites/{suite_id}")
@requires_permission('manage_tests')
async def delete_suite(suite_id: str, db: AsyncSession = Depends(get_async_db), organization: Organization = Depends(get_current_organization), current_user: User = Depends(current_user)):
    await suite_service.delete_suite(db, str(organization.id), current_user, suite_id)
    return {"status": "deleted"}


# Cases
@router.post("/suites/{suite_id}/cases", response_model=TestCaseSchema)
@requires_permission('manage_tests')
async def create_case(suite_id: str, payload: TestCaseCreate, db: AsyncSession = Depends(get_async_db), organization: Organization = Depends(get_current_organization), current_user: User = Depends(current_user)):
    case = await case_service.create_case(db, str(organization.id), current_user, suite_id, payload.name, payload.prompt_json, payload.expectations_json, payload.data_source_ids_json)
    return case


@router.get("/suites/{suite_id}/cases", response_model=List[TestCaseSchema])
@requires_permission('manage_tests')
async def list_cases(suite_id: str, db: AsyncSession = Depends(get_async_db), organization: Organization = Depends(get_current_organization), current_user: User = Depends(current_user)):
    return await case_service.list_cases(db, str(organization.id), current_user, suite_id)


@router.get("/cases", response_model=List[TestCaseSchema])
@requires_permission('manage_tests')
async def list_cases_across_suites(
    suite_id: Optional[str] = None,
    search: Optional[str] = None,
    page: int = Query(1, ge=1),
    limit: int = Query(100, ge=1, le=1000),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user),
):
    """List test cases across suites with optional suite and text search filters."""
    if suite_id:
        return await case_service.list_cases(db, str(organization.id), current_user, suite_id)
    return await case_service.list_cases_multi(db, str(organization.id), current_user, suite_ids=None, search=search, page=page, limit=limit)


@router.get("/cases/{case_id}", response_model=TestCaseSchema)
@requires_permission('manage_tests')
async def get_case(case_id: str, db: AsyncSession = Depends(get_async_db), organization: Organization = Depends(get_current_organization), current_user: User = Depends(current_user)):
    return await case_service.get_case(db, str(organization.id), current_user, case_id)


@router.patch("/cases/{case_id}", response_model=TestCaseSchema)
@requires_permission('manage_tests')
async def update_case(case_id: str, payload: TestCaseUpdate, db: AsyncSession = Depends(get_async_db), organization: Organization = Depends(get_current_organization), current_user: User = Depends(current_user)):
    return await case_service.update_case(db, str(organization.id), current_user, case_id, payload.name, payload.prompt_json, payload.expectations_json, payload.data_source_ids_json)


@router.delete("/cases/{case_id}")
@requires_permission('manage_tests')
async def delete_case(case_id: str, db: AsyncSession = Depends(get_async_db), organization: Organization = Depends(get_current_organization), current_user: User = Depends(current_user)):
    await case_service.delete_case(db, str(organization.id), current_user, case_id)
    return {"status": "deleted"}


# Runs
@router.post("/suites/{suite_id}/runs", response_model=TestRunSchema)
@requires_permission('manage_tests')
async def run_suite(suite_id: str, background: bool = Query(True), db: AsyncSession = Depends(get_async_db), organization: Organization = Depends(get_current_organization), current_user: User = Depends(current_user)):
    run = await run_service.run_suite(db, organization, current_user, suite_id, background=background)
    return run


@router.post("/runs", response_model=TestRunSchema)
@requires_permission('manage_tests')
async def create_run(payload: TestRunCreate, db: AsyncSession = Depends(get_async_db), organization: Organization = Depends(get_current_organization), current_user: User = Depends(current_user)):
    run = await run_service.create_run(db, organization, current_user, case_ids=payload.case_ids, trigger_reason=payload.trigger_reason or "manual", build_id=payload.build_id)
    return run


@router.get("/runs", response_model=List[TestRunSchema])
@requires_permission('manage_tests')
async def list_runs(
    suite_id: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user)
):
    runs = await run_service.list_runs(db, str(organization.id), current_user, suite_id=suite_id, status=status, page=page, limit=limit)
    return runs


@router.get("/runs/{run_id}", response_model=TestRunSchema)
@requires_permission('manage_tests')
async def get_run(run_id: str, db: AsyncSession = Depends(get_async_db), organization: Organization = Depends(get_current_organization), current_user: User = Depends(current_user)):
    return await run_service.get_run(db, str(organization.id), current_user, run_id)

@router.post("/runs/{run_id}/stop", response_model=TestRunSchema)
@requires_permission('manage_tests')
async def stop_run(run_id: str, db: AsyncSession = Depends(get_async_db), organization: Organization = Depends(get_current_organization), current_user: User = Depends(current_user)):
    return await run_service.stop_run(db, str(organization.id), current_user, run_id)


@router.get("/runs/{run_id}/results", response_model=List[TestResultSchema])
@requires_permission('manage_tests')
async def list_results(run_id: str, db: AsyncSession = Depends(get_async_db), organization: Organization = Depends(get_current_organization), current_user: User = Depends(current_user)):
    return await run_service.list_results(db, str(organization.id), current_user, run_id)


@router.get("/results/{result_id}", response_model=TestResultSchema)
@requires_permission('manage_tests')
async def get_result(result_id: str, db: AsyncSession = Depends(get_async_db), organization: Organization = Depends(get_current_organization), current_user: User = Depends(current_user)):
    return await run_service.get_result(db, str(organization.id), current_user, result_id)


@router.get("/rules/catalog")
@requires_permission('manage_tests')
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
            {"id": "answer_question.output.content", "group": "Tools · answer_question", "label": "Output · content includes", "scope": "tool_output", "valueType": "text"},
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
@router.get("/catalog", response_model=TestCatalog)
@requires_permission('manage_tests')
async def get_test_catalog(db: AsyncSession = Depends(get_async_db), organization: Organization = Depends(get_current_organization), current_user: User = Depends(current_user)):
    return await suite_service.get_test_catalog(db, str(organization.id), current_user)


# ---------------- New Run APIs ----------------

@router.post("/runs/batch", response_model=TestRunSchema)
@requires_permission('manage_tests')
async def create_run_batch(payload: TestRunBatchCreate, db: AsyncSession = Depends(get_async_db), organization: Organization = Depends(get_current_organization), current_user: User = Depends(current_user)):
    run, _results = await run_service.create_and_execute_background(
        db, organization, current_user,
        case_ids=payload.case_ids,
        suite_id=payload.suite_id,
        trigger_reason=payload.trigger_reason or "manual",
        build_id=payload.build_id,
    )
    return run


@router.get("/runs/{run_id}/status", response_model=TestRunStatusResponse)
@requires_permission('manage_tests')
async def get_run_status(run_id: str, limit: int = Query(50, ge=1, le=200), db: AsyncSession = Depends(get_async_db), organization: Organization = Depends(get_current_organization), current_user: User = Depends(current_user)):
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
@requires_permission('manage_tests')
async def stream_run(run_id: str, db: AsyncSession = Depends(get_async_db), organization: Organization = Depends(get_current_organization), current_user: User = Depends(current_user)):
    return await run_service.stream_run(db, organization, current_user, run_id)



