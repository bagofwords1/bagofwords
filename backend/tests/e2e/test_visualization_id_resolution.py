"""A miscopied visualization id reaches the chart that was meant, or a useful error.

A model copies a 36-character visualization id out of an earlier tool result
and can get a character wrong. Every tool that takes one now reads it through
one resolver (_viz_ids): an id that nearly spells exactly one visualization of
the SAME report is used; an id that names nothing is refused with the reason
and the ids that would work. A near miss never reaches another report's chart.

Render validation is stubbed (it needs Chromium); these tests are about which
visualizations a call binds and what it says when it cannot.

Run:
    cd backend
    TESTING=true uv run pytest tests/e2e/test_visualization_id_resolution.py --db=sqlite
"""
import asyncio
import uuid
from datetime import datetime, timedelta

import pytest

from app.ai.tools.implementations.create_artifact import CreateArtifactTool
from app.ai.tools.implementations.create_doc import CreateDocTool
from app.ai.tools.implementations.edit_artifact import EditArtifactTool
from app.ai.tools.implementations.read_query import ReadQueryTool
from app.dependencies import async_session_maker
from app.models.artifact import ArtifactVersion
from app.models.organization import Organization
from app.models.query import Query
from app.models.report import Report
from app.models.step import Step
from app.models.user import User
from app.models.visualization import Visualization
from app.models.widget import Widget
from sqlalchemy import select
from tests.fixtures.artifact import seed_artifact


def _run(coro):
    return asyncio.run(coro)


def _insert_char(vid):
    return vid[:13] + "a" + vid[13:]


def _drop_char(vid):
    return vid[:20] + vid[21:]


def _wrong_char(vid):
    return vid[:-1] + ("0" if vid[-1] != "0" else "1")


def _cut_off(vid):
    return vid[:18]


def _shouted_in_braces(vid):
    return "{" + vid.upper() + "}"


SLIPS = [_insert_char, _drop_char, _wrong_char, _cut_off, _shouted_in_braces]


async def _seed_viz(report_id: str, *, title="Viz", step_status="success"):
    """A visualization with one step. Direct rows: a step's stored result is
    written by an agent run, which no API call can stand in for."""
    suffix = uuid.uuid4().hex[:8]
    async with async_session_maker() as db:
        report = await db.get(Report, report_id)
        widget = Widget(title=f"W {suffix}", slug=f"w-{suffix}", report_id=report_id)
        db.add(widget)
        await db.flush()
        query = Query(
            title=title, report_id=report_id, widget_id=widget.id,
            organization_id=report.organization_id, user_id=report.user_id,
        )
        db.add(query)
        await db.flush()
        step = Step(
            title="S", slug=f"s-{suffix}", status=step_status,
            widget_id=widget.id, query_id=query.id, code="",
            data={"rows": [{"artist": "Queen", "revenue": 10}],
                  "columns": [{"field": "artist"}, {"field": "revenue"}]},
            created_at=datetime.utcnow() - timedelta(hours=1),
        )
        db.add(step)
        await db.flush()
        query.default_step_id = step.id
        viz = Visualization(
            title=title, status="success", report_id=report_id,
            query_id=query.id, view={"type": "bar_chart"},
        )
        db.add(viz)
        await db.commit()
        return str(viz.id)


async def _tool_end(tool, report_id: str, tool_input: dict):
    async with async_session_maker() as db:
        report = await db.get(Report, report_id)
        ctx = {
            "db": db, "report": report,
            "user": await db.get(User, report.user_id),
            "organization": await db.get(Organization, report.organization_id),
        }
        ends = [e async for e in tool.run_stream(tool_input, ctx) if e.type == "tool.end"]
    assert ends, "tool produced no tool.end event"
    return ends[-1].payload


async def _versions(report_id: str):
    async with async_session_maker() as db:
        rows = (await db.execute(
            select(ArtifactVersion).where(ArtifactVersion.report_id == report_id)
        )).scalars().all()
        return [{"id": str(r.id), "content": r.content or {}, "version": r.version} for r in rows]


async def _seed_page(report_id: str, viz_ids, code: str):
    async with async_session_maker() as db:
        report = await db.get(Report, report_id)
        version = await seed_artifact(
            db, report_id=report_id, user_id=report.user_id, organization_id=report.organization_id,
            content={"code": code, "visualization_ids": list(viz_ids)},
        )
        await db.commit()
        return str(version.id)


def _page(*viz_ids):
    binds = " ".join(f'const v{i} = vizById("{vid}");' for i, vid in enumerate(viz_ids))
    titles = "".join(f"<p>{{v{i} && v{i}.title}}</p>" for i in range(len(viz_ids)))
    return f"function App() {{ {binds} return <div><h1>Sales</h1>{titles}</div> }}"


@pytest.fixture
def no_render(monkeypatch):
    """Headless render validation is a separate Chromium boundary."""
    async def _clean(self, code, artifact_data, mode, runtime_ctx, **kwargs):
        yield {"code": code, "clean": True, "screenshot": None, "errors": [], "repair_attempts": 0}

    async def _no_screenshot(self, *_args, **_kwargs):
        return None, []

    monkeypatch.setattr(CreateArtifactTool, "_validate_and_repair_stream", _clean)
    monkeypatch.setattr(CreateArtifactTool, "_take_preview_screenshot", _no_screenshot)


@pytest.fixture
def workspace(create_report, create_user, login_user, whoami, test_client):
    """Two conversations of one user, each able to hold visualizations."""
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]

    def new_report():
        return create_report(title=f"r-{uuid.uuid4().hex[:6]}", user_token=token, org_id=org_id, data_sources=[])["id"]

    return {"report": new_report(), "other_report": new_report()}


def _available(payload):
    return {v["id"]: v["title"] for v in payload["observation"]["error"]["available_visualizations"]}


# ---------------------------------------------------------------- create_artifact

@pytest.mark.e2e
@pytest.mark.parametrize("slip", SLIPS, ids=lambda f: f.__name__.strip("_"))
def test_create_binds_the_visualization_a_miscopied_id_meant(workspace, no_render, slip):
    report = workspace["report"]
    meant = _run(_seed_viz(report, title="Revenue by artist"))
    _run(_seed_viz(report, title="Orders"))  # a second candidate the slip must not reach
    wrong = slip(meant)

    payload = _run(_tool_end(CreateArtifactTool(), report, {
        "prompt": "build", "title": "Sales", "mode": "page",
        "code": _page(wrong), "visualization_ids": [wrong],
    }))

    assert payload["output"].get("artifact_id"), payload
    [saved] = _run(_versions(report))
    assert saved["content"]["visualization_ids"] == [meant]
    # The same slip in the authored code is corrected with the id list.
    assert f'vizById("{meant}")' in saved["content"]["code"]
    assert f'vizById("{wrong}")' not in saved["content"]["code"]


@pytest.mark.e2e
def test_create_corrects_a_slip_that_is_only_in_the_code(workspace, no_render):
    report = workspace["report"]
    meant = _run(_seed_viz(report))
    wrong = _insert_char(meant)

    payload = _run(_tool_end(CreateArtifactTool(), report, {
        "prompt": "build", "title": "Sales", "mode": "page",
        "code": _page(wrong), "visualization_ids": [meant],
    }))

    assert payload["output"].get("artifact_id"), payload
    [saved] = _run(_versions(report))
    assert f'vizById("{meant}")' in saved["content"]["code"]
    assert f'vizById("{wrong}")' not in saved["content"]["code"]


@pytest.mark.e2e
def test_create_says_which_id_failed_and_which_would_work(workspace, no_render):
    report = workspace["report"]
    usable = _run(_seed_viz(report, title="Revenue by artist"))
    failed = _run(_seed_viz(report, title="Broken query", step_status="error"))
    elsewhere = _run(_seed_viz(workspace["other_report"], title="Someone else's"))
    unknown = str(uuid.uuid4())

    payload = _run(_tool_end(CreateArtifactTool(), report, {
        "prompt": "build", "title": "Sales", "mode": "page",
        "code": _page(unknown), "visualization_ids": [unknown],
    }))

    assert payload["output"]["success"] is False
    assert payload["output"].get("artifact_id") is None
    # The person reading the chat sees the id that failed, not a list of
    # everything that could have gone wrong.
    assert unknown in payload["output"]["error"]
    error = payload["observation"]["error"]
    assert error["type"] == "no_valid_visualizations"
    # The retry can copy an id from the error: this report's usable charts
    # only — not one whose query failed, not another report's.
    assert _available(payload) == {usable: "Revenue by artist"}
    assert failed not in str(error["available_visualizations"])
    assert elsewhere not in str(error)
    assert _run(_versions(report)) == []


@pytest.mark.e2e
@pytest.mark.parametrize("slip", [None, _insert_char, _drop_char, _wrong_char, _cut_off],
                         ids=lambda f: f.__name__.strip("_") if f else "exact")
def test_create_never_reaches_another_reports_visualization(workspace, no_render, slip):
    report = workspace["report"]
    usable = _run(_seed_viz(report, title="Revenue by artist"))
    elsewhere = _run(_seed_viz(workspace["other_report"], title="Someone else's"))
    written = slip(elsewhere) if slip else elsewhere

    payload = _run(_tool_end(CreateArtifactTool(), report, {
        "prompt": "build", "title": "Sales", "mode": "page",
        "code": _page(written), "visualization_ids": [written],
    }))

    # Its exact id is refused as before; a near miss of it is not completed
    # into it, and it is never offered as an id that would work.
    assert payload["output"]["success"] is False, payload
    assert _available(payload) == {usable: "Revenue by artist"}
    assert "Someone else's" not in str(payload)
    assert _run(_versions(report)) == []


@pytest.mark.e2e
def test_create_names_a_visualization_whose_query_failed(workspace, no_render):
    report = workspace["report"]
    usable = _run(_seed_viz(report, title="Revenue by artist"))
    failed = _run(_seed_viz(report, title="Broken query", step_status="error"))

    payload = _run(_tool_end(CreateArtifactTool(), report, {
        "prompt": "build", "title": "Sales", "mode": "page",
        "code": _page(failed), "visualization_ids": [failed],
    }))

    assert payload["output"]["success"] is False
    assert "Broken query" in payload["output"]["error"]
    assert _available(payload) == {usable: "Revenue by artist"}


@pytest.mark.e2e
def test_create_with_exact_ids_keeps_their_order_and_lists_each_once(workspace, no_render):
    report = workspace["report"]
    first = _run(_seed_viz(report, title="A"))
    second = _run(_seed_viz(report, title="B"))

    payload = _run(_tool_end(CreateArtifactTool(), report, {
        "prompt": "build", "title": "Sales", "mode": "page",
        "code": _page(second, first), "visualization_ids": [second, first, second],
    }))

    assert payload["output"].get("artifact_id"), payload
    assert "warnings" not in payload["observation"]
    [saved] = _run(_versions(report))
    assert saved["content"]["visualization_ids"] == [second, first]
    assert _page(second, first) in saved["content"]["code"]


# ------------------------------------------------------------------ edit_artifact

@pytest.mark.e2e
@pytest.mark.parametrize("slip", SLIPS, ids=lambda f: f.__name__.strip("_"))
def test_edit_adds_the_visualization_a_miscopied_id_meant(workspace, no_render, slip):
    report = workspace["report"]
    existing = _run(_seed_viz(report, title="A"))
    meant = _run(_seed_viz(report, title="B"))
    artifact_id = _run(_seed_page(report, [existing], _page(existing)))
    wrong = slip(meant)

    payload = _run(_tool_end(EditArtifactTool(), report, {
        "artifact_id": artifact_id,
        "visualization_ids": [wrong],
        "edits": [{"find": "<h1>Sales</h1>",
                   "replace": f'<h1>Sales</h1><p>{{(vizById("{wrong}") || {{}}).title}}</p>'}],
    }))

    assert payload["output"]["success"] is True, payload
    latest = max(_run(_versions(report)), key=lambda v: v["version"])
    assert latest["content"]["visualization_ids"] == [existing, meant]
    assert f'vizById("{meant}")' in latest["content"]["code"]
    assert f'vizById("{wrong}")' not in latest["content"]["code"]


@pytest.mark.e2e
def test_edit_refuses_an_id_that_names_nothing_and_saves_nothing(workspace, no_render):
    report = workspace["report"]
    existing = _run(_seed_viz(report, title="A"))
    elsewhere = _run(_seed_viz(workspace["other_report"], title="Someone else's"))
    artifact_id = _run(_seed_page(report, [existing], _page(existing)))

    for viz_id in (str(uuid.uuid4()), elsewhere):
        payload = _run(_tool_end(EditArtifactTool(), report, {
            "artifact_id": artifact_id,
            "visualization_ids": [viz_id],
            "edits": [{"find": "<h1>Sales</h1>", "replace": "<h1>Sales 2</h1>"}],
        }))
        assert payload["output"]["success"] is False, payload
        assert payload["observation"]["error"]["type"] == "visualization_not_found"
        assert viz_id in payload["output"]["error"]
        assert _available(payload) == {existing: "A"}

    [only] = _run(_versions(report))
    assert only["content"]["visualization_ids"] == [existing]


@pytest.mark.e2e
def test_edit_removes_the_visualization_a_miscopied_id_meant(workspace, no_render):
    report = workspace["report"]
    kept = _run(_seed_viz(report, title="A"))
    gone = _run(_seed_viz(report, title="B"))
    artifact_id = _run(_seed_page(report, [kept, gone], _page(kept, gone)))

    payload = _run(_tool_end(EditArtifactTool(), report, {
        "artifact_id": artifact_id,
        "remove_visualization_ids": [_drop_char(gone)],
        "edits": [
            {"find": f' const v1 = vizById("{gone}");', "replace": ""},
            {"find": "<p>{v1 && v1.title}</p>", "replace": ""},
        ],
    }))

    assert payload["output"]["success"] is True, payload
    latest = max(_run(_versions(report)), key=lambda v: v["version"])
    assert latest["content"]["visualization_ids"] == [kept]


# ----------------------------------------------------------------------- docs

@pytest.mark.e2e
@pytest.mark.parametrize("slip", [_insert_char, _drop_char, _wrong_char, _cut_off],
                         ids=lambda f: f.__name__.strip("_"))
def test_a_document_embeds_the_visualization_a_miscopied_id_meant(workspace, slip):
    report = workspace["report"]
    meant = _run(_seed_viz(report, title="Revenue by artist"))
    _run(_seed_viz(report, title="Orders"))

    payload = _run(_tool_end(CreateDocTool(), report, {
        "title": "Findings",
        "markdown": f"# Findings\n\nRevenue is concentrated.\n\n{{{{viz:{slip(meant)}}}}}\n",
    }))

    assert payload["output"].get("success") is not False, payload
    [saved] = _run(_versions(report))
    assert saved["content"]["visualization_ids"] == [meant]
    assert f"{{{{viz:{meant}}}}}" in saved["content"]["markdown"]


@pytest.mark.e2e
def test_a_document_with_an_id_that_names_nothing_lists_what_would_work(workspace):
    report = workspace["report"]
    usable = _run(_seed_viz(report, title="Revenue by artist"))
    unknown = str(uuid.uuid4())

    payload = _run(_tool_end(CreateDocTool(), report, {
        "title": "Findings", "markdown": f"# Findings\n\n{{{{viz:{unknown}}}}}\n",
    }))

    assert payload["output"]["success"] is False
    assert unknown in payload["output"]["error"]
    assert _available(payload) == {usable: "Revenue by artist"}
    assert _run(_versions(report)) == []


# ----------------------------------------------------------------- read_query

@pytest.mark.e2e
@pytest.mark.parametrize("slip", SLIPS, ids=lambda f: f.__name__.strip("_"))
def test_read_query_reads_the_visualization_a_miscopied_id_meant(workspace, slip):
    report = workspace["report"]
    meant = _run(_seed_viz(report, title="Revenue by artist"))
    _run(_seed_viz(report, title="Orders"))

    payload = _run(_tool_end(ReadQueryTool(), report, {"visualization_ids": [slip(meant)]}))

    [result] = payload["output"]["results"]
    assert not result.get("error"), result
    assert result["visualization_id"] == meant


@pytest.mark.e2e
def test_read_query_with_an_id_that_names_nothing_lists_what_would_work(workspace):
    report = workspace["report"]
    usable = _run(_seed_viz(report, title="Revenue by artist"))
    elsewhere = _run(_seed_viz(workspace["other_report"], title="Someone else's"))

    payload = _run(_tool_end(ReadQueryTool(), report, {"visualization_ids": [_wrong_char(elsewhere)]}))

    [result] = payload["output"]["results"]
    assert usable in result["error"] and "Revenue by artist" in result["error"]
    assert elsewhere not in result["error"]
