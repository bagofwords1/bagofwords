"""A chart's spec (its series) must reach the stored step.

Chat channels (WhatsApp, Slack, Teams, Google Chat) build the chart image
from the STORED step's data_model, so a spec that only lives in memory means
no image. Drives the agent's real progress handler and end-of-tool handler
against the database, in both write modes: one shared session for the whole
run (single-writer: always on SQLite) and a fresh session per write.
"""
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.orm import lazyload

import main  # noqa: F401  (registers every mapper)
from app.ai.agent_v2 import AgentV2
from app.dependencies import async_session_maker
from app.models.step import Step
from app.project_manager import ProjectManager

pytestmark = pytest.mark.db

COLUMNS = [
    {"generated_column_name": "artist", "column_name": "artist"},
    {"generated_column_name": "total_sales", "column_name": "total_sales"},
]


def _agent(shared_session):
    agent = object.__new__(AgentV2)
    agent.project_manager = ProjectManager()
    agent.current_execution = None
    agent.report = None
    agent.system_completion = None
    agent.head_completion = None
    agent.platform = None

    @asynccontextmanager
    async def writes_session():
        if shared_session is not None:
            yield shared_session
        else:
            async with async_session_maker() as db:
                yield db

    agent._writes_session = writes_session
    agent._use_single_write_session = lambda: shared_session is not None

    async def _noop(*a, **kw):
        return None

    agent._emit_sse_event = _noop
    agent._schedule_bg_write = lambda *a, **kw: None
    agent._record_step_table_usage = _noop
    return agent


async def _new_step(db, chart_type):
    # As the agent creates it when the chart type is known.
    step = Step(id=str(uuid.uuid4()), title="t", slug=uuid.uuid4().hex, status="draft",
                prompt="", code="", data_model={"type": chart_type, "columns": [], "series": []})
    db.add(step)
    await db.commit()
    return step


async def _stored_data_model(step_id):
    async with async_session_maker() as db:
        step = (await db.execute(
            select(Step).options(lazyload("*")).where(Step.id == step_id)
        )).scalar_one()
        return step.data_model


async def _run_tool(agent, inv, series):
    # create_data's own progress: the series once inference picks it (it
    # emits no column_added — only the inactive create_widget does).
    progress = lambda stage, **p: {"type": "tool.progress", "payload": {"stage": stage, **p}}
    await agent._handle_streaming_event("create_data", progress("series_configured", series=series), {}, inv=inv)
    await agent._handle_tool_output(
        "create_data", {},
        {"success": True, "summary": "ok"},
        tool_output={
            "success": True, "code": "df = ...",
            "data": {"rows": [{"artist": "A", "total_sales": 1.0}], "columns": COLUMNS},
            "data_model": {"type": inv.chart_type, "series": series},
        },
        inv=inv,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("single_writer", [True, False], ids=["shared-session", "session-per-write"])
@pytest.mark.parametrize("chart_type,series", [
    ("bar_chart", [{"name": "Total Sales", "key": "artist", "value": "total_sales"}]),
    ("line_chart", [{"name": "Sales", "key": "month", "value": "sales"},
                    {"name": "Refunds", "key": "month", "value": "refunds"}]),
    ("pie_chart", [{"name": "Share", "key": "genre", "value": "share"}]),
])
async def test_chart_spec_reaches_the_stored_step(single_writer, chart_type, series):
    async with async_session_maker() as shared:
        session = shared if single_writer else None
        db = shared if single_writer else None
        if db is None:
            async with async_session_maker() as setup_db:
                step = await _new_step(setup_db, chart_type)
        else:
            step = await _new_step(db, chart_type)
        inv = SimpleNamespace(current_step_id=str(step.id), current_visualization=None,
                              current_query=None, current_widget=None, chart_type=chart_type)

        await _run_tool(_agent(session), inv, series)

    stored = await _stored_data_model(step.id)
    assert stored["type"] == chart_type
    assert stored["series"] == series


@pytest.mark.asyncio
@pytest.mark.parametrize("single_writer", [True, False], ids=["shared-session", "session-per-write"])
async def test_series_is_stored_as_soon_as_it_is_configured(single_writer):
    """A reader that looks at the step mid-run (another request, a chat
    sender reacting to a step update) sees the configured series."""
    series = [{"name": "Total Sales", "key": "artist", "value": "total_sales"}]
    async with async_session_maker() as shared:
        session = shared if single_writer else None
        if single_writer:
            step = await _new_step(shared, "bar_chart")
        else:
            async with async_session_maker() as setup_db:
                step = await _new_step(setup_db, "bar_chart")
        inv = SimpleNamespace(current_step_id=str(step.id), current_visualization=None,
                              current_query=None, current_widget=None)
        agent = _agent(session)
        await agent._handle_streaming_event(
            "create_data", {"type": "tool.progress", "payload": {"stage": "series_configured", "series": series}},
            {}, inv=inv,
        )

    assert (await _stored_data_model(step.id))["series"] == series
