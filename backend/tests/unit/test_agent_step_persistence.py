"""Agent-path step/visualization persistence: one commit per write-set, and
the completion event bus still carries what Stop and steering need.
"""

# Mapper registration intentionally runs before the app-model imports below.
# ruff: noqa: E402

from __future__ import annotations

import json
import re
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

_env_src = (Path(__file__).resolve().parents[2] / "alembic" / "env.py").read_text()
for _stmt in re.findall(r"^from app\.models\S* import \([^)]*\)|^from app\.models[^\n]+", _env_src, re.M):
    exec(_stmt)  # noqa: S102 — test-only, mirrors alembic/env.py

from app.models.base import Base
from app.models.completion import Completion, _bus_event
from app.models.organization import Organization
from app.models.query import Query
from app.models.report import Report
from app.models.step import Step
from app.models.user import User
from app.models.visualization import Visualization
from app.models.widget import Widget
from app.project_manager import ProjectManager


@pytest_asyncio.fixture
async def ctx(tmp_path):
    Completion.__table__.c.sigkill.nullable = True
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'persist.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    commits = {"n": 0}

    @event.listens_for(engine.sync_engine, "commit")
    def _count(_conn):
        commits["n"] += 1

    # Direct model writes: an agent tool mid-run is not reachable through the
    # HTTP API without a live LLM.
    async with maker() as db:
        user = User(name="Persist", email=f"persist-{uuid.uuid4()}@example.com", hashed_password="x")
        org = Organization(name=f"Persist org {uuid.uuid4()}")
        db.add_all([user, org])
        await db.flush()
        report = Report(title="r", slug=f"r-{uuid.uuid4()}", user_id=str(user.id), organization_id=str(org.id))
        db.add(report)
        await db.commit()
        ids = SimpleNamespace(report=str(report.id), org=str(org.id), user=str(user.id))
    yield SimpleNamespace(maker=maker, ids=ids, commits=commits)
    await engine.dispose()


async def _report(db, report_id):
    from sqlalchemy.orm import lazyload
    return (await db.execute(select(Report).options(lazyload("*")).where(Report.id == report_id))).scalar_one()


@pytest.mark.asyncio
@pytest.mark.parametrize("viz_type", ["table", "line_chart"])
async def test_create_query_step_visualization_is_one_commit(ctx, viz_type):
    pm = ProjectManager()
    async with ctx.maker() as db:
        report = await _report(db, ctx.ids.report)
        before = ctx.commits["n"]
        query, step, viz = await pm.create_query_step_visualization(
            db, report, "Sales by year",
            initial_data_model={"type": viz_type, "columns": [], "series": []},
            viz_view={"type": viz_type},
        )
        assert ctx.commits["n"] - before == 1
        assert query.default_step_id == str(step.id)

    async with ctx.maker() as fresh:
        q = await fresh.get(Query, str(query.id))
        s = await fresh.get(Step, str(step.id))
        v = await fresh.get(Visualization, str(viz.id))
        w = await fresh.get(Widget, str(q.widget_id))
    assert q.report_id == ctx.ids.report and q.default_step_id == s.id
    assert s.query_id == q.id and s.widget_id == w.id and s.status == "draft"
    assert s.data_model["type"] == viz_type
    assert v.query_id == q.id and v.status == "draft" and v.view.get("type") == viz_type


@pytest.mark.asyncio
@pytest.mark.parametrize("rows", [1, 250])
async def test_finalize_tool_step_persists_everything_in_one_commit(ctx, rows):
    pm = ProjectManager()
    async with ctx.maker() as db:
        report = await _report(db, ctx.ids.report)
        query, step, viz = await pm.create_query_step_visualization(db, report, "t", initial_data_model={"type": "table"})
        data = {"columns": [{"field": "a"}], "rows": [{"a": i} for i in range(rows)]}
        params = [{"name": "year", "type": "number", "source": "input"}]
        before = ctx.commits["n"]
        await pm.finalize_tool_step(
            db, step, code="select 1", data=data, data_model={"type": "table", "series": []},
            parameters=params, applied_params={"year": 2024}, status="success",
        )
        assert ctx.commits["n"] - before == 1
        await pm.finalize_visualization(db, viz, {"type": "table"}, "success")
        assert ctx.commits["n"] - before == 2

    async with ctx.maker() as fresh:
        s = await fresh.get(Step, str(step.id))
        q = await fresh.get(Query, str(query.id))
        v = await fresh.get(Visualization, str(viz.id))
    assert s.status == "success" and s.code == "select 1"
    assert len(s.data["rows"]) == rows
    assert s.data_model["type"] == "table"
    assert s.applied_params == {"year": 2024}
    assert [p["name"] for p in q.parameters] == ["year"]
    assert v.status == "success" and v.view.get("type") == "table"


@pytest.mark.asyncio
async def test_finalize_tool_step_keeps_data_when_parameters_fail(ctx, monkeypatch):
    pm = ProjectManager()
    async with ctx.maker() as db:
        report = await _report(db, ctx.ids.report)
        _, step, _ = await pm.create_query_step_visualization(db, report, "t", initial_data_model={"type": "table"})

        async def _boom(*a, **k):
            raise RuntimeError("parameter write failed")

        monkeypatch.setattr(pm, "_stage_query_parameters", _boom)
        await pm.finalize_tool_step(
            db, step, code="c", data={"rows": [{"a": 1}]}, parameters=[{"name": "x"}], status="success",
        )

    async with ctx.maker() as fresh:
        s = await fresh.get(Step, str(step.id))
    assert s.status == "success" and s.data["rows"] == [{"a": 1}] and s.code == "c"


@pytest.mark.parametrize("message_type, has_prompt", [("steering", True), ("ai_completion", False)])
def test_completion_bus_event_carries_stop_and_steering_fields(message_type, has_prompt):
    target = SimpleNamespace(
        id=uuid.uuid4(), report_id=uuid.uuid4(), status="in_progress", role="user",
        message_type=message_type, parent_id=str(uuid.uuid4()), sigkill=None,
        prompt={"content": "also add a total"}, completion={"content": "x" * 10_000},
    )
    data = json.loads(json.dumps(_bus_event("update_completion", target)))
    for key in ("event", "completion_id", "report_id", "message_type", "parent_id", "sigkill"):
        assert key in data
    assert ("prompt" in data) is has_prompt
    assert "completion" not in data
