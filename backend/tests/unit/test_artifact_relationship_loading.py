"""Artifact/query reads must stay bounded to the object explicitly requested."""

# Mapper registration intentionally runs before the app-model imports below.
# ruff: noqa: E402

from __future__ import annotations

import re
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# Register the same mapped model graph as Alembic before creating the in-memory
# schema. This keeps relationship-loader behavior identical to the app.
_env_src = (Path(__file__).resolve().parents[2] / "alembic" / "env.py").read_text()
for _stmt in re.findall(r"^from app\.models\S* import \([^)]*\)|^from app\.models[^\n]+", _env_src, re.M):
    exec(_stmt)  # noqa: S102 — test-only, mirrors alembic/env.py

from app.ai.agent_v2 import AgentV2
from app.ai.tools.implementations.edit_artifact import EditArtifactTool
from app.ai.tools.implementations.read_artifact import ReadArtifactTool
from app.ai.tools.implementations.read_query import ReadQueryTool
from app.models.artifact import Artifact
from app.models.base import Base
from app.models.completion import Completion
from app.models.organization import Organization
from app.models.query import Query
from app.models.report import Report
from app.models.step import Step
from app.models.user import User
from app.models.visualization import Visualization
from app.models.widget import Widget


@pytest_asyncio.fixture
async def artifact_context():
    Completion.__table__.c.sigkill.nullable = True
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with maker() as db:
        user = User(name="Artifact owner", email=f"artifact-{uuid.uuid4()}@example.test", hashed_password="x")
        db.add(user)
        await db.flush()
        organization = Organization(name=f"Artifact org {uuid.uuid4()}")
        db.add(organization)
        await db.flush()
        report = Report(
            title="Artifact-heavy report",
            slug=f"artifact-report-{uuid.uuid4()}",
            user_id=str(user.id),
            organization_id=str(organization.id),
        )
        db.add(report)
        await db.flush()

        requested_widget = Widget(
            title="Requested widget",
            slug=f"requested-widget-{uuid.uuid4()}",
            report_id=str(report.id),
        )
        sibling_widget = Widget(
            title="Unrelated widget",
            slug=f"unrelated-widget-{uuid.uuid4()}",
            report_id=str(report.id),
        )
        db.add_all([requested_widget, sibling_widget])
        await db.flush()

        requested_query = Query(
            title="Requested query",
            report_id=str(report.id),
            widget_id=str(requested_widget.id),
            organization_id=str(organization.id),
            user_id=str(user.id),
        )
        sibling_query = Query(
            title="Unrelated query",
            report_id=str(report.id),
            widget_id=str(sibling_widget.id),
            organization_id=str(organization.id),
            user_id=str(user.id),
        )
        db.add_all([requested_query, sibling_query])
        await db.flush()

        requested_old_step = Step(
            title="Requested historical step",
            slug=f"requested-old-step-{uuid.uuid4()}",
            status="success",
            prompt="select old",
            code="SELECT 'old'",
            data={"columns": ["value"], "rows": [["old"]]},
            data_model={"type": "table"},
            view={"type": "table"},
            description="Historical version of the requested query",
            widget_id=str(requested_widget.id),
            query_id=str(requested_query.id),
        )
        requested_step = Step(
            title="Requested current step",
            slug=f"requested-step-{uuid.uuid4()}",
            status="success",
            prompt="select requested",
            code="SELECT 'requested'",
            data={"columns": ["value"], "rows": [["requested"]]},
            data_model={"type": "table"},
            view={"type": "table"},
            description="Current version of the requested query",
            widget_id=str(requested_widget.id),
            query_id=str(requested_query.id),
        )
        sibling_step = Step(
            title="Unrelated step",
            slug=f"unrelated-step-{uuid.uuid4()}",
            status="success",
            prompt="select unrelated",
            code="SELECT 'unrelated'",
            data={"columns": ["value"], "rows": [["unrelated"]]},
            data_model={"type": "table"},
            view={"type": "table"},
            description="Must not be hydrated by read_query",
            widget_id=str(sibling_widget.id),
            query_id=str(sibling_query.id),
        )
        db.add_all([requested_old_step, requested_step, sibling_step])
        await db.flush()
        requested_query.default_step_id = str(requested_step.id)
        sibling_query.default_step_id = str(sibling_step.id)

        requested_visualization = Visualization(
            title="Requested visualization",
            status="success",
            report_id=str(report.id),
            query_id=str(requested_query.id),
            view={"type": "table"},
        )
        sibling_visualization = Visualization(
            title="Unrelated visualization",
            status="success",
            report_id=str(report.id),
            query_id=str(sibling_query.id),
            view={"type": "table"},
        )
        db.add_all([requested_visualization, sibling_visualization])

        dashboard = Artifact(
            report_id=str(report.id),
            user_id=str(user.id),
            organization_id=str(organization.id),
            title="Requested dashboard",
            mode="page",
            version=3,
            status="completed",
            content={"code": "function App() { return null }", "visualization_ids": []},
        )
        document = Artifact(
            report_id=str(report.id),
            user_id=str(user.id),
            organization_id=str(organization.id),
            title="Requested document",
            mode="doc",
            version=1,
            status="completed",
            content={"markdown": "# Exact document text", "visualization_ids": []},
        )
        db.add_all([dashboard, document])
        db.add(
            Completion(
                report_id=str(report.id),
                user_id=str(user.id),
                role="user",
                status="success",
                prompt={"content": "Unrelated conversation history"},
                completion={},
            )
        )
        for index in range(4):
            db.add(
                Artifact(
                    report_id=str(report.id),
                    user_id=str(user.id),
                    organization_id=str(organization.id),
                    title=f"Unrelated artifact {index}",
                    mode="page",
                    version=index + 1,
                    status="completed",
                    content={"code": "x" * 2_000, "visualization_ids": []},
                )
            )
        await db.commit()

        ids = {
            "report": str(report.id),
            "organization": str(organization.id),
            "dashboard": str(dashboard.id),
            "document": str(document.id),
            "query": str(requested_query.id),
            "query_step": str(requested_step.id),
            "query_old_step": str(requested_old_step.id),
            "visualization": str(requested_visualization.id),
        }
        db.expunge_all()
        yield db, ids

    await engine.dispose()


async def _record_sql(db, operation):
    statements: list[str] = []

    def _capture(_conn, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement.lower())

    sync_engine = db.bind.sync_engine
    event.listen(sync_engine, "before_cursor_execute", _capture)
    try:
        result = await operation()
    finally:
        event.remove(sync_engine, "before_cursor_execute", _capture)
    return result, statements


async def _record_sql_and_graph_loads(db, operation):
    """Record SQL plus ORM identities materialized while exercising a tool."""
    loaded: dict[str, set[str]] = {}

    def _capture_load(_session, instance):
        if isinstance(instance, (Report, Artifact, Completion, Query, Step, Visualization, Widget)):
            loaded.setdefault(type(instance).__name__, set()).add(str(instance.id))

    event.listen(db.sync_session, "loaded_as_persistent", _capture_load)
    try:
        result, statements = await _record_sql(db, operation)
    finally:
        event.remove(db.sync_session, "loaded_as_persistent", _capture_load)
    return result, statements, loaded


def _assert_report_graph_not_loaded(statements: list[str]) -> None:
    assert not any("from reports" in statement for statement in statements), (
        "loading one artifact hydrated its owning report and the report's collections"
    )


@pytest.mark.asyncio
async def test_read_artifact_does_not_hydrate_owning_report(artifact_context):
    db, ids = artifact_context

    async def _read():
        events = []
        runtime_ctx = {
            "db": db,
            "report": SimpleNamespace(id=ids["report"]),
            "organization": SimpleNamespace(id=ids["organization"]),
        }
        async for tool_event in ReadArtifactTool().run_stream({"artifact_id": ids["dashboard"]}, runtime_ctx):
            events.append(tool_event)
        return events

    events, statements = await _record_sql(db, _read)

    assert events[-1].payload["output"]["code"] == "function App() { return null }"
    _assert_report_graph_not_loaded(statements)


@pytest.mark.asyncio
async def test_edit_artifact_guard_does_not_hydrate_owning_report(artifact_context):
    db, ids = artifact_context

    async def _edit_doc_guard():
        events = []
        runtime_ctx = {
            "db": db,
            "report": SimpleNamespace(id=ids["report"]),
            "organization": SimpleNamespace(id=ids["organization"]),
        }
        async for tool_event in EditArtifactTool().run_stream(
            {"artifact_id": ids["document"], "edit_prompt": "Make the heading blue"},
            runtime_ctx,
        ):
            events.append(tool_event)
        return events

    events, statements = await _record_sql(db, _edit_doc_guard)

    assert events[-1].payload["output"]["success"] is False
    _assert_report_graph_not_loaded(statements)


@pytest.mark.asyncio
async def test_active_artifact_lookup_does_not_hydrate_report_graph(artifact_context):
    db, ids = artifact_context
    agent = object.__new__(AgentV2)
    agent.db = db
    agent.report = SimpleNamespace(id=ids["report"])

    active, statements = await _record_sql(db, agent._get_active_artifact)

    assert active is not None
    assert active["mode"] == "page"
    _assert_report_graph_not_loaded(statements)


@pytest.mark.asyncio
@pytest.mark.parametrize("lookup_kind", ["query", "visualization"])
async def test_read_query_loads_only_requested_query_graph(artifact_context, lookup_kind):
    db, ids = artifact_context

    async def _read():
        events = []
        tool_input = (
            {"query_ids": [ids["query"]]} if lookup_kind == "query" else {"visualization_ids": [ids["visualization"]]}
        )
        runtime_ctx = {
            "db": db,
            "report": SimpleNamespace(id=ids["report"]),
            "organization": SimpleNamespace(id=ids["organization"]),
        }
        async for tool_event in ReadQueryTool().run_stream(tool_input, runtime_ctx):
            events.append(tool_event)
        return events

    events, statements, loaded = await _record_sql_and_graph_loads(db, _read)

    output = events[-1].payload["output"]
    assert output["success"] is True
    assert len(output["results"]) == 1
    result = output["results"][0]
    assert result["query_id"] == ids["query"]
    assert result["visualization_id"] == ids["visualization"]
    assert result["step_id"] == ids["query_step"]
    assert result["data"]["rows"] == [["requested"]]

    assert loaded.get("Query", set()) == {ids["query"]}
    assert loaded.get("Step", set()) == {ids["query_old_step"], ids["query_step"]}
    assert loaded.get("Visualization", set()) == {ids["visualization"]}
    assert loaded.get("Report", set()) == set()
    assert loaded.get("Artifact", set()) == set()
    assert loaded.get("Completion", set()) == set()
    assert loaded.get("Widget", set()) == set()
    for unrelated_table in ("reports", "artifacts", "completions"):
        assert not any(f"from {unrelated_table}" in statement for statement in statements)
