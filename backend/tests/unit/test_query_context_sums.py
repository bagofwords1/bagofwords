"""The ``<queries>`` section shows a ``column_sums`` block per query.

The section is rebuilt every iteration from each default step's persisted
summary: the stored ``info`` (all rows) plus at most 5 ``preview_rows``. For a
longer result those rows are incomplete, so a total row the section cannot see
is caught by the ``k × max`` check only. Hidden previews get no block.

Seam: ``QueryContextBuilder.build`` on the migration-backed test database; the
step's summary is written by the existing Step write-through.
"""
from __future__ import annotations

import logging
import uuid
from types import SimpleNamespace

import pandas as pd
import pytest
import pytest_asyncio
from sqlalchemy import select

from app.ai import data_preview
from app.ai.code_execution.code_execution import StreamingCodeExecutor
from app.ai.context.builders.query_context_builder import QueryContextBuilder
from app.ai.data_preview import clamp_stats, gate_stats_for_privacy
from app.dependencies import async_session_maker
from app.models.organization import Organization
from app.models.query import Query
from app.models.report import Report
from app.models.step import Step
from app.models.user import User
from app.models.widget import Widget
from tests.unit.test_observation_sums import EXPECTED_SUMS, _departments_df

pytestmark = pytest.mark.db  # opens real sessions — see tests/unit/conftest.py

NOTE_12 = "sum = sum of all 12 rows"


@pytest_asyncio.fixture
async def db():
    """Use the migration-backed test database selected by ``--db``."""
    async with async_session_maker() as session:
        yield session
        await session.rollback()


async def _seed_step(db, df) -> SimpleNamespace:
    user = User(
        name="Sums owner",
        email=f"sums-{uuid.uuid4()}@example.test",
        hashed_password="not-used",
    )
    organization = Organization(name=f"Sums org {uuid.uuid4()}")
    db.add_all([user, organization])
    await db.flush()
    report = Report(
        title="Budget report",
        slug=f"sums-report-{uuid.uuid4()}",
        user_id=str(user.id),
        organization_id=str(organization.id),
    )
    db.add(report)
    await db.flush()
    widget = Widget(title="Budget", slug=f"sums-widget-{uuid.uuid4()}", report_id=str(report.id))
    db.add(widget)
    await db.flush()
    query = Query(
        title="Budget by department",
        report_id=str(report.id),
        widget_id=str(widget.id),
        organization_id=str(organization.id),
        user_id=str(user.id),
    )
    db.add(query)
    await db.flush()
    step = Step(
        title="Budget by department",
        slug=f"sums-step-{uuid.uuid4()}",
        status="success",
        prompt="select department, budget_eur, actual_eur",
        code="select department, budget_eur, actual_eur",
        data=StreamingCodeExecutor().format_df_for_widget(df),
        type="table",
        data_model={"type": "table"},
        widget_id=str(widget.id),
        query_id=str(query.id),
    )
    db.add(step)
    await db.flush()
    query.default_step_id = str(step.id)
    await db.commit()
    return SimpleNamespace(report_id=str(report.id), step_id=str(step.id))


async def _section(db, scenario, *, include_data_preview=True):
    return await QueryContextBuilder(
        db,
        organization=None,
        report=SimpleNamespace(id=scenario.report_id),
    ).build(max_queries=5, include_data_preview=include_data_preview)


def _with_unlabelled_total_row(df):
    """A 13th row with a NULL label holding each column's total (UNION ALL)."""
    total = {
        "department": None,
        "budget_eur": int(df["budget_eur"].sum()),
        "actual_eur": int(df["actual_eur"].sum()),
        "variance_pct": round(float(df["variance_pct"].sum()), 2),
        "dept_id": 99,
    }
    return pd.concat([df, pd.DataFrame([total])], ignore_index=True)


@pytest.mark.asyncio
async def test_queries_section_shows_sums_when_previews_visible(db):
    scenario = await _seed_step(db, _departments_df())
    persisted = (
        await db.execute(select(Step.context_summary_json).where(Step.id == scenario.step_id))
    ).scalar_one()
    assert len(persisted["preview_rows"]) == 5  # 5 of 12 rows: incomplete

    section = await _section(db, scenario)

    [item] = section.items
    assert item.row_count == 12
    assert item.column_sums == {"note": NOTE_12, "sum": EXPECTED_SUMS}
    rendered = section.render()
    assert "<column_sums>" in rendered
    assert NOTE_12 in rendered
    assert "&quot;budget_eur&quot;: 2140" in rendered  # xml_escape, as every tag


@pytest.mark.asyncio
async def test_queries_section_drops_sums_for_a_total_row_it_cannot_see(db):
    df = _with_unlabelled_total_row(_departments_df())
    assert df["budget_eur"].sum() == 2 * df["budget_eur"].max()  # the k = 2 signature
    scenario = await _seed_step(db, df)

    section = await _section(db, scenario)

    [item] = section.items
    assert item.row_count == 13
    assert item.column_sums is None
    assert "<column_sums>" not in section.render()
    # Control: test_queries_section_shows_sums_when_previews_visible (same
    # step without the total row) gets the block.


@pytest.mark.asyncio
async def test_queries_section_has_no_sums_when_previews_hidden(db):
    scenario = await _seed_step(db, _departments_df())
    persisted = (
        await db.execute(select(Step.context_summary_json).where(Step.id == scenario.step_id))
    ).scalar_one()
    info = dict(persisted["info"])

    hidden = await _section(db, scenario, include_data_preview=False)

    [item] = hidden.items
    assert item.column_sums is None
    assert item.data_preview is None
    assert "<column_sums>" not in hidden.render()
    # The privacy stats are rendered exactly as before this change.
    assert item.stats == clamp_stats(gate_stats_for_privacy(info))

    # Control: the same step with previews visible shows the block.
    visible = await _section(db, scenario)
    assert visible.items[0].column_sums == {"note": NOTE_12, "sum": EXPECTED_SUMS}
    assert "<column_sums>" in visible.render()


@pytest.mark.asyncio
async def test_single_row_query_has_no_sums(db):
    scenario = await _seed_step(db, _departments_df().head(1))

    section = await _section(db, scenario)

    [item] = section.items
    assert item.row_count == 1
    assert item.column_sums is None
    assert "<column_sums>" not in section.render()
    # Control: the 12-row step of test_queries_section_shows_sums_when_previews_visible.


@pytest.mark.asyncio
async def test_queries_section_survives_a_column_sums_failure(db, monkeypatch, caplog):
    scenario = await _seed_step(db, _departments_df())
    monkeypatch.setattr(logging.getLogger("app.ai.data_preview"), "disabled", False)
    caplog.set_level(logging.INFO, logger="app.ai.data_preview")

    def boom(*args, **kwargs):
        raise ValueError("budget 2140")

    monkeypatch.setattr(data_preview, "column_sums", boom)

    section = await _section(db, scenario)

    [item] = section.items
    assert item.row_count == 12
    assert item.data_preview is not None  # set before the sums call; kept
    assert item.column_sums is None
    assert "<column_sums>" not in section.render()
    records = [r for r in caplog.records if r.name == "app.ai.data_preview"]
    assert [(r.levelno, r.getMessage()) for r in records] == [
        (logging.WARNING, f"column sums skipped: step={scenario.step_id} error=ValueError"),
    ]
    # Control: test_queries_section_shows_sums_when_previews_visible (same
    # step, real column_sums) gets the block.
