"""Query context reads the bounded Step projection, never the full snapshot."""

from __future__ import annotations

import json
import re
import uuid
from datetime import timedelta
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import event, null, select, update

from app.ai.context.builders.query_context_builder import QueryContextBuilder
from app.ai.context.summary_write_through import persist_step_context_projections
from app.dependencies import async_session_maker
from app.models.organization import Organization
from app.models.query import Query
from app.models.report import Report
from app.models.step import Step
from app.models.user import User
from app.models.widget import Widget

_MARKER = "later-row-must-not-enter-step-context-"


@pytest_asyncio.fixture
async def db():
    """Use the migration-backed test database selected by ``--db``."""
    async with async_session_maker() as session:
        yield session
        await session.rollback()


async def _seed_summarized_query(db):
    user = User(
        name="Step summary owner",
        email=f"step-summary-{uuid.uuid4()}@example.test",
        hashed_password="not-used",
    )
    organization = Organization(name=f"Step summary org {uuid.uuid4()}")
    db.add_all([user, organization])
    await db.flush()

    report = Report(
        title="Step summary report",
        slug=f"step-summary-report-{uuid.uuid4()}",
        user_id=str(user.id),
        organization_id=str(organization.id),
    )
    db.add(report)
    await db.flush()
    widget = Widget(
        title="Regional result",
        slug=f"step-summary-widget-{uuid.uuid4()}",
        report_id=str(report.id),
    )
    db.add(widget)
    await db.flush()
    query = Query(
        title="Regional revenue",
        report_id=str(report.id),
        widget_id=str(widget.id),
        organization_id=str(organization.id),
        user_id=str(user.id),
    )
    db.add(query)
    await db.flush()

    rows = [{"region": f"Region {index}", "net_revenue": index * 10} for index in range(5)]
    rows.append({"region": _MARKER * 20_000, "net_revenue": 999})
    step = Step(
        title="Regional revenue result",
        slug=f"step-summary-step-{uuid.uuid4()}",
        status="success",
        prompt="select region, net_revenue",
        code="select region, net_revenue",
        data={
            "columns": [{"field": "region"}, {"field": "net_revenue"}],
            "rows": rows,
            "info": {
                "total_rows": 101_039,
                "total_columns": 2,
                "column_info": {
                    "net_revenue": {"dtype": "int64", "mean": 20, "count": 5},
                },
            },
        },
        description="Large stored result",
        type="table",
        data_model={"type": "table"},
        view={"theme": "default"},
        widget_id=str(widget.id),
        query_id=str(query.id),
    )
    db.add(step)
    await db.flush()
    query.default_step_id = str(step.id)
    await db.commit()
    return SimpleNamespace(
        report_id=str(report.id),
        step_id=str(step.id),
        query_id=str(query.id),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("include_data_preview", [True, False])
async def test_query_context_uses_step_summary_without_step_data(
    db,
    include_data_preview,
):
    scenario = await _seed_summarized_query(db)
    persisted_summary = (
        await db.execute(select(Step.context_summary_json).where(Step.id == scenario.step_id))
    ).scalar_one()
    assert persisted_summary["row_count"] == 101_039
    assert persisted_summary["columns"] == [
        {"field": "region"},
        {"field": "net_revenue"},
    ]
    assert len(persisted_summary["preview_rows"]) == 5
    assert _MARKER not in json.dumps(persisted_summary)

    statements: list[str] = []

    def _record_sql(_conn, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement.lower())

    event.listen(db.bind.sync_engine, "before_cursor_execute", _record_sql)
    try:
        section = await QueryContextBuilder(
            db,
            organization=None,
            report=SimpleNamespace(id=scenario.report_id),
        ).build(max_queries=5, include_data_preview=include_data_preview)
    finally:
        event.remove(db.bind.sync_engine, "before_cursor_execute", _record_sql)

    assert len(section.items) == 1
    observation = section.items[0]
    assert observation.query_id == scenario.query_id
    assert observation.row_count == 101_039
    assert observation.column_names == ["region", "net_revenue"]
    assert bool(observation.data_preview) is include_data_preview
    assert _MARKER not in section.render()

    normalized = [re.sub(r"[\"`]", "", statement) for statement in statements]
    assert any("steps.context_summary_json" in statement for statement in normalized)
    assert not any(re.search(r"\b(?:steps|s)\.data\b", statement) for statement in normalized), (
        "summary-backed query context selected the full Step.data payload"
    )


@pytest.mark.asyncio
async def test_legacy_step_projection_is_persisted_after_first_read(db):
    """A legacy Step becomes summary-backed without rerunning its query."""
    scenario = await _seed_summarized_query(db)
    await db.execute(update(Step).where(Step.id == scenario.step_id).values(context_summary_json=None))
    await db.commit()

    first_section = await QueryContextBuilder(
        db,
        organization=None,
        report=SimpleNamespace(id=scenario.report_id),
    ).build(max_queries=5, include_data_preview=True)

    async with async_session_maker() as verification_db:
        persisted = (
            await verification_db.execute(select(Step.context_summary_json).where(Step.id == scenario.step_id))
        ).scalar_one()
    assert isinstance(persisted, dict)

    statements: list[str] = []
    async with async_session_maker() as second_db:

        def _record_sql(_conn, _cursor, statement, _parameters, _context, _executemany):
            statements.append(statement.lower())

        event.listen(second_db.bind.sync_engine, "before_cursor_execute", _record_sql)
        try:
            second_section = await QueryContextBuilder(
                second_db,
                organization=None,
                report=SimpleNamespace(id=scenario.report_id),
            ).build(max_queries=5, include_data_preview=True)
        finally:
            event.remove(second_db.bind.sync_engine, "before_cursor_execute", _record_sql)

    assert second_section.render() == first_section.render()
    normalized = [re.sub(r"[\"`]", "", statement) for statement in statements]
    assert not any(re.search(r"\b(?:steps|s)\.data\b", statement) for statement in normalized)


@pytest.mark.asyncio
@pytest.mark.parametrize("race", ["rerun", "retention"])
async def test_stale_step_projection_is_not_written_after_source_changes(db, race):
    """Write-through cannot resurrect purged data or overwrite a newer run."""
    scenario = await _seed_summarized_query(db)
    source = (
        await db.execute(
            select(
                Step.id,
                Step.updated_at,
                Step.data,
            ).where(Step.id == scenario.step_id)
        )
    ).one()
    data = source.data
    projection = {
        "id": str(source.id),
        "updated_at": source.updated_at,
        "info": data["info"],
        "columns": data["columns"],
        "preview_rows": data["rows"][:5],
    }

    if race == "rerun":
        await db.execute(
            update(Step)
            .where(Step.id == scenario.step_id)
            .values(
                data={"columns": [{"field": "new"}], "rows": [{"new": 1}]},
                context_summary_json=None,
                updated_at=source.updated_at + timedelta(seconds=1),
            )
        )
    else:
        await db.execute(
            update(Step)
            .where(Step.id == scenario.step_id)
            .values(
                # The production retention job writes SQL NULL directly.
                data=null(),
                context_summary_json=None,
                updated_at=source.updated_at,
            )
        )
    await db.commit()

    await persist_step_context_projections(db, [projection])

    async with async_session_maker() as verification_db:
        persisted = (
            await verification_db.execute(select(Step.context_summary_json).where(Step.id == scenario.step_id))
        ).scalar_one()
    assert persisted is None
