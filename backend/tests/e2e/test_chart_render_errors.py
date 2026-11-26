"""A chart that can't draw from its props is a render error the agent repairs.

TrendChart/RankChart draw a warning box instead of a chart when their props are
wrong (no viz and no rows, a missing x/y or label/value, a field the rows don't
have). The headless render check used to see no error at all, so a weak model
was told the page worked while every chart was a warning box. Now the chart's
own message is a render error:

- it drives the in-tool repair while attempts remain;
- when chart errors are all that is left, the page is saved anyway and the
  result carries them as a warning to fix with edit_artifact (a thrown error
  still fails the call);
- an edit is judged on the chart errors it adds: a chart the page already got
  wrong never blocks an unrelated edit.

A chart whose viz is gone (``viz`` passed but null: its query was deleted) is a
viewer state, not an author error, and an unrelated console.warn stays ignored.

These run the real create_artifact / edit_artifact tools with the real
headless-Chromium render check, so they skip without Playwright + Chromium +
the vendored JS libs. Only the LLM provider is faked, where a tool writes code.

Run:
    cd backend
    TESTING=true uv run pytest tests/e2e/test_chart_render_errors.py -m e2e --db=sqlite
"""
import asyncio
import json
import os
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.dependencies import async_session_maker
from app.models.organization import Organization
from app.models.query import Query
from app.models.report import Report
from app.models.step import Step
from app.models.user import User
from app.models.visualization import Visualization
from app.models.widget import Widget
from tests.fixtures.artifact import seed_artifact


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


def _script(app: str) -> str:
    return '<script type="text/babel">\n' + app + MOUNT + "</script>"


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


async def _seed_page(report_id: str, viz_id: str, app: str) -> str:
    """A page saved before chart errors were render-checked (no API makes one
    now: a broken chart either gets fixed or is saved with a warning)."""
    async with async_session_maker() as db:
        report = await db.get(Report, report_id)
        version = await seed_artifact(
            db, report_id=report_id, user_id=str(report.user_id), organization_id=str(report.organization_id),
            content={"code": _script(app), "visualization_ids": [viz_id], "runtime_version": 11},
        )
        await db.commit()
        return str(version.id)


async def _ctx(db, report_id, model=None):
    report = await db.get(Report, report_id)
    ctx = {"db": db, "report": report, "user": await db.get(User, report.user_id),
           "organization": await db.get(Organization, report.organization_id)}
    if model is not None:
        ctx["model"] = model
    return ctx


async def _tool_end(tool_cls, report_id: str, tool_input: dict, model=None) -> dict:
    async with async_session_maker() as db:
        ends = [e async for e in tool_cls().run_stream(tool_input, await _ctx(db, report_id, model))
                if e.type == "tool.end"]
    assert ends, "no tool.end event"
    return ends[-1].payload


def _create(report_id, viz_ids, app=None, model=None):
    """create_artifact with the planner's code, or (app=None) the tool writes it."""
    from app.ai.tools.implementations.create_artifact import CreateArtifactTool
    tool_input = {"prompt": "albums by artist", "title": "Albums", "mode": "page", "visualization_ids": viz_ids}
    if app is not None:
        tool_input["code"] = _script(app)
    return _run(_tool_end(CreateArtifactTool, report_id, tool_input, model))


def _edit(report_id, artifact_id, find, replace):
    from app.ai.tools.implementations.edit_artifact import EditArtifactTool
    return _run(_tool_end(EditArtifactTool, report_id, {
        "artifact_id": artifact_id, "edits": [{"find": find, "replace": replace}],
    }))


def _legacy_edit(report_id, artifact_id, model):
    from app.ai.tools.implementations.edit_artifact_legacy import EditArtifactTool
    return _run(_tool_end(EditArtifactTool, report_id, {
        "artifact_id": artifact_id, "edit_prompt": "Change the page",
    }, model))


async def _stored(version_id: str) -> dict:
    from app.services.artifact_service import ArtifactService
    async with async_session_maker() as db:
        row = await ArtifactService().get(db, version_id)
        return {"status": row.status, "code": (row.content or {}).get("code", "")}


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


# -- the LLM provider boundary, for the tools that write code themselves ------

FAKE_MODEL = SimpleNamespace(
    model_id="fake-model", organization_id=None, supports_vision=False,
    provider=SimpleNamespace(provider_type="openai", additional_config={}, decrypt_credentials=lambda: ["k"]),
)


class _Provider:
    """Answers each LLM call by its usage scope; records what each was asked."""

    def __init__(self):
        self.answers = {}
        self.asked = []

    def install(self, monkeypatch):
        from app.ai.llm.llm import LLM
        from app.ai.llm.types import MessageStopEvent, TextDeltaEvent

        provider = self

        async def scripted(self, *, messages, usage_scope=None, **kwargs):
            text = json.dumps([getattr(m, "content", m) for m in messages or []], default=str)
            provider.asked.append((usage_scope, text))
            answer = provider.answers.get(usage_scope)
            yield TextDeltaEvent(text=answer(text) if callable(answer) else (answer or ""))
            yield MessageStopEvent(stop_reason="end_turn", raw_stop_reason="end_turn")

        monkeypatch.setattr(LLM, "inference_stream_v2", scripted)

    def prompts(self, scope):
        return [text for s, text in self.asked if s == scope]


@pytest.fixture
def provider(monkeypatch):
    p = _Provider()
    p.install(monkeypatch)
    return p


# -- create: the chart's own error reaches the agent -------------------------

# A page without visualizations: no page check looks at this chart, only the
# render shows that it draws the warning box.
NO_VIZ_NO_ROWS = "function App() {\n  return (<div><h1>Revenue</h1><TrendChart x=\"month\" y=\"revenue\" /></div>);\n}\n"
ROWS = "const ROWS = [{ month: '2025-01', revenue: 3 }, { month: '2025-02', revenue: 5 }];\n"
TREND_MISSING_X = ROWS + "function App() {\n  return (<div><h1>Revenue</h1><TrendChart rows={ROWS} y=\"revenue\" /></div>);\n}\n"
TREND_FIXED = TREND_MISSING_X.replace('y="revenue"', 'x="month" y="revenue"')


@pytest.mark.parametrize("page", ["no_viz_and_no_rows", "missing_a_field_prop"])
def test_a_chart_the_planners_code_gets_wrong_is_saved_with_a_warning(
    page, create_report, create_user, login_user, whoami, test_client
):
    report = _make_report(create_report, create_user, login_user, whoami)
    if page == "no_viz_and_no_rows":
        viz_ids, app, chart, fields = [], NO_VIZ_NO_ROWS, "TrendChart", ()
    else:
        viz_id = _run(_seed_viz(report["id"]))
        viz_ids, app, chart, fields = [viz_id], _ranked(viz_id, '<RankChart viz={albums} value="albums" />'), \
            "RankChart", ("artist", "albums")

    payload = _create(report["id"], viz_ids, app)

    assert payload["output"].get("success") is not False, payload["observation"]
    assert _run(_stored(payload["output"]["artifact_id"]))["status"] == "completed"
    obs = payload["observation"]
    assert obs["chart_errors"] == payload["output"]["chart_errors"]
    assert len(obs["chart_errors"]) == 1 and chart in obs["chart_errors"][0], obs["chart_errors"]
    # Actionable: the chart's own message, its fields, and the tool to fix it with.
    assert all(f in obs["chart_errors"][0] for f in fields), obs["chart_errors"]
    assert chart in obs["summary"] and "edit_artifact" in obs["summary"], obs["summary"]


def test_a_thrown_error_beside_a_broken_chart_still_fails_create(
    create_report, create_user, login_user, whoami, test_client
):
    report = _make_report(create_report, create_user, login_user, whoami)
    app = NO_VIZ_NO_ROWS.replace(
        "  return", "  React.useEffect(() => { throw new Error('albums failed to load'); }, []);\n  return")

    payload = _create(report["id"], [], app)

    assert payload["output"].get("success") is False, payload["observation"].get("summary")
    assert any("albums failed to load" in e for e in _render_errors(payload)), _render_errors(payload)
    assert "chart_errors" not in payload["observation"]


def test_chart_errors_drive_the_in_tool_repair_first(
    provider, create_report, create_user, login_user, whoami, test_client
):
    report = _make_report(create_report, create_user, login_user, whoami)
    provider.answers["create_artifact"] = "```jsx\n" + _script(TREND_MISSING_X) + "```"
    provider.answers["create_artifact_fix"] = lambda asked: _script(TREND_FIXED) if "TrendChart" in asked else ""

    payload = _create(report["id"], [], model=FAKE_MODEL)

    assert payload["output"].get("success") is not False, payload["observation"]
    fixes = provider.prompts("create_artifact_fix")
    assert fixes and "TrendChart needs x and y" in fixes[0], "the repair was asked with the chart's message"
    assert 'x="month"' in _run(_stored(payload["output"]["artifact_id"]))["code"]
    assert "chart_errors" not in payload["observation"]


def test_a_chart_the_repair_cant_fix_is_saved_with_a_warning(
    provider, create_report, create_user, login_user, whoami, test_client
):
    report = _make_report(create_report, create_user, login_user, whoami)
    provider.answers["create_artifact"] = "```jsx\n" + _script(TREND_MISSING_X) + "```"
    provider.answers["create_artifact_fix"] = _script(TREND_MISSING_X)   # "repairs" it unchanged

    payload = _create(report["id"], [], model=FAKE_MODEL)

    assert provider.prompts("create_artifact_fix"), "the repair ran first"
    assert payload["output"].get("success") is not False, payload["observation"]
    assert _run(_stored(payload["output"]["artifact_id"]))["status"] == "completed"
    assert "TrendChart" in payload["observation"]["chart_errors"][0]


# -- edit: only the chart errors an edit adds count ---------------------------

def _page_with_a_broken_trend(viz_id):
    # The TrendChart has no x: the page already draws one warning box.
    return _ranked(viz_id, '<RankChart viz={albums} label="artist" value="albums" />'
                           '<TrendChart viz={albums} y="albums" />')


def test_an_edit_beside_a_chart_the_page_already_got_wrong_is_applied(
    create_report, create_user, login_user, whoami, test_client
):
    report = _make_report(create_report, create_user, login_user, whoami)
    viz_id = _run(_seed_viz(report["id"]))
    artifact_id = _run(_seed_page(report["id"], viz_id, _page_with_a_broken_trend(viz_id)))

    edited = _edit(report["id"], artifact_id, "<h1>Albums</h1>", "<h1>Albums per artist</h1>")

    assert edited["output"].get("success") is True, edited["observation"]


def test_an_edit_that_breaks_another_chart_is_rejected_for_that_chart(
    create_report, create_user, login_user, whoami, test_client
):
    report = _make_report(create_report, create_user, login_user, whoami)
    viz_id = _run(_seed_viz(report["id"]))
    artifact_id = _run(_seed_page(report["id"], viz_id, _page_with_a_broken_trend(viz_id)))

    edited = _edit(report["id"], artifact_id, 'label="artist" ', "")

    assert edited["output"].get("success") is False, edited["observation"].get("summary")
    errors = _chart_errors(edited)
    assert errors and all("RankChart" in e for e in errors), errors


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


def _diff(find, replace):
    return f"<<<<<<< SEARCH\n{find}\n=======\n{replace}\n>>>>>>> REPLACE\n"


def test_a_legacy_edit_beside_a_chart_the_page_already_got_wrong_is_applied_without_repair(
    provider, create_report, create_user, login_user, whoami, test_client
):
    report = _make_report(create_report, create_user, login_user, whoami)
    viz_id = _run(_seed_viz(report["id"]))
    artifact_id = _run(_seed_page(report["id"], viz_id, _page_with_a_broken_trend(viz_id)))
    provider.answers["edit_artifact"] = _diff("<h1>Albums</h1>", "<h1>Albums per artist</h1>")

    edited = _legacy_edit(report["id"], artifact_id, FAKE_MODEL)

    assert provider.prompts("create_artifact_fix") == [], "the old chart error drove a repair"
    assert edited["output"].get("success") is not False, edited["observation"]
    assert "chart_errors" not in edited["observation"]


def test_a_legacy_edit_that_breaks_a_chart_is_repaired_then_saved_with_a_warning(
    provider, create_report, create_user, login_user, whoami, test_client
):
    report = _make_report(create_report, create_user, login_user, whoami)
    viz_id = _run(_seed_viz(report["id"]))
    artifact_id = _run(_seed_page(report["id"], viz_id, _page_with_a_broken_trend(viz_id)))
    provider.answers["edit_artifact"] = _diff('label="artist" ', "")
    # The repair can't fix it: it sends the edited code back unchanged.
    provider.answers["create_artifact_fix"] = _script(_page_with_a_broken_trend(viz_id).replace('label="artist" ', ""))

    edited = _legacy_edit(report["id"], artifact_id, FAKE_MODEL)

    fixes = provider.prompts("create_artifact_fix")
    assert fixes and "RankChart" in fixes[0], "the new chart error drove the repair"
    assert edited["output"].get("success") is not False, edited["observation"]
    charts = edited["observation"]["chart_errors"]
    assert charts and all("RankChart" in e for e in charts), charts
    assert "edit_artifact" in edited["observation"]["summary"], edited["observation"]["summary"]


# -- controls: pages whose charts draw or are empty stay fine ----------------

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
    assert "chart_errors" not in payload["observation"]
