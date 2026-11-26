"""A chart that can't draw from its props is a render error the agent repairs.

TrendChart/RankChart draw a warning box instead of a chart when their props are
wrong (no viz and no rows, a missing x/y or label/value, a field the rows don't
have). The headless render check used to see no error at all, so a weak model
was told the page worked while every chart was a warning box. Now the chart's
own message is a fatal render error: create_artifact and edit_artifact fail
with it, so the agent gets the exact text to fix.

A chart whose viz is gone (``viz`` passed but null: its query was deleted) is a
viewer state, not an author error, and an unrelated console.warn stays ignored.

These run the real create_artifact / edit_artifact tools with the real
headless-Chromium render check, so they skip without Playwright + Chromium +
the vendored JS libs.

Run:
    cd backend
    TESTING=true uv run pytest tests/e2e/test_chart_render_errors.py -m e2e --db=sqlite
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


pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        not _browser_stack_available(),
        reason="needs Playwright + Chromium + the vendored JS libs (scripts/download-vendor-libs.sh)",
    ),
]

CHARTS = ("TrendChart", "RankChart")
MOUNT = "ReactDOM.createRoot(document.getElementById('root')).render(<App />);\n"


def _run(coro):
    return asyncio.run(coro)


def _make_report(create_report, create_user, login_user, whoami):
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    return create_report(title=f"R {uuid.uuid4().hex[:6]}", user_token=token, org_id=org_id, data_sources=[])


async def _seed_viz(report_id: str) -> str:
    """A viz with an `artist` label column and an `albums` count (direct rows:
    the run-state API can't produce a finished step)."""
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
            data={"rows": [{"artist": "Queen", "albums": 10}, {"artist": "Abba", "albums": 8},
                           {"artist": "Muse", "albums": 9}],
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


def _create(report_id, viz_ids, app):
    from app.ai.tools.implementations.create_artifact import CreateArtifactTool
    code = '<script type="text/babel">\n' + app + MOUNT + "</script>"
    return _run(_tool_end(CreateArtifactTool, report_id, {
        "prompt": "albums by artist", "title": "Albums", "mode": "page", "code": code,
        "visualization_ids": viz_ids,
    }))


def _edit(report_id, artifact_id, find, replace):
    from app.ai.tools.implementations.edit_artifact import EditArtifactTool
    return _run(_tool_end(EditArtifactTool, report_id, {
        "artifact_id": artifact_id, "edits": [{"find": find, "replace": replace}],
    }))


def _render_errors(payload) -> list:
    obs = payload["observation"]
    return list((obs.get("error") or {}).get("render_errors") or obs.get("render_errors") or [])


def _chart_errors(payload) -> list:
    """Render errors that carry a chart component's own message and gate
    completion (``[console.error]`` entries are advisory)."""
    return [e for e in _render_errors(payload)
            if any(c in e for c in CHARTS) and not e.startswith("[console.error]")]


def _ranked(viz_id, chart='<RankChart viz={albums} label="artist" value="albums" />', before=""):
    return (
        "function App() {\n"
        f"  const albums = vizById('{viz_id}');\n"
        f"{before}"
        f"  return (<div><h1>Albums</h1><p>Albums by artist</p>{chart}</div>);\n"
        "}\n"
    )


# -- the chart's own error reaches the agent ---------------------------------

def test_a_chart_with_no_viz_and_no_rows_fails_create_with_its_message(
    create_report, create_user, login_user, whoami, test_client
):
    # A page without visualizations: no page check looks at this chart, only
    # the render shows that it draws the warning box.
    report = _make_report(create_report, create_user, login_user, whoami)
    app = "function App() {\n  return (<div><h1>Revenue</h1><TrendChart x=\"month\" y=\"revenue\" /></div>);\n}\n"

    payload = _create(report["id"], [], app)

    assert payload["output"].get("success") is False, payload["observation"].get("summary")
    errors = _chart_errors(payload)
    assert errors and "TrendChart" in errors[0], _render_errors(payload)
    assert "viz" in errors[0], errors
    # The agent is pointed at the chart message, not a generic failure.
    assert "TrendChart" in payload["observation"]["error"]["message"], payload["observation"]


def test_a_chart_missing_a_field_prop_fails_create_and_lists_the_fields(
    create_report, create_user, login_user, whoami, test_client
):
    report = _make_report(create_report, create_user, login_user, whoami)
    viz_id = _run(_seed_viz(report["id"]))

    payload = _create(report["id"], [viz_id], _ranked(viz_id, '<RankChart viz={albums} value="albums" />'))

    assert payload["output"].get("success") is False, payload["observation"].get("summary")
    errors = _chart_errors(payload)
    assert errors and "RankChart" in errors[0], _render_errors(payload)
    # Actionable: the fields the chart could have used.
    assert "artist" in errors[0] and "albums" in errors[0], errors


def test_an_edit_that_breaks_a_chart_is_rejected_with_its_message(
    create_report, create_user, login_user, whoami, test_client
):
    report = _make_report(create_report, create_user, login_user, whoami)
    viz_id = _run(_seed_viz(report["id"]))
    created = _create(report["id"], [viz_id], _ranked(viz_id))
    assert created["output"].get("success") is not False, created["observation"].get("summary")

    edited = _edit(report["id"], created["output"]["artifact_id"], 'label="artist" ', "")

    assert edited["output"].get("success") is False, edited["observation"].get("summary")
    errors = _chart_errors(edited)
    assert errors and "RankChart" in errors[0], edited["observation"]
    assert "RankChart" in edited["observation"]["error"]["message"], edited["observation"]


# -- controls: pages whose charts are fine stay fine -------------------------

CONTROLS = {
    "a_correct_chart": lambda v: _ranked(v),
    "an_unrelated_console_warn": lambda v: _ranked(v, before="  console.warn('albums are sorted by name');\n"),
    # viz passed but null: what a chart shows when its viz was removed.
    "a_chart_whose_viz_is_gone": lambda v: _ranked(
        v, '<RankChart viz={albums} label="artist" value="albums" /><TrendChart viz={null} x="month" y="sales" />'),
}


@pytest.mark.parametrize("page", sorted(CONTROLS))
def test_a_page_whose_charts_draw_or_are_empty_is_saved(
    page, create_report, create_user, login_user, whoami, test_client
):
    report = _make_report(create_report, create_user, login_user, whoami)
    viz_id = _run(_seed_viz(report["id"]))

    payload = _create(report["id"], [viz_id], CONTROLS[page](viz_id))

    assert payload["output"].get("success") is not False, payload["observation"]
    assert _chart_errors(payload) == [], _render_errors(payload)
