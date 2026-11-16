"""Page artifacts survive the runtime mistakes weaker models make, and a page
that renders nothing is never saved as working.

Artifact code is one classic <script type="text/babel"> whose runtime provides
React, ReactDOM and the BoW helpers (useArtifactData, vizById, ...) as globals.
Models that write ES-module React instead (imports, `export default`, helpers
destructured from React, a bare createRoot, no mount call at all) get their
code corrected before validation. A page that throws nothing but renders
nothing (App never mounted, App returns null) fails validation instead of
being stored as a dashboard stuck on "Loading…".

These tests run the real create_artifact / edit_artifact tools with the real
headless-Chromium render check, so they skip without Playwright + Chromium +
the vendored JS libs.

Run:
    cd backend
    TESTING=true uv run pytest tests/e2e/test_artifact_runtime_mistakes.py --db=sqlite
"""
import asyncio
import os
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from app.dependencies import async_session_maker
from app.models.organization import Organization
from app.models.query import Query
from app.models.report import Report
from app.models.step import Step
from app.models.user import User
from app.models.visualization import Visualization
from app.models.widget import Widget


def _browser_stack_available() -> bool:
    # Sync API on purpose: evaluated at import, outside any event loop.
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False
    from app.services.artifact_libs import _GLOBALS_FILENAME, _PAGE_LIBS, _find_libs_dir

    libs_dir = _find_libs_dir()
    if libs_dir is None or not all((libs_dir / n).is_file() for n in (*_PAGE_LIBS, _GLOBALS_FILENAME)):
        return False
    override = os.environ.get("BOW_CHROMIUM_EXECUTABLE")
    if override:
        return Path(override).exists()
    try:
        with sync_playwright() as p:
            return Path(p.chromium.executable_path).exists()
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _browser_stack_available(),
    reason="needs Playwright + Chromium + the vendored JS libs (scripts/download-vendor-libs.sh)",
)


def _run(coro):
    return asyncio.run(coro)


def _make_report(create_report, create_user, login_user, whoami):
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    return create_report(title=f"R {uuid.uuid4().hex[:6]}", user_token=token, org_id=org_id, data_sources=[])


async def _seed_viz(report_id: str) -> str:
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
        step = Step(
            title="S", slug=f"s-{suffix}", status="success", widget_id=widget.id, query_id=query.id, code="",
            data={"rows": [{"artist": "Queen", "albums": 10}, {"artist": "Abba", "albums": 8}],
                  "columns": [{"field": "artist"}, {"field": "albums"}]},
            created_at=datetime.utcnow() - timedelta(hours=1),
        )
        db.add(step)
        await db.flush()
        query.default_step_id = step.id
        viz = Visualization(title="Albums", status="success", report_id=report_id,
                            query_id=query.id, view={"type": "table"})
        db.add(viz)
        await db.commit()
        return str(viz.id)


async def _ctx(db, report_id):
    report = await db.get(Report, report_id)
    return {"db": db, "report": report, "user": await db.get(User, report.user_id),
            "organization": await db.get(Organization, report.organization_id)}


async def _tool_end(tool_cls, report_id: str, tool_input: dict) -> dict:
    async with async_session_maker() as db:
        ends = [e async for e in tool_cls().run_stream(tool_input, await _ctx(db, report_id)) if e.type == "tool.end"]
    assert ends, "no tool.end event"
    return ends[-1].payload


def _create(report_id, viz_id, code):
    from app.ai.tools.implementations.create_artifact import CreateArtifactTool
    return _run(_tool_end(CreateArtifactTool, report_id, {
        "prompt": "albums table", "title": "Albums", "mode": "page", "code": code, "visualization_ids": [viz_id],
    }))


def _edit(report_id, artifact_id, find, replace):
    from app.ai.tools.implementations.edit_artifact import EditArtifactTool
    return _run(_tool_end(EditArtifactTool, report_id, {
        "artifact_id": artifact_id, "edits": [{"find": find, "replace": replace}],
    }))


async def _stored(version_id: str) -> dict:
    from app.services.artifact_service import ArtifactService
    async with async_session_maker() as db:
        row = await ArtifactService().get(db, version_id)
        return {"status": row.status, "code": (row.content or {}).get("code", "")}


def _body(viz_id: str) -> str:
    return (
        "function App() {\n"
        "  const data = useArtifactData();\n"
        "  const [q, setQ] = useState('');\n"
        f"  const rows = ((data && vizById('{viz_id}')) || {{}}).rows || [];\n"
        "  const shown = rows.filter(r => String(r.artist).toLowerCase().includes(q.toLowerCase()));\n"
        "  return (<div><h1>Albums</h1><input value={q} onChange={e => setQ(e.target.value)} />"
        "<ul>{shown.map(r => <li key={r.artist}>{r.artist}: {r.albums}</li>)}</ul></div>);\n"
        "}\n"
    )


MOUNT = "ReactDOM.createRoot(document.getElementById('root')).render(<App />);\n"

# Each wraps the same working App in one ES-module-style mistake.
MISTAKES = {
    "imports_and_export_default": lambda v: (
        "import React, { useState } from 'react';\nimport ReactDOM from 'react-dom/client';\n"
        + "export default " + _body(v) + MOUNT
    ),
    "runtime_globals_destructured_from_react": lambda v: (
        "const { useState, useArtifactData, vizById } = React;\n" + _body(v) + MOUNT
    ),
    "bare_create_root": lambda v: (
        "import { createRoot } from 'react-dom/client';\n" + _body(v)
        + "createRoot(document.getElementById('root')).render(<App />);\n"
    ),
    "app_never_mounted": lambda v: _body(v),
}


@pytest.mark.e2e
@pytest.mark.parametrize("mistake", sorted(MISTAKES))
def test_runtime_mistakes_are_corrected_and_the_page_renders(
    mistake, create_report, create_user, login_user, whoami, test_client
):
    report = _make_report(create_report, create_user, login_user, whoami)
    viz_id = _run(_seed_viz(report["id"]))

    code = '<script type="text/babel">\n' + MISTAKES[mistake](viz_id) + "</script>"
    payload = _create(report["id"], viz_id, code)

    assert payload["output"].get("success") is not False, payload["observation"].get("summary")
    assert not payload["observation"].get("render_errors"), payload["observation"]
    # The model is told what was corrected, so it stops writing it.
    assert payload["observation"].get("warnings"), payload["observation"]
    stored = _run(_stored(payload["output"]["artifact_id"]))
    assert stored["status"] == "completed"


@pytest.mark.e2e
@pytest.mark.parametrize("mounted", [True, False], ids=["mounted", "never_mounted"])
def test_a_page_that_renders_nothing_is_not_saved_as_working(
    mounted, create_report, create_user, login_user, whoami, test_client
):
    report = _make_report(create_report, create_user, login_user, whoami)
    viz_id = _run(_seed_viz(report["id"]))

    stub = f"function App() {{\n  const v = vizById('{viz_id}');\n  return null;\n}}\n"
    code = '<script type="text/babel">\n' + stub + (MOUNT if mounted else "") + "</script>"
    payload = _create(report["id"], viz_id, code)

    assert payload["output"].get("success") is False
    assert payload["observation"]["error"]["type"] == "render_validation_failed"
    stored = _run(_stored(payload["output"]["artifact_id"]))
    assert stored["status"] != "completed"


@pytest.mark.e2e
def test_edit_that_empties_the_page_is_rejected_and_a_dropped_mount_is_restored(
    create_report, create_user, login_user, whoami, test_client
):
    report = _make_report(create_report, create_user, login_user, whoami)
    viz_id = _run(_seed_viz(report["id"]))
    created = _create(report["id"], viz_id, '<script type="text/babel">\n' + _body(viz_id) + MOUNT + "</script>")
    artifact_id = created["output"]["artifact_id"]

    emptied = _edit(report["id"], artifact_id, "return (<div><h1>Albums</h1>", "return null; (<div><h1>Albums</h1>")
    assert emptied["output"].get("success") is False

    unmounted = _edit(report["id"], artifact_id, MOUNT, "")
    assert unmounted["output"].get("success") is True, unmounted["observation"]
    assert unmounted["observation"].get("warnings"), unmounted["observation"]
    assert "createRoot" in _run(_stored(unmounted["output"]["artifact_id"]))["code"]
