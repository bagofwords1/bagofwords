"""A page the tool writes itself must pass the page checks, through create_artifact's public path.

When the planner sends no code, create_artifact asks its own LLM for the page
and repairs it in-tool. It used to save the page even if a page check still
failed (only a note rode along), so every check could be skipped by not
sending code. Now the call fails with the check errors and no completed
version is saved.

Only the external boundaries are faked: the LLM (it writes a raw line chart,
and its "repair" returns the same code) and the headless browser (the page
renders without a thrown error).

Run:
    cd backend
    TESTING=true uv run pytest tests/e2e/test_generated_page_checks.py --db=sqlite
"""
import asyncio

import pytest

from app.dependencies import async_session_maker
from app.models.organization import Organization
from app.models.report import Report
from app.models.user import User

from tests.e2e.test_create_artifact_replaces import _make_report, _seed_viz

RAW_LINE_PAGE = (
    '```jsx\n<script type="text/babel">\n'
    "function App() {\n"
    "  const option = { xAxis: { type: 'category', data: ['a', 'b'] }, yAxis: { type: 'value' },\n"
    "    series: [{ type: 'line', data: [1, 2] }] };\n"
    "  return <EChart option={option} />;\n"
    "}\n"
    "ReactDOM.createRoot(document.getElementById('root')).render(<App />);\n"
    "</script>\n```"
)


class _FakeLLM:
    """The model writes a raw line chart; the in-tool repair returns the same page."""

    def __init__(self, *args, **kwargs):
        pass

    async def inference_stream_v2(self, **kwargs):
        from app.ai.llm.types import TextDeltaEvent
        yield TextDeltaEvent(text=RAW_LINE_PAGE)


@pytest.fixture()
def fake_boundaries(monkeypatch):
    import app.ai.tools.implementations.create_artifact as mod

    async def _renders(self, html):
        return None, []          # the browser: renders, no thrown error

    monkeypatch.setattr(mod, "LLM", _FakeLLM)
    monkeypatch.setattr(mod.CreateArtifactTool, "_take_preview_screenshot", _renders)
    # No vendored browser libs in CI: give the validator a page to "render".
    monkeypatch.setattr(mod.CreateArtifactTool, "_build_thumbnail_html", lambda self, data, code, mode="page": "<html></html>")


async def _create(report_id: str, viz_id: str):
    from app.ai.tools.implementations.create_artifact import CreateArtifactTool

    async with async_session_maker() as db:
        report = await db.get(Report, report_id)
        user = await db.get(User, report.user_id)
        organization = await db.get(Organization, report.organization_id)
        runtime_ctx = {"db": db, "report": report, "user": user, "organization": organization}
        events = []
        tool_input = {"prompt": "A line chart of revenue", "title": "Revenue", "mode": "page", "visualization_ids": [viz_id]}
        async for evt in CreateArtifactTool().run_stream(tool_input, runtime_ctx):
            events.append(evt)
    ends = [e for e in events if e.type == "tool.end"]
    assert ends, f"no tool.end event in {[e.type for e in events]}"
    return ends[-1].payload


async def _completed_versions(report_id: str) -> int:
    from sqlalchemy import text
    async with async_session_maker() as db:
        row = await db.execute(text(
            "select count(*) from artifact_versions v join artifacts a on a.id = v.artifact_id "
            "where a.report_id = :r and v.status = 'completed'"), {"r": report_id})
        return int(row.scalar() or 0)


@pytest.mark.e2e
def test_a_generated_page_that_fails_a_check_is_not_saved_as_completed(
    create_report, create_user, login_user, whoami, test_client, fake_boundaries
):
    report, _token, _org = _make_report(create_report, create_user, login_user, whoami, "Generated page checks")
    viz_id = asyncio.run(_seed_viz(report["id"]))

    payload = asyncio.run(_create(report["id"], viz_id))
    observation = payload.get("observation") or {}
    text = str(observation)
    assert observation.get("success") is False or payload.get("output", {}).get("success") is False, payload
    assert "[charts]" in text, "the planner is told which check failed"
    assert asyncio.run(_completed_versions(report["id"])) == 0, "no completed version is saved"
