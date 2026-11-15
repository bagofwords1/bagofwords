"""create_artifact and edit_artifact repair module syntax and explain missing names.

A model that writes `import { … } from 'bow'` / `export default App` used to
get Babel's raw "'import' and 'export' may appear only with sourceType:
module" and resubmit the same page. The statements are now removed before
anything is checked or saved, and a render error about a missing runtime name
comes back with what to change. Pages written the supported way are saved
exactly as written.

Rendering is the stubbed boundary (it needs Chromium).

Run:
    cd backend
    TESTING=true uv run pytest tests/e2e/test_artifact_page_normalization.py --db=sqlite
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


async def _versions(report_id: str):
    async with async_session_maker() as db:
        rows = (await db.execute(
            select(ArtifactVersion).where(ArtifactVersion.report_id == report_id)
        )).scalars().all()
        return sorted(((r.version, (r.content or {}).get("code", ""), r.status) for r in rows))


async def _seed_page(report_id: str, viz_id: str, code: str):
    async with async_session_maker() as db:
        report = await db.get(Report, report_id)
        version = await seed_artifact(
            db, report_id=report_id, user_id=report.user_id, organization_id=report.organization_id,
            content={"code": code, "visualization_ids": [viz_id]},
        )
        await db.commit()
        return str(version.id)


def _body(viz_id: str) -> str:
    return (f'function App() {{ const v = vizById("{viz_id}"); '
            "return <div><h1>Sales</h1><p>{v && v.title}</p></div> }\n"
            "ReactDOM.createRoot(document.getElementById('root')).render(<App />);")


@pytest.fixture
def report(create_report, create_user, login_user, whoami, test_client):
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    return create_report(title=f"r-{uuid.uuid4().hex[:6]}", user_token=token, org_id=org_id, data_sources=[])["id"]


@pytest.fixture
def renders(monkeypatch):
    """The browser renders whatever it is given without errors."""
    async def _screenshot(self, *_args, **_kwargs):
        return "c2NyZWVuc2hvdA==", []

    async def _validate(self, code, artifact_data, mode, runtime_ctx, **kwargs):
        yield {"code": code, "clean": True, "screenshot": "c2NyZWVuc2hvdA==", "errors": [], "repair_attempts": 0}

    async def _parses(code):
        return None

    monkeypatch.setattr(CreateArtifactTool, "_take_preview_screenshot", _screenshot)
    monkeypatch.setattr(CreateArtifactTool, "_validate_and_repair_stream", _validate)
    monkeypatch.setattr("app.ai.tools.implementations._artifact_parse.parse_check_page_code", _parses)


MODULE_STYLE = [
    ("import React from 'react';\nimport { useArtifactData, vizById, fmt } from 'bow';\n", "\nexport default App;"),
    ("import { KPICard, SectionCard } from '@bow/kit';\n", ""),
    ("", "\nexport default App;"),
]


@pytest.mark.e2e
@pytest.mark.parametrize("head, tail", MODULE_STYLE, ids=["imports and export", "imports only", "export only"])
def test_create_saves_a_module_style_page_as_a_plain_script(report, renders, head, tail):
    viz = _run(_seed_viz(report))

    payload = _run(_tool_end(CreateArtifactTool(), report, {
        "prompt": "build", "title": "Sales", "mode": "page",
        "code": head + _body(viz) + tail, "visualization_ids": [viz],
    }))

    assert payload["output"].get("artifact_id"), payload
    [(_, saved, _status)] = _run(_versions(report))
    assert "import " not in saved and "export " not in saved
    assert _body(viz) in saved
    # The model is told, so it stops writing it.
    assert "module statements" in " ".join(payload["observation"]["warnings"])


@pytest.mark.e2e
def test_create_saves_a_page_written_the_supported_way_exactly_as_written(report, renders):
    viz = _run(_seed_viz(report))
    code = _body(viz)

    payload = _run(_tool_end(CreateArtifactTool(), report, {
        "prompt": "build", "title": "Sales", "mode": "page", "code": code, "visualization_ids": [viz],
    }))

    assert payload["output"].get("artifact_id"), payload
    [(_, saved, _status)] = _run(_versions(report))
    assert code in saved
    assert "warnings" not in payload["observation"]


@pytest.mark.e2e
def test_edit_repairs_module_syntax_the_edit_introduces(report, renders):
    viz = _run(_seed_viz(report))
    artifact_id = _run(_seed_page(report, viz, _body(viz)))

    payload = _run(_tool_end(EditArtifactTool(), report, {
        "artifact_id": artifact_id,
        "edits": [{"find": "function App() {", "replace": "import { fmt } from 'bow';\nexport default function App() {"}],
    }))

    assert payload["output"]["success"] is True, payload
    versions = _run(_versions(report))
    assert versions[0][1] == _body(viz), "the stored version is never rewritten"
    latest = versions[-1][1]
    assert "import " not in latest and "export " not in latest and "function App() {" in latest


@pytest.mark.e2e
@pytest.mark.parametrize("error, meant", [
    ("useVizById is not defined", "vizById"),
    ("bow.useArtifactData is not a function", "useArtifactData"),
])
def test_a_render_error_about_a_missing_helper_says_what_to_use(report, monkeypatch, error, meant):
    viz = _run(_seed_viz(report))

    async def _fails(self, code, artifact_data, mode, runtime_ctx, **kwargs):
        yield {"code": code, "clean": False, "screenshot": None, "errors": [error], "repair_attempts": 0}

    async def _parses(code):
        return None

    monkeypatch.setattr(CreateArtifactTool, "_validate_and_repair_stream", _fails)
    monkeypatch.setattr("app.ai.tools.implementations._artifact_parse.parse_check_page_code", _parses)

    payload = _run(_tool_end(CreateArtifactTool(), report, {
        "prompt": "build", "title": "Sales", "mode": "page", "code": _body(viz), "visualization_ids": [viz],
    }))

    assert payload["output"]["success"] is False
    remediation = payload["observation"]["error"]["remediation"]
    assert f"`{meant}`" in remediation and "global" in remediation
