"""Completion stream bootstrap must not hydrate the report's result graph."""

# Mapper registration intentionally runs before the app-model imports below.
# ruff: noqa: E402

from __future__ import annotations

import asyncio
import re
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# Register the mapped model graph before creating the in-memory schema. This is
# the same import set Alembic uses and preserves mapper-level loader behavior.
_env_src = (Path(__file__).resolve().parents[2] / "alembic" / "env.py").read_text()
for _stmt in re.findall(r"^from app\.models\S* import \([^)]*\)|^from app\.models[^\n]+", _env_src, re.M):
    exec(_stmt)  # noqa: S102 -- test-only, mirrors alembic/env.py

from app.ai.context.builders.files_context_builder import FilesContextBuilder
from app.models.artifact import Artifact
from app.models.base import Base
from app.models.completion import Completion
from app.models.file import File
from app.models.file_tag import FileTag
from app.models.organization import Organization
from app.models.query import Query
from app.models.report import Report
from app.models.report_file_association import report_file_association
from app.models.sheet_schema import SheetSchema
from app.models.user import User
from app.models.widget import Widget
from app.schemas.completion_v2_schema import CompletionCreate
from app.services.completion_service import CompletionService


@pytest_asyncio.fixture
async def report_with_unrelated_graph():
    Completion.__table__.c.sigkill.nullable = True
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with maker() as db:
        suffix = uuid.uuid4().hex
        owner = User(
            name="Completion owner",
            email=f"completion-{suffix}@example.test",
            hashed_password="x",
        )
        organization = Organization(name=f"Completion org {suffix}")
        db.add_all([owner, organization])
        await db.flush()

        report = Report(
            title="Report with historical output",
            slug=f"bounded-bootstrap-{suffix}",
            user_id=str(owner.id),
            organization_id=str(organization.id),
        )
        db.add(report)
        await db.flush()

        # This exact graph is not exposed by a setup API. It represents data
        # left by an earlier turn and must be irrelevant to starting a new one.
        widget = Widget(
            title="Historical widget",
            slug=f"historical-widget-{suffix}",
            report_id=str(report.id),
        )
        db.add(widget)
        await db.flush()
        db.add(Query(
            title="Historical query",
            report_id=str(report.id),
            widget_id=str(widget.id),
            organization_id=str(organization.id),
            user_id=str(owner.id),
        ))
        db.add(Artifact(
            report_id=str(report.id),
            user_id=str(owner.id),
            organization_id=str(organization.id),
            title="Historical dashboard",
            mode="page",
            version=1,
            status="completed",
            content={"code": "export default function Dashboard() {}"},
        ))

        # Legacy files have no preview JSON; their LLM descriptions still come
        # from file_tags / sheet_schemas. There is no setup API for this old
        # persisted shape, so seed the model rows and report association here.
        tagged_pdf = File(
            filename="legacy-policy.pdf",
            path="/tmp/legacy-policy.pdf",
            content_type="application/pdf",
            preview=None,
            user_id=str(owner.id),
            organization_id=str(organization.id),
        )
        legacy_sheet = File(
            filename="legacy-revenue.xlsx",
            path="/tmp/legacy-revenue.xlsx",
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            preview=None,
            user_id=str(owner.id),
            organization_id=str(organization.id),
        )
        db.add_all([tagged_pdf, legacy_sheet])
        await db.flush()
        db.add_all([
            FileTag(key="department", value="finance", file_id=str(tagged_pdf.id)),
            SheetSchema(
                sheet_name="Revenue",
                sheet_index=0,
                schema={"columns": ["month", "amount"]},
                file_id=str(legacy_sheet.id),
            ),
        ])
        await db.execute(report_file_association.insert(), [
            {"report_id": str(report.id), "file_id": str(tagged_pdf.id)},
            {"report_id": str(report.id), "file_id": str(legacy_sheet.id)},
        ])
        await db.commit()

        yield db, maker, report, owner, organization

    await engine.dispose()


@pytest.mark.asyncio
async def test_stream_bootstrap_does_not_hydrate_unrelated_report_graph(
    report_with_unrelated_graph,
):
    db, _maker, report, owner, organization = report_with_unrelated_graph
    statements: list[str] = []

    def _capture(_conn, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement.lower())

    event.listen(db.bind.sync_engine, "before_cursor_execute", _capture)
    try:
        # No LLM model is seeded intentionally. Model resolution is the public
        # service's read-only stopping boundary: it lets us inspect bootstrap
        # SQL without creating completions or starting a background agent.
        with pytest.raises(HTTPException) as exc_info:
            await CompletionService().create_completion_stream(
                db,
                str(report.id),
                CompletionCreate(prompt={"content": "Start a new turn"}),
                owner,
                organization,
            )
    finally:
        event.remove(db.bind.sync_engine, "before_cursor_execute", _capture)

    assert exc_info.value.status_code == 400
    unrelated_tables = ("artifacts", "completions", "queries", "steps", "visualizations")
    loaded = {
        table
        for table in unrelated_tables
        if any(re.search(rf"\bfrom\s+(?:\w+\.)?[\"`]?{table}[\"`]?\b", sql) for sql in statements)
    }
    assert not loaded, (
        "starting a completion hydrated unrelated historical report data: "
        f"{sorted(loaded)}"
    )


@pytest.mark.asyncio
async def test_background_refetch_preserves_legacy_file_descriptions_without_report_graph(
    report_with_unrelated_graph,
    monkeypatch,
):
    db, maker, report, owner, organization = report_with_unrelated_graph
    captured: dict[str, str] = {}
    statements: list[str] = []

    class InspectingAgent:
        """Stop at the agent boundary after exercising its real file builder."""

        def __init__(self, *, db, organization, report, head_completion, **_kwargs):
            self.db = db
            self.organization = organization
            self.report = report
            self.head_completion = head_completion

        async def main_execution(self):
            section = await FilesContextBuilder(
                self.db,
                self.organization,
                self.report,
                self.head_completion,
            ).build()
            captured["files"] = section.render()

    async def _resolved_models(*_args, **_kwargs):
        model = SimpleNamespace(id="model-id", model_id="test-model")
        return model, model, {}

    async def _no_next_prompt(*_args, **_kwargs):
        return None

    # External/model execution is outside this bootstrap regression. Keep the
    # public streaming service path, replacing only the LLM-agent boundary.
    service = CompletionService()
    monkeypatch.setattr(service, "_resolve_completion_models", _resolved_models)
    monkeypatch.setattr(service, "start_next_queued_if_idle", _no_next_prompt)
    monkeypatch.setattr(
        "app.services.completion_service.create_async_session_factory",
        lambda: maker,
    )
    monkeypatch.setattr("app.services.completion_service.AgentV2", InspectingAgent)

    def _capture(_conn, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement.lower())

    event.listen(db.bind.sync_engine, "before_cursor_execute", _capture)
    try:
        response = await service.create_completion_stream(
            db,
            str(report.id),
            CompletionCreate(prompt={"content": "Use the legacy files"}),
            owner,
            organization,
        )

        async def _consume_stream():
            return [chunk async for chunk in response.body_iterator]

        chunks = await asyncio.wait_for(_consume_stream(), timeout=5)
    finally:
        event.remove(db.bind.sync_engine, "before_cursor_execute", _capture)

    assert chunks
    rendered = captured["files"]
    assert "department: finance" in rendered
    assert "Sheet Name: Revenue" in rendered
    assert "month" in rendered and "amount" in rendered

    unrelated_tables = ("artifacts", "queries", "steps", "visualizations")
    loaded = {
        table
        for table in unrelated_tables
        if any(re.search(rf"\bfrom\s+(?:\w+\.)?[\"`]?{table}[\"`]?\b", sql) for sql in statements)
    }
    assert not loaded, (
        "the bounded agent refetch hydrated unrelated report data: "
        f"{sorted(loaded)}"
    )
