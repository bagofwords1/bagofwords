"""A page saved without a render check says so; names wrongly taken from React are fixed.

Two things let a page that cannot render reach the user as "done":

* the server could not start its browser, so nothing was rendered, and the
  tool reported success all the same. It still saves (a deployment without a
  browser must keep working) but now reports `render_checked: False` and
  tells the model not to claim the page works;
* the page listed runtime globals in `const { … } = React`, which makes them
  undefined. Those names are dropped before anything is saved, so this class
  of failure is fixed even where no browser is available.

The browser is the stubbed boundary here: `_take_preview_screenshot` returning
no screenshot is exactly what the tools see when Chromium cannot launch.

Run:
    cd backend
    TESTING=true uv run pytest tests/e2e/test_artifact_render_unchecked.py --db=sqlite
"""
import asyncio
import uuid
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from app.ai.tools.implementations.create_artifact import CreateArtifactTool
from app.ai.tools.implementations.edit_artifact import EditArtifactTool
from app.dependencies import async_session_maker
from app.models.artifact import ArtifactVersion
from app.models.organization import Organization
from app.models.query import Query
from app.models.report import Report
from app.models.step import Step
from app.models.user import User
from app.models.visualization import Visualization
from app.models.widget import Widget
from tests.fixtures.artifact import seed_artifact


def _run(coro):
    return asyncio.run(coro)


async def _seed_viz(report_id: str):
    """Direct rows: a step's stored result is written by an agent run."""
    suffix = uuid.uuid4().hex[:8]
    async with async_session_maker() as db:
        report = await db.get(Report, report_id)
        widget = Widget(title=f"W {suffix}", slug=f"w-{suffix}", report_id=report_id)
        db.add(widget)
        await db.flush()
        query = Query(title="Q", report_id=report_id, widget_id=widget.id,
                      organization_id=report.organization_id, user_id=report.user_id)
        db.add(query)
        await db.flush()
        step = Step(title="S", slug=f"s-{suffix}", status="success", widget_id=widget.id,
                    query_id=query.id, code="",
                    data={"rows": [{"genre": "Rock", "revenue": 10}],
                          "columns": [{"field": "genre"}, {"field": "revenue"}]},
                    created_at=datetime.utcnow() - timedelta(hours=1))
        db.add(step)
        await db.flush()
        query.default_step_id = step.id
        viz = Visualization(title="Revenue", status="success", report_id=report_id,
                            query_id=query.id, view={"type": "bar_chart"})
        db.add(viz)
        await db.commit()
        return str(viz.id)


async def _tool_end(tool, report_id: str, tool_input: dict):
    async with async_session_maker() as db:
        report = await db.get(Report, report_id)
        ctx = {"db": db, "report": report, "user": await db.get(User, report.user_id),
               "organization": await db.get(Organization, report.organization_id)}
        ends = [e async for e in tool.run_stream(tool_input, ctx) if e.type == "tool.end"]
    assert ends, "tool produced no tool.end event"
    return ends[-1].payload


async def _latest_code(report_id: str) -> str:
    async with async_session_maker() as db:
        rows = (await db.execute(
            select(ArtifactVersion).where(ArtifactVersion.report_id == report_id)
        )).scalars().all()
        return (max(rows, key=lambda r: r.version).content or {}).get("code", "")


async def _seed_page(report_id: str, viz_id: str, code: str):
    async with async_session_maker() as db:
        report = await db.get(Report, report_id)
        version = await seed_artifact(
            db, report_id=report_id, user_id=report.user_id, organization_id=report.organization_id,
            content={"code": code, "visualization_ids": [viz_id]},
        )
        await db.commit()
        return str(version.id)


def _page(viz_id: str, head: str = "") -> str:
    return (f'{head}function App() {{ const v = vizById("{viz_id}"); '
            "return <div><h1>Sales</h1><p>{v && v.title}</p></div> }"
            "\nReactDOM.createRoot(document.getElementById('root')).render(<App />);")


@pytest.fixture
def report(create_report, create_user, login_user, whoami, test_client):
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    return create_report(title=f"r-{uuid.uuid4().hex[:6]}", user_token=token, org_id=org_id, data_sources=[])["id"]


@pytest.fixture(params=["browser_unavailable", "rendered"])
def browser(request, monkeypatch):
    """What the server-side browser did with the page."""
    rendered = request.param == "rendered"
    shot = "c2NyZWVuc2hvdA==" if rendered else None

    async def _screenshot(self, *_args, **_kwargs):
        return shot, []

    async def _validate(self, code, artifact_data, mode, runtime_ctx, **kwargs):
        yield {"code": code, "clean": True, "screenshot": shot, "errors": [], "repair_attempts": 0}

    monkeypatch.setattr(CreateArtifactTool, "_take_preview_screenshot", _screenshot)
    monkeypatch.setattr(CreateArtifactTool, "_validate_and_repair_stream", _validate)
    return rendered


@pytest.mark.e2e
def test_create_reports_whether_the_page_was_rendered(report, browser):
    viz = _run(_seed_viz(report))

    payload = _run(_tool_end(CreateArtifactTool(), report, {
        "prompt": "build", "title": "Sales", "mode": "page",
        "code": _page(viz), "visualization_ids": [viz],
    }))

    # Either way the page is saved: a deployment without a browser keeps working.
    assert payload["output"].get("artifact_id"), payload
    assert payload["output"]["render_checked"] is browser
    # Only an unrendered page carries the flag the model is told to act on.
    assert (payload["observation"].get("render_checked") is False) is (not browser)


@pytest.mark.e2e
def test_edit_reports_whether_the_page_was_rendered(report, browser):
    viz = _run(_seed_viz(report))
    artifact_id = _run(_seed_page(report, viz, _page(viz)))

    payload = _run(_tool_end(EditArtifactTool(), report, {
        "artifact_id": artifact_id,
        "edits": [{"find": "<h1>Sales</h1>", "replace": "<h1>Revenue</h1>"}],
    }))

    assert payload["output"]["success"] is True, payload
    assert payload["output"]["render_checked"] is browser
    assert (payload["observation"].get("render_checked") is False) is (not browser)


@pytest.mark.e2e
@pytest.mark.parametrize("taken", ["KPICard", "vizById, useArtifactData", "SectionCard, DataTable, fmt"])
def test_create_drops_runtime_globals_taken_from_react(report, browser, taken):
    viz = _run(_seed_viz(report))
    names = [n.strip() for n in taken.split(",")]

    payload = _run(_tool_end(CreateArtifactTool(), report, {
        "prompt": "build", "title": "Sales", "mode": "page",
        "code": _page(viz, head=f"const {{ useState, {taken} }} = React;\n"),
        "visualization_ids": [viz],
    }))

    assert payload["output"].get("artifact_id"), payload
    saved = _run(_latest_code(report))
    assert "const { useState } = React;" in saved
    for name in names:
        assert name not in saved.split("= React;")[0]
    # The model is told what was removed, by name.
    assert all(name in " ".join(payload["observation"]["warnings"]) for name in names)


@pytest.mark.e2e
def test_edit_drops_runtime_globals_taken_from_react(report, browser):
    viz = _run(_seed_viz(report))
    # The stored page is already broken this way; any edit repairs it.
    artifact_id = _run(_seed_page(
        report, viz, _page(viz, head="const { useState, useEffect, KPICard, SectionCard } = React;\n")))

    payload = _run(_tool_end(EditArtifactTool(), report, {
        "artifact_id": artifact_id,
        "edits": [{"find": "<h1>Sales</h1>", "replace": "<h1>Revenue</h1>"}],
    }))

    assert payload["output"]["success"] is True, payload
    saved = _run(_latest_code(report))
    assert "const { useState, useEffect } = React;" in saved
    assert "KPICard" not in saved and "SectionCard" not in saved
    assert "KPICard" in " ".join(payload["observation"]["warnings"])


@pytest.mark.e2e
def test_a_page_that_takes_only_react_exports_is_saved_as_written(report, browser):
    viz = _run(_seed_viz(report))
    code = _page(viz, head="const { useState, useEffect, useMemo } = React;\n")

    payload = _run(_tool_end(CreateArtifactTool(), report, {
        "prompt": "build", "title": "Sales", "mode": "page", "code": code, "visualization_ids": [viz],
    }))

    assert payload["output"].get("artifact_id"), payload
    assert code in _run(_latest_code(report))
    assert "warnings" not in payload["observation"]


@pytest.mark.e2e
def test_invalid_jsx_fails_creation_without_chromium(report, monkeypatch):
    monkeypatch.setenv("BOW_CHROMIUM_EXECUTABLE", "/missing/chromium")
    viz = _run(_seed_viz(report))
    code = _page(viz)
    invalid = '<h1 data-count={1 + }>Example</h1>'
    payload = _run(_tool_end(CreateArtifactTool(), report, {
        "prompt": "build", "title": "Sales", "mode": "page",
        "code": code.replace("<h1>Sales</h1>", invalid), "visualization_ids": [viz],
    }))
    assert payload["output"]["success"] is False
    assert payload["observation"]["error"]["type"] == "parse_error"


@pytest.mark.e2e
def test_invalid_edit_preserves_last_version_without_chromium(report, monkeypatch):
    monkeypatch.setenv("BOW_CHROMIUM_EXECUTABLE", "/missing/chromium")
    viz = _run(_seed_viz(report))
    code = _page(viz)
    invalid = "<h1>Sales</h2>"
    artifact_id = _run(_seed_page(report, viz, code))
    edit = _run(_tool_end(EditArtifactTool(), report, {
        "artifact_id": artifact_id,
        "edits": [{"find": "<h1>Sales</h1>", "replace": invalid}],
    }))
    assert edit["output"]["success"] is False
    assert _run(_latest_code(report)) == code
