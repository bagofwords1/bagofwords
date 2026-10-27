"""read_artifact: the code the agent asked for reaches the model.

The real tool and transcript must deliver valid bounded JSON, exact source
windows with usable continuations, and optional profiles that cannot crowd out
the requested source. Windowed reads omit profiles unless explicitly requested.

Run:
    cd backend
    uv run pytest tests/e2e/test_read_artifact_model_view.py -v
"""
import asyncio
import json
import uuid

import pytest

from app.ai.context.parts import ToolCallPart
from app.ai.context.result_parts import build_result_part
from app.ai.context.transcript import Transcript
from app.ai.tools.implementations.read_artifact import ReadArtifactTool
from app.dependencies import async_session_maker
from app.models.organization import Organization
from app.models.query import Query
from app.models.report import Report
from app.models.step import Step
from app.models.user import User
from app.models.visualization import Visualization
from app.models.widget import Widget
from tests.fixtures.artifact import seed_artifact

SLICER = '<option value="">כל המותגים</option>'
COLUMNS = ["מותג", "קטגוריה", "שם לקוח", "עיר", "נציג", "מכירות", "יעד"]


def _rows(n_rows, seed):
    """Hebrew-heavy rows: escaped as \\uXXXX in the JSON the model receives."""
    return [
        {
            "מותג": f"מותג לדוגמה מספר {seed}-{i}",
            "קטגוריה": f"קטגוריית מוצרים לדוגמה {i}",
            "שם לקוח": f"לקוח לדוגמה סניף מרכזי - {seed}{i}",
            "עיר": "עיר לדוגמה בדרום הארץ",
            "נציג": f"נציג מכירות אזור לדוגמה {i}",
            "מכירות": 1000.5 * (i + 1),
            "יעד": 990.25 * (i + 1),
        }
        for i in range(n_rows)
    ]


def _dashboard_code():
    """A page whose slicers sit far into one long JSX line, the way the agent writes it."""
    head = "\n".join(f"function Part{i}(){{return <div className=\"part-{i}\">חלק {i}</div>}}" for i in range(40))
    filler = "".join(f"<div className=\"cell-{i}\">תא {i}</div>" for i in range(90))
    body = f"return <div className=\"app\">{filler}<select aria-label=\"מותג\">{SLICER}</select></div>"
    return f"{head}\n{body}\n"


async def _seed_dashboard(report_id, n_vizs, **version_fields):
    """Artifact + its visualizations, each with a successful step holding rows.

    Visualizations, queries and steps come from the agent's create_data flow
    in production and have no public CRUD API, so the graph is seeded directly
    (same approach as tests/e2e/test_report_refresh_on_view.py).
    """
    async with async_session_maker() as db:
        report = await db.get(Report, report_id)
        viz_ids = []
        for n in range(n_vizs):
            suffix = uuid.uuid4().hex[:8]
            widget = Widget(title=f"W {suffix}", slug=f"w-{suffix}", report_id=report_id)
            db.add(widget)
            await db.flush()
            query = Query(title=f"Q {n}", report_id=report_id, widget_id=widget.id,
                          organization_id=report.organization_id, user_id=report.user_id)
            db.add(query)
            await db.flush()
            rows = _rows(8, n)
            step = Step(title="Default", slug=f"default-{suffix}", status="success",
                        widget_id=widget.id, query_id=query.id, code="def generate_df(ds_clients, excel_files): ...",
                        data={"rows": rows, "columns": [{"field": c, "headerName": c} for c in COLUMNS]})
            db.add(step)
            await db.flush()
            query.default_step_id = step.id
            viz = Visualization(title=f"גרף {n}", status="success", report_id=report_id,
                                query_id=query.id, view={"version": "v2", "view": {"type": "table"}})
            db.add(viz)
            await db.flush()
            viz_ids.append(str(viz.id))
        version = await seed_artifact(
            db, report_id=report_id, user_id=report.user_id, organization_id=report.organization_id,
            mode="page", title="Executive dashboard",
            content={"code": version_fields.pop("source_code", _dashboard_code()), "visualization_ids": viz_ids},
            **version_fields,
        )
        await db.commit()
        return str(version.id)


async def _read(report_id, user_id, tool_input):
    async with async_session_maker() as db:
        report = await db.get(Report, report_id)
        ctx = {"db": db, "report": report, "user": await db.get(User, user_id),
               "organization": await db.get(Organization, report.organization_id)}
        events = [e async for e in ReadArtifactTool().run_stream(tool_input, ctx)]
    ends = [e for e in events if e.type == "tool.end"]
    assert ends, [e.type for e in events]
    return ends[-1].payload["observation"]


def _model_view(observation):
    """The tool_result text the planner model receives for this observation."""
    t = Transcript()
    t.add_user_text("edit the slicers")
    t.add_assistant_step(calls=[ToolCallPart(id="c1", tool_name="read_artifact", args={})])
    t.add_tool_results([build_result_part(call_id="c1", tool_name="read_artifact", observation=observation)])
    for message in t.to_model_messages():
        for block in message.content if isinstance(message.content, list) else []:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                return block["content"] if isinstance(block["content"], str) else json.dumps(block["content"])
    pytest.fail("no tool_result rendered")


def _escaped(text):
    return json.dumps(text)[1:-1]


@pytest.fixture
def dashboard(create_user, login_user, whoami, create_report):
    def _make(n_vizs, **version_fields):
        user = create_user()
        token = login_user(user["email"], user["password"])
        me = whoami(token)
        org_id = me["organizations"][0]["id"]
        report = create_report(title="Big dashboard", user_token=token, org_id=org_id, data_sources=[])
        artifact_id = asyncio.run(_seed_dashboard(report["id"], n_vizs, **version_fields))
        return report["id"], me["id"], artifact_id
    return _make


@pytest.mark.e2e
@pytest.mark.parametrize("n_vizs", [24, 40])
@pytest.mark.parametrize("read", ["full", "range", "grep"])
def test_requested_code_reaches_the_model_when_profiles_overflow(dashboard, n_vizs, read):
    report_id, user_id, artifact_id = dashboard(n_vizs)
    code = _dashboard_code()
    slicer_line = next(i for i, line in enumerate(code.split("\n"), 1) if SLICER in line)
    tool_input = {
        "full": {"artifact_id": artifact_id},
        "range": {"artifact_id": artifact_id, "offset": slicer_line, "limit": 1},
        "grep": {"artifact_id": artifact_id, "grep_pattern": "כל המותגים"},
    }[read]

    observation = asyncio.run(_read(report_id, user_id, tool_input))
    if read in ("range", "grep"):
        assert "visualization_profiles" not in observation
    sent = _model_view(observation)

    # Optional profiles must not turn the model response into truncated JSON.
    assert "[truncated at" not in sent
    json.loads(sent)
    payload = observation["matches"] if read == "grep" else observation["code"]
    payload_key = "matches" if read == "grep" else "code"
    assert f'"{payload_key}": ' in sent
    if read == "full":
        assert _escaped(code) in sent  # the whole code, not a prefix
    else:
        assert _escaped(SLICER) in sent
        assert payload  # and the tool found it
    # The small fields later calls need (edit_artifact, manage_artifact_resources)
    # survive the cut too.
    for key in ("visualization_ids", "version", "resource_artifact_id", "resources_enabled"):
        assert f'"{key}": ' in sent, key


@pytest.mark.e2e
def test_render_errors_reach_the_model_when_profiles_overflow(dashboard):
    errors = [{"message": "ReferenceError: marketParams is not defined", "line": 117}]
    report_id, user_id, artifact_id = dashboard(24, render_errors=errors)
    observation = asyncio.run(_read(report_id, user_id, {"artifact_id": artifact_id, "load_screenshot": True}))
    sent = _model_view(observation)
    assert "[truncated at" not in sent
    assert _escaped(errors[0]["message"]) in sent


@pytest.mark.e2e
def test_small_dashboard_reaches_the_model_whole(dashboard):
    report_id, user_id, artifact_id = dashboard(2)
    observation = asyncio.run(_read(report_id, user_id, {"artifact_id": artifact_id}))
    sent = _model_view(observation)
    assert "[truncated at" not in sent
    assert _escaped(_dashboard_code()) in sent
    assert json.loads(sent)["visualization_profiles"] == json.loads(json.dumps(observation["visualization_profiles"], default=str))


@pytest.mark.e2e
@pytest.mark.parametrize("code,first_read", [
    ("שלום" * 10_000, {}),  # raw full-read size fits; JSON-escaped size does not
    ("漢字" * 60_000, {"offset": 1, "limit": 1}),  # one oversized line
    ("\n".join(["مرحبا" * 500] * 30), {"offset": 1, "limit": 30}),
], ids=["hebrew-full", "cjk-long-line", "arabic-multiline"])
def test_source_continuations_cover_requested_source_verbatim(dashboard, code, first_read):
    report_id, user_id, artifact_id = dashboard(0, source_code=code)
    args = {"artifact_id": artifact_id, **first_read}
    cursor = 0
    for _ in range(40):
        observation = asyncio.run(_read(report_id, user_id, args))
        sent = json.loads(_model_view(observation))
        assert len(_model_view(observation)) < 100_000
        assert sent["artifact_id"] == artifact_id
        assert sent["version"] > 0
        start = sum(len(line) + 1 for line in code.split("\n")[:args.get("offset", 1) - 1]) + args.get("character_offset", 0)
        assert start == cursor
        part = sent["code"]
        assert part and code[start:start + len(part)] == part
        cursor += len(part)
        if not sent.get("next_read"):
            break
        args = sent["next_read"]
    else:
        pytest.fail("continuation failed to finish")
    assert cursor == len(code)


@pytest.mark.e2e
@pytest.mark.parametrize("read", [{"offset": 1, "limit": 1}, {"grep_pattern": "Part"}])
def test_windowed_reads_offer_explicit_profiles(dashboard, read):
    report_id, user_id, artifact_id = dashboard(2)
    args = {"artifact_id": artifact_id, **read}
    default = asyncio.run(_read(report_id, user_id, args))
    assert "visualization_profiles" not in default
    requested = asyncio.run(_read(report_id, user_id, {**args, "include_visualization_profiles": True}))
    assert len(json.loads(_model_view(requested))["visualization_profiles"]) == 2


@pytest.mark.e2e
def test_grep_continuation_preserves_every_match_and_bounds_context(dashboard):
    code = "\n".join("שלום" * 150 + f"needle{i}" for i in range(160))
    report_id, user_id, artifact_id = dashboard(0, source_code=code)
    args = {"artifact_id": artifact_id, "grep_pattern": "needle", "max_matches": 200}
    lines = []
    for _ in range(20):
        sent = json.loads(_model_view(asyncio.run(_read(report_id, user_id, args))))
        lines.extend(m["line_no"] for m in sent["matches"])
        if not sent.get("next_read"):
            break
        args = sent["next_read"]
    assert lines == list(range(1, 161))


@pytest.mark.e2e
def test_oversized_grep_context_is_explicitly_omitted(dashboard):
    code = "漢" * 40_000 + "\nneedle\n" + "ש" * 40_000
    report_id, user_id, artifact_id = dashboard(0, source_code=code)
    sent = json.loads(_model_view(asyncio.run(_read(report_id, user_id, {
        "artifact_id": artifact_id, "grep_pattern": "needle", "before": 1, "after": 1}))))
    assert sent["matches"][0]["line"] == "needle"
    assert sent["matches"][0]["context_omitted"]
    assert "context_note" in sent
