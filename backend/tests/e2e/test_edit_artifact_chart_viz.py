"""edit_artifact and TrendChart/RankChart without a viz.

The page check asks every TrendChart/RankChart for the viz its rows come
from. Pages saved before that check may already carry charts without one; an
edit that leaves them alone must still save, while an edit that adds a new
viz-less chart is rejected with the [chart] error.

The edit runs through the real tool. Only the headless browser is stubbed
(the Babel parse gate and the render check need Playwright + Chromium); the
deterministic page checks run as in production.

Run:
    cd backend
    TESTING=true uv run pytest tests/e2e/test_edit_artifact_chart_viz.py --db=sqlite
"""
import asyncio
import uuid
from datetime import datetime, timedelta

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


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def no_browser(monkeypatch):
    """The page parses and renders cleanly; no Playwright needed."""
    from app.ai.tools.implementations import _artifact_parse
    from app.ai.tools.implementations.create_artifact import CreateArtifactTool

    async def _parses(code, timeout_ms=8000):
        return None

    async def _renders(self, html, *args, **kwargs):
        return None, []

    monkeypatch.setattr(_artifact_parse, "parse_check_page_code", _parses)
    monkeypatch.setattr(CreateArtifactTool, "_take_preview_screenshot", _renders)


def _make_report(create_report, create_user, login_user, whoami):
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    return create_report(title=f"R {uuid.uuid4().hex[:6]}", user_token=token, org_id=org_id, data_sources=[])


async def _seed_page(report_id: str, body: str):
    """A viz with rows, and a runtime-11 page whose App renders ``body``."""
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
            data={"rows": [{"month": "2025-01", "rep": "A", "total": 3}, {"month": "2025-02", "rep": "B", "total": 5}],
                  "columns": [{"field": "month"}, {"field": "rep"}, {"field": "total"}]},
            created_at=datetime.utcnow() - timedelta(hours=1),
        )
        db.add(step)
        await db.flush()
        query.default_step_id = step.id
        viz = Visualization(title="Sales", status="success", report_id=report_id,
                            query_id=query.id, view={"type": "table"})
        db.add(viz)
        await db.flush()
        viz_id = str(viz.id)
        code = (
            '<script type="text/babel">\n'
            "function App() {\n"
            f'  const sales = vizById("{viz_id}");\n'
            "  const rows = (sales && sales.rows) || [];\n"
            f"  return (<div>{body}</div>);\n"
            "}\n"
            "ReactDOM.createRoot(document.getElementById('root')).render(<App />);\n"
            "</script>"
        )
        version = await seed_artifact(
            db, report_id=report_id, user_id=str(report.user_id), organization_id=str(report.organization_id),
            content={"code": code, "visualization_ids": [viz_id], "runtime_version": 11},
        )
        await db.commit()
        return str(version.id), viz_id


def _edit(report_id: str, artifact_id: str, find: str, replace: str) -> dict:
    from app.ai.tools.implementations.edit_artifact import EditArtifactTool

    async def go():
        async with async_session_maker() as db:
            report = await db.get(Report, report_id)
            ctx = {"db": db, "report": report, "user": await db.get(User, report.user_id),
                   "organization": await db.get(Organization, report.organization_id)}
            ends = [e async for e in EditArtifactTool().run_stream(
                {"artifact_id": artifact_id, "edits": [{"find": find, "replace": replace}]}, ctx,
            ) if e.type == "tool.end"]
        assert ends, "no tool.end event"
        return ends[-1].payload

    return _run(go())


VIZLESS = '<TrendChart rows={rows} x="month" y="total" />'


@pytest.mark.e2e
def test_an_edit_beside_a_chart_saved_without_a_viz_is_applied(
    create_report, create_user, login_user, whoami, test_client, no_browser
):
    report = _make_report(create_report, create_user, login_user, whoami)
    artifact_id, _ = _run(_seed_page(report["id"], "<h1>Sales</h1>" + VIZLESS))

    payload = _edit(report["id"], artifact_id, "<h1>Sales</h1>", "<h1>Monthly sales</h1>")

    assert payload["output"].get("success") is True, payload["observation"]
    assert payload["output"]["artifact_id"] != artifact_id, "the edit is saved as a new version"


@pytest.mark.e2e
def test_an_edit_that_adds_a_chart_without_a_viz_is_rejected(
    create_report, create_user, login_user, whoami, test_client, no_browser
):
    report = _make_report(create_report, create_user, login_user, whoami)
    artifact_id, viz_id = _run(_seed_page(report["id"], "<h1>Sales</h1>" + VIZLESS))

    added = _edit(report["id"], artifact_id, "<h1>Sales</h1>",
                  '<h1>Sales</h1><RankChart rows={rows} label="rep" value="total" />')
    assert added["output"].get("success") is False
    error = added["observation"]["error"]
    assert error["type"] == "contract_errors"
    assert len(error["errors"]) == 1 and error["errors"][0].startswith("[chart] <RankChart>"), error["errors"]

    # The same chart given the viz is accepted.
    with_viz = _edit(report["id"], artifact_id, "<h1>Sales</h1>",
                     '<h1>Sales</h1><RankChart viz={sales} rows={rows} label="rep" value="total" />')
    assert with_viz["output"].get("success") is True, with_viz["observation"]


@pytest.mark.e2e
def test_an_edit_by_instruction_sends_the_chart_rules_to_the_model(
    create_report, create_user, login_user, whoami, create_llm_provider_and_models, test_client, monkeypatch
):
    """The MCP edit tool writes the code with one prompt; the chart rules must be in it."""
    from app.ai.llm.llm import LLM
    from app.ai.tools.implementations.edit_artifact_legacy import EDIT_NEW_CHARTS_RULE, EDIT_VIZ_PROP_RULE
    from app.ai.tools.mcp.edit_artifact import EditArtifactMCPTool

    monkeypatch.setenv("OPENAI_API_KEY_TEST", "dummy-local-provider-boundary")
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    create_llm_provider_and_models(user_token=token, org_id=org_id)
    report = create_report(title=f"R {uuid.uuid4().hex[:6]}", user_token=token, org_id=org_id, data_sources=[])
    artifact_id, _ = _run(_seed_page(report["id"], "<h1>Sales</h1>"))

    prompts = []

    def _model(self, prompt, *args, usage_scope=None, **kwargs):
        if usage_scope == "mcp_edit_artifact":  # the edit itself, not context selection
            prompts.append(prompt)
        return ""

    monkeypatch.setattr(LLM, "inference", _model)

    async def go():
        async with async_session_maker() as db:
            r = await db.get(Report, report["id"])
            return await EditArtifactMCPTool().execute(
                {"report_id": report["id"], "artifact_id": artifact_id, "edit_instruction": "Add a monthly trend chart"},
                db, await db.get(User, r.user_id), await db.get(Organization, r.organization_id),
            )

    _run(go())
    assert len(prompts) == 1, "the model was not asked for the edit"
    for rule in (EDIT_VIZ_PROP_RULE, EDIT_NEW_CHARTS_RULE):
        assert rule in prompts[0], rule[:80]
    # Rows from no visualization: the rule's own wording of the null viz reaches the model.
    assert "passes `viz={null}`" in prompts[0]
