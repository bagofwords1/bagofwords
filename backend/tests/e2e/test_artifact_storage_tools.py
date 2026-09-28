"""Agent tools and artifact routes: the `storage` declaration end to end.

Contract (docs/design/artifact-app-persistence.md, "App changes"):
- create_artifact / edit_artifact accept an optional `storage` declaration;
  it is persisted as `content["storage"]` and omitted when absent.
- Mechanical gates run before anything is persisted: an invalid declaration,
  an undeclared or non-literal `useCollection` call, or a new required field
  without a default in an existing collection rejects the call with no row.
- The declaration is never silently dropped: edit (mechanical, legacy, MCP),
  duplicate, add-visualization, and a rebuild without `storage` carry it.
- read_artifact and the planner's <current_artifact> show it.
- Artifacts without storage keep exactly the content keys they had before and
  never touch `app_records` (the dev database may not have that table).

Playwright is the boundary here: render validation and the parse gate are
stubbed (as in test_create_artifact_replaces.py); LLM providers are faked for
the legacy and MCP edit paths, which generate diffs with a model.

Run:
    cd backend
    TESTING=true uv run pytest tests/e2e/test_artifact_storage_tools.py --db=sqlite
"""
import asyncio
import copy
import json
import uuid
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import event, func, select

from app.dependencies import async_session_maker
from app.models.artifact import ArtifactVersion
from app.models.organization import Organization
from app.models.query import Query
from app.models.report import Report
from app.models.step import Step
from app.models.user import User
from app.models.visualization import Visualization
from app.models.widget import Widget

pytestmark = pytest.mark.e2e


def _run(coro):
    return asyncio.run(coro)


NOTES_DECL = {
    "collections": {
        "notes": {
            "scope": "shared", "create": "members", "modify": "author",
            "fields": {"country": {"type": "string", "required": True}, "text": {"type": "string", "max_length": 500}},
        },
    },
}
PREFS = {"scope": "per_user", "fields": {"genre": {"type": "string", "default": None}}}


def _page_code(viz_id: str, *collections: str, label: str = "Notes") -> str:
    uses = "".join(f'  const c{i} = useCollection("{name}");\n' for i, name in enumerate(collections))
    return (
        '<script type="text/babel">\n'
        "function App() {\n"
        f'  const v = vizById("{viz_id}");\n'
        f"{uses}"
        f'  return <div className="p-4">{{v ? v.title : "none"}} {label}</div>;\n'
        "}\n"
        "</script>"
    )


def _make_report(create_report, create_user, login_user, whoami, title="Storage"):
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    report = create_report(title=title, user_token=token, org_id=org_id, data_sources=[])
    return report, token, org_id


def _headers(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


async def _seed_viz(report_id: str) -> str:
    # Direct DB seeding: no API creates a successful query step without an
    # LLM run (same approach as test_create_artifact_replaces.py).
    suffix = uuid.uuid4().hex[:8]
    async with async_session_maker() as db:
        report = await db.get(Report, report_id)
        widget = Widget(title=f"W {suffix}", slug=f"w-{suffix}", report_id=report_id)
        db.add(widget)
        await db.flush()
        query = Query(
            title=f"Q {suffix}", report_id=report_id, widget_id=widget.id,
            organization_id=report.organization_id, user_id=report.user_id,
        )
        db.add(query)
        await db.flush()
        step = Step(
            title="S", slug=f"s-{suffix}", status="success",
            widget_id=widget.id, query_id=query.id, code="",
            data={"rows": [{"country": "PT", "revenue": 10}],
                  "columns": [{"field": "country"}, {"field": "revenue"}]},
            data_model={"type": "table"},
            created_at=datetime.utcnow() - timedelta(hours=1),
        )
        db.add(step)
        await db.flush()
        query.default_step_id = step.id
        viz = Visualization(
            title=f"Viz {suffix}", status="success", report_id=report_id,
            query_id=query.id, view={"type": "table"},
        )
        db.add(viz)
        await db.commit()
        return str(viz.id)


@pytest.fixture()
def stub_render(monkeypatch):
    """Skip Playwright: authored code parses and renders cleanly."""
    from app.ai.tools.implementations import _artifact_parse
    from app.ai.tools.implementations.create_artifact import CreateArtifactTool

    async def _clean(self, code, artifact_data, mode, runtime_ctx, **kwargs):
        yield {"code": code, "clean": True, "screenshot": None, "errors": [], "repair_attempts": 0}

    async def _no_screenshot(self, html, *args, **kwargs):
        return None, []

    async def _parses(code):
        return None

    monkeypatch.setattr(CreateArtifactTool, "_validate_and_repair_stream", _clean)
    monkeypatch.setattr(CreateArtifactTool, "_take_preview_screenshot", _no_screenshot)
    monkeypatch.setattr(_artifact_parse, "parse_check_page_code", _parses)


class _SqlLog:
    def __init__(self):
        self.statements = []

    def __call__(self, _conn, _cursor, statement, _params, _context, _executemany):
        self.statements.append(statement.lower())

    def mentions(self, needle: str) -> bool:
        return any(needle in s for s in self.statements)


async def _runtime_ctx(db, report_id):
    report = await db.get(Report, report_id)
    user = await db.get(User, report.user_id)
    organization = await db.get(Organization, report.organization_id)
    return {"db": db, "report": report, "user": user, "organization": organization}


async def _run_tool(tool, report_id: str, tool_input: dict, sql: _SqlLog = None):
    async with async_session_maker() as db:
        runtime_ctx = await _runtime_ctx(db, report_id)
        engine = db.bind.sync_engine
        if sql is not None:
            event.listen(engine, "before_cursor_execute", sql)
        try:
            events = [evt async for evt in tool.run_stream(tool_input, runtime_ctx)]
        finally:
            if sql is not None:
                event.remove(engine, "before_cursor_execute", sql)
    ends = [e for e in events if e.type == "tool.end"]
    assert ends, f"no tool.end event in {[e.type for e in events]}"
    return ends[-1].payload


def _create(report_id, tool_input, sql=None):
    from app.ai.tools.implementations.create_artifact import CreateArtifactTool
    return _run(_run_tool(CreateArtifactTool(), report_id, {"prompt": "build", "mode": "page", **tool_input}, sql))


def _edit(report_id, tool_input, sql=None):
    from app.ai.tools.implementations.edit_artifact import EditArtifactTool
    return _run(_run_tool(EditArtifactTool(), report_id, tool_input, sql))


async def _versions(report_id: str):
    async with async_session_maker() as db:
        rows = (await db.execute(
            select(ArtifactVersion).where(ArtifactVersion.report_id == report_id)
            .order_by(ArtifactVersion.created_at)
        )).scalars().all()
        return [{"id": str(r.id), "artifact_id": str(r.artifact_id), "version": r.version,
                 "status": r.status, "content": copy.deepcopy(r.content)} for r in rows]


async def _content(version_id: str) -> dict:
    async with async_session_maker() as db:
        row = await db.get(ArtifactVersion, version_id)
        return copy.deepcopy(row.content)


async def _version_count(report_id: str) -> int:
    async with async_session_maker() as db:
        return (await db.execute(
            select(func.count(ArtifactVersion.id)).where(ArtifactVersion.report_id == report_id)
        )).scalar_one()


@pytest.fixture()
def page(create_report, create_user, login_user, whoami, test_client, stub_render):
    report, token, org_id = _make_report(create_report, create_user, login_user, whoami)
    viz_id = _run(_seed_viz(report["id"]))
    return SimpleNamespace(report_id=report["id"], token=token, org_id=org_id, viz_id=viz_id,
                           headers=_headers(token, org_id))


def _create_with_notes(page, **extra):
    result = _create(page.report_id, {
        "title": "Notes", "code": _page_code(page.viz_id, "notes"),
        "visualization_ids": [page.viz_id], "storage": NOTES_DECL, **extra,
    })
    assert result["output"].get("success", True) is not False, result["observation"]
    return result["output"]["artifact_id"]


# ---------------------------------------------------------------------------
# create_artifact
# ---------------------------------------------------------------------------

def test_create_persists_the_declaration(page):
    version_id = _create_with_notes(page)
    content = _run(_content(version_id))
    assert content["storage"] == NOTES_DECL
    assert [v["status"] for v in _run(_versions(page.report_id))] == ["completed"]


@pytest.mark.parametrize("case", ["undeclared", "declared_other", "non_literal"])
def test_create_rejects_unbacked_use_collection_and_persists_nothing(page, case):
    code = {
        "undeclared": _page_code(page.viz_id, "notes"),
        "declared_other": _page_code(page.viz_id, "notes"),
        "non_literal": _page_code(page.viz_id).replace('const v =', 'const name = "notes"; const n = useCollection(name);\n  const v ='),
    }[case]
    storage = {"collections": {"prefs": PREFS}} if case == "declared_other" else (NOTES_DECL if case == "non_literal" else None)
    tool_input = {"code": code, "visualization_ids": [page.viz_id]}
    if storage is not None:
        tool_input["storage"] = storage
    result = _create(page.report_id, tool_input)
    assert result["output"]["success"] is False
    assert result["observation"]["error"]["type"] == "storage_errors"
    assert all(e.startswith("[storage]") for e in result["observation"]["error"]["errors"])
    assert _run(_version_count(page.report_id)) == 0, "a rejected create must not persist a row"


@pytest.mark.parametrize("bad", [
    {"collections": {"notes": {**NOTES_DECL["collections"]["notes"], "modify": "anyone"}}},
    {"collections": {"notes": {**NOTES_DECL["collections"]["notes"], "fields": {"x": {"type": "text"}}}}},
    {"collections": {"notes": {**NOTES_DECL["collections"]["notes"], "public": True}}},
])
def test_create_rejects_invalid_declaration_and_persists_nothing(page, bad):
    result = _create(page.report_id, {"code": _page_code(page.viz_id, "notes"),
                                      "visualization_ids": [page.viz_id], "storage": bad})
    assert result["output"]["success"] is False
    assert result["observation"]["error"]["type"] == "storage_errors"
    assert _run(_version_count(page.report_id)) == 0


@pytest.mark.parametrize("variant", ["slides", "no_code"])
def test_create_rejects_storage_on_slides_or_generated_code(page, variant):
    tool_input = {"visualization_ids": [page.viz_id], "storage": NOTES_DECL}
    if variant == "slides":
        tool_input.update(mode="slides", code="prs.save(_pptx_output_path)")
    result = _create(page.report_id, tool_input)
    assert result["output"]["success"] is False
    assert result["observation"]["error"]["type"] == "storage_unsupported"
    assert _run(_version_count(page.report_id)) == 0


def test_rebuild_without_storage_carries_the_effective_declaration(page):
    v1 = _create_with_notes(page)
    rebuilt = _create(page.report_id, {
        "code": _page_code(page.viz_id, "notes", label="Rebuilt"),
        "visualization_ids": [page.viz_id], "replaces_artifact_id": v1,
    })
    assert rebuilt["output"].get("success", True) is not False, rebuilt["observation"]
    content = _run(_content(rebuilt["output"]["artifact_id"]))
    assert content["storage"] == NOTES_DECL


def test_rebuild_with_empty_declaration_removes_storage(page, monkeypatch):
    # Removing a collection asks the user (covered in
    # test_artifact_storage_confirmation.py); approve it here.
    from app.ai.tools.implementations import _artifact_storage

    async def _approve(runtime_ctx, *, tool_name, changes, started_monotonic=None):
        assert [c.kind for c in changes] == ["collection_removed"]
        yield {"approved": True, "reason": None}

    monkeypatch.setattr(_artifact_storage, "confirm_storage_changes", _approve)
    v1 = _create_with_notes(page)
    rebuilt = _create(page.report_id, {
        "code": _page_code(page.viz_id), "visualization_ids": [page.viz_id],
        "replaces_artifact_id": v1, "storage": {"collections": {}},
    })
    assert rebuilt["output"].get("success", True) is not False, rebuilt["observation"]
    assert _run(_content(rebuilt["output"]["artifact_id"]))["storage"] == {"collections": {}}


def test_rebuild_rejects_new_required_field_without_default(page):
    v1 = _create_with_notes(page)
    storage = copy.deepcopy(NOTES_DECL)
    storage["collections"]["notes"]["fields"]["rating"] = {"type": "number", "required": True}
    before = _run(_version_count(page.report_id))
    result = _create(page.report_id, {
        "code": _page_code(page.viz_id, "notes"), "visualization_ids": [page.viz_id],
        "replaces_artifact_id": v1, "storage": storage,
    })
    assert result["output"]["success"] is False
    assert any("rating" in e for e in result["observation"]["error"]["errors"])
    assert _run(_version_count(page.report_id)) == before


def test_new_identity_note_only_when_another_artifact_has_storage(page):
    first = _create(page.report_id, {"code": _page_code(page.viz_id, "notes"),
                                     "visualization_ids": [page.viz_id], "storage": NOTES_DECL})
    assert "replaces_artifact_id" not in first["observation"]["summary"], "no other storage artifact yet"

    plain = _create(page.report_id, {"code": _page_code(page.viz_id), "visualization_ids": [page.viz_id]})
    assert "replaces_artifact_id" not in plain["observation"]["summary"], "no storage given: no note"

    second = _create(page.report_id, {"code": _page_code(page.viz_id, "notes"),
                                      "visualization_ids": [page.viz_id], "storage": NOTES_DECL})
    summary = second["observation"]["summary"]
    assert "replaces_artifact_id" in summary and first["output"]["artifact_id"] in summary

    rebuild = _create(page.report_id, {"code": _page_code(page.viz_id, "notes"), "visualization_ids": [page.viz_id],
                                       "storage": NOTES_DECL, "replaces_artifact_id": first["output"]["artifact_id"]})
    assert "replaces_artifact_id" not in rebuild["observation"]["summary"], "a rebuild keeps its identity: no note"


# ---------------------------------------------------------------------------
# edit_artifact (mechanical)
# ---------------------------------------------------------------------------

def test_edit_without_storage_carries_it(page):
    v1 = _create_with_notes(page)
    result = _edit(page.report_id, {"artifact_id": v1, "edits": [{"find": "Notes</div>", "replace": "Team notes</div>"}]})
    assert result["output"]["success"] is True, result["observation"]
    content = _run(_content(result["output"]["artifact_id"]))
    assert "Team notes" in content["code"]
    assert content["storage"] == NOTES_DECL


def test_edit_with_storage_replaces_it(page):
    v1 = _create_with_notes(page)
    storage = copy.deepcopy(NOTES_DECL)
    storage["collections"]["prefs"] = PREFS
    result = _edit(page.report_id, {
        "artifact_id": v1, "storage": storage,
        "edits": [{"find": 'const c0 = useCollection("notes");', "replace": 'const c0 = useCollection("notes");\n  const p = useCollection("prefs");'}],
    })
    assert result["output"]["success"] is True, result["observation"]
    assert _run(_content(result["output"]["artifact_id"]))["storage"] == storage


def test_storage_only_edit_is_allowed(page):
    v1 = _create_with_notes(page)
    storage = copy.deepcopy(NOTES_DECL)
    storage["collections"]["notes"]["fields"]["pinned"] = {"type": "boolean", "default": False}
    result = _edit(page.report_id, {"artifact_id": v1, "storage": storage})
    assert result["output"]["success"] is True, result["observation"]
    content = _run(_content(result["output"]["artifact_id"]))
    assert content["storage"] == storage
    assert content["code"] == _run(_content(v1))["code"]


@pytest.mark.parametrize("case", ["undeclared_use", "invalid_storage", "new_required_field"])
def test_edit_gate_failures_persist_nothing(page, case):
    v1 = _create_with_notes(page)
    tool_input = {"artifact_id": v1}
    if case == "undeclared_use":
        tool_input["edits"] = [{"find": 'const c0 = useCollection("notes");', "replace": 'const c0 = useCollection("notes"); const x = useCollection("votes");'}]
    elif case == "invalid_storage":
        tool_input["storage"] = {"collections": {"notes": {"scope": "global", "fields": {"a": {"type": "string"}}}}}
    else:
        storage = copy.deepcopy(NOTES_DECL)
        storage["collections"]["notes"]["fields"]["rating"] = {"type": "number", "required": True}
        tool_input["storage"] = storage
    before = _run(_version_count(page.report_id))
    result = _edit(page.report_id, tool_input)
    assert result["output"]["success"] is False
    errors = result["observation"]["error"]["errors"]
    assert errors and all(e.startswith("[storage]") for e in errors)
    assert _run(_version_count(page.report_id)) == before, "a rejected edit must not persist"


def test_edit_rejects_storage_on_slides(page):
    from tests.fixtures.artifact import seed_artifact

    async def _seed():
        async with async_session_maker() as db:
            report = await db.get(Report, page.report_id)
            row = await seed_artifact(db, report_id=page.report_id, user_id=report.user_id,
                                      organization_id=report.organization_id, mode="slides",
                                      content={"code": "prs.save(_pptx_output_path)", "visualization_ids": []})
            await db.commit()
            return str(row.id)

    slides_id = _run(_seed())
    before = _run(_version_count(page.report_id))
    result = _edit(page.report_id, {"artifact_id": slides_id, "storage": NOTES_DECL})
    assert result["output"]["success"] is False
    assert result["observation"]["error"]["type"] == "storage_unsupported"
    assert _run(_version_count(page.report_id)) == before


# ---------------------------------------------------------------------------
# Other version-minting paths carry the declaration
# ---------------------------------------------------------------------------

class _FakeModel:
    supports_vision = False


def _diff(find: str, replace: str) -> str:
    from app.ai.tools.implementations.edit_artifact_legacy import DIVIDER_MARKER, REPLACE_MARKER, SEARCH_MARKER
    return f"{SEARCH_MARKER}\n{find}\n{DIVIDER_MARKER}\n{replace}\n{REPLACE_MARKER}\n"


def test_legacy_edit_carries_storage(page, monkeypatch):
    from app.ai.llm.types import TextDeltaEvent
    from app.ai.tools.implementations import edit_artifact_legacy

    diff = _diff("Notes</div>", "Legacy notes</div>")

    class _FakeLLM:  # LLM provider boundary
        def __init__(self, *args, **kwargs):
            pass

        async def inference_stream_v2(self, *args, **kwargs):
            yield TextDeltaEvent(text=diff)

    monkeypatch.setattr(edit_artifact_legacy, "LLM", _FakeLLM)
    v1 = _create_with_notes(page)

    async def _go():
        async with async_session_maker() as db:
            ctx = await _runtime_ctx(db, page.report_id)
            ctx["model"] = _FakeModel()
            events = [e async for e in edit_artifact_legacy.EditArtifactTool().run_stream(
                {"artifact_id": v1, "edit_prompt": "rename the heading"}, ctx)]
        return [e for e in events if e.type == "tool.end"][-1].payload

    result = _run(_go())
    assert result["output"].get("success", True) is not False, result["observation"]
    content = _run(_content(result["output"]["artifact_id"]))
    assert "Legacy notes" in content["code"]
    assert content["storage"] == NOTES_DECL


def test_mcp_edit_carries_storage(page, monkeypatch):
    from app.ai.tools.mcp import edit_artifact as mcp_edit

    diff = _diff("Notes</div>", "MCP notes</div>")

    class _FakeLLM:  # LLM provider boundary
        def __init__(self, *args, **kwargs):
            pass

        def inference(self, *args, **kwargs):
            return diff

    async def _fake_rich_context(**kwargs):
        # Stands in for the org's model configuration + retrieval context (LLM boundary).
        return SimpleNamespace(model=_FakeModel(), instructions_text="", org_settings=None)

    monkeypatch.setattr(mcp_edit, "LLM", _FakeLLM)
    monkeypatch.setattr(mcp_edit, "build_rich_context", _fake_rich_context)
    v1 = _create_with_notes(page)

    async def _go():
        async with async_session_maker() as db:
            ctx = await _runtime_ctx(db, page.report_id)
            return await mcp_edit.EditArtifactMCPTool().execute(
                {"report_id": page.report_id, "artifact_id": v1, "edit_instruction": "rename the heading"},
                db, ctx["user"], ctx["organization"],
            )

    result = _run(_go())
    assert result["success"] is True, result
    content = _run(_content(result["artifact_id"]))
    assert "MCP notes" in content["code"]
    assert content["storage"] == NOTES_DECL


def test_duplicate_carries_storage(page, test_client):
    v1 = _create_with_notes(page)
    res = test_client.post(f"/api/artifacts/{v1}/duplicate", headers=page.headers)
    assert res.status_code == 200, res.json()
    assert res.json()["content"]["storage"] == NOTES_DECL


def test_add_visualization_carries_storage(page, test_client):
    first = test_client.post(f"/api/artifacts/report/{page.report_id}/add-visualization",
                             json={"visualization_id": page.viz_id}, headers=page.headers)
    assert first.status_code == 200, first.json()
    artifact = first.json()
    patched = test_client.patch(f"/api/artifacts/{artifact['id']}", headers=page.headers,
                                json={"content": {**artifact["content"], "storage": NOTES_DECL}})
    assert patched.status_code == 200, patched.json()

    second_viz = _run(_seed_viz(page.report_id))
    added = test_client.post(f"/api/artifacts/report/{page.report_id}/add-visualization",
                             json={"visualization_id": second_viz, "artifact_id": artifact["id"]},
                             headers=page.headers)
    assert added.status_code == 200, added.json()
    assert added.json()["version"] == artifact["version"] + 1
    assert added.json()["content"]["storage"] == NOTES_DECL


# ---------------------------------------------------------------------------
# REST ingress validation
# ---------------------------------------------------------------------------

def test_artifact_routes_validate_storage(page, test_client):
    base = {"report_id": page.report_id, "mode": "page", "title": "Direct",
            "content": {"code": _page_code(page.viz_id, "notes"), "visualization_ids": [], "runtime_version": 11}}
    bad = {"collections": {"notes": {"scope": "shared", "fields": {"a": {"type": "string"}}}}}

    rejected = test_client.post("/api/artifacts", headers=page.headers,
                                json={**base, "content": {**base["content"], "storage": bad}})
    assert rejected.status_code == 422

    created = test_client.post("/api/artifacts", headers=page.headers,
                               json={**base, "content": {**base["content"], "storage": NOTES_DECL}})
    assert created.status_code == 200, created.json()
    assert created.json()["content"]["storage"] == NOTES_DECL

    plain = test_client.post("/api/artifacts", headers=page.headers, json=base)
    assert plain.status_code == 200, "content without storage is unaffected"

    patched = test_client.patch(f"/api/artifacts/{created.json()['id']}", headers=page.headers,
                                json={"content": {**base["content"], "storage": bad}})
    assert patched.status_code == 422


# ---------------------------------------------------------------------------
# read_artifact and planner context
# ---------------------------------------------------------------------------

def test_read_artifact_returns_storage(page):
    from app.ai.tools.implementations.read_artifact import ReadArtifactTool
    with_storage = _create_with_notes(page)
    without = _create(page.report_id, {"code": _page_code(page.viz_id), "visualization_ids": [page.viz_id]})

    read = _run(_run_tool(ReadArtifactTool(), page.report_id, {"artifact_id": with_storage}))
    assert read["output"]["storage"] == NOTES_DECL
    assert read["observation"]["storage"] == NOTES_DECL

    read_plain = _run(_run_tool(ReadArtifactTool(), page.report_id, {"artifact_id": without["output"]["artifact_id"]}))
    assert read_plain["output"]["storage"] is None
    assert "storage" not in read_plain["observation"]


def test_planner_current_artifact_shows_storage(page):
    from app.ai.agent_v2 import AgentV2
    from app.ai.agents.planner.prompt_builder import PromptBuilder

    _create_with_notes(page)

    async def _active():
        async with async_session_maker() as db:
            agent = object.__new__(AgentV2)
            agent.db = db
            agent.report = SimpleNamespace(id=page.report_id)
            return await agent._get_active_artifact()

    active = _run(_active())
    assert active["storage"] == NOTES_DECL
    block = PromptBuilder._render_current_artifact(active)
    start = block.index("<storage>") + len("<storage>")
    rendered = block[start:block.index("</storage>")]
    assert json.loads(rendered.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")) == NOTES_DECL

    plain = dict(active, storage=None)
    assert "<storage>" not in PromptBuilder._render_current_artifact(plain)


# ---------------------------------------------------------------------------
# Legacy invariance and unmigrated-database safety
# ---------------------------------------------------------------------------

def test_artifacts_without_storage_keep_their_content_keys_and_never_touch_app_records(page):
    sql = _SqlLog()
    created = _create(page.report_id, {"title": "Plain", "code": _page_code(page.viz_id),
                                       "visualization_ids": [page.viz_id]}, sql=sql)
    v1 = created["output"]["artifact_id"]
    assert set(_run(_content(v1))) == {"code", "visualization_ids", "runtime_version"}

    edited = _edit(page.report_id, {"artifact_id": v1, "edits": [{"find": "Notes</div>", "replace": "Plain</div>"}]}, sql=sql)
    assert edited["output"]["success"] is True, edited["observation"]
    assert set(_run(_content(edited["output"]["artifact_id"]))) == {"code", "visualization_ids", "runtime_version"}

    rebuilt = _create(page.report_id, {"code": _page_code(page.viz_id), "visualization_ids": [page.viz_id],
                                       "replaces_artifact_id": v1}, sql=sql)
    assert set(_run(_content(rebuilt["output"]["artifact_id"]))) == {"code", "visualization_ids", "runtime_version"}

    assert sql.statements, "the listener must have seen the tools' SQL"
    assert not sql.mentions("app_records"), "no storage anywhere: app_records must never be queried"


# ---------------------------------------------------------------------------
# Planner guidance (live-run findings): lenient per_user, misplaced storage,
# write-handling note, effective rules echoed in the summary
# ---------------------------------------------------------------------------

COMMENTS_OWNER = {"scope": "shared", "create": "owner", "modify": "owner",
                  "fields": {"text": {"type": "string", "required": True}}}


def _writing_code(viz_id: str, *, handled: bool) -> str:
    save = "c.add({ text: 'x' })" + (".catch(() => {})" if handled else "")
    return (
        '<script type="text/babel">\n'
        "function App() {\n"
        f'  const v = vizById("{viz_id}");\n'
        '  const c = useCollection("comments");\n'
        '  const p = useCollection("prefs");\n'
        f"  const save = () => {save};\n"
        '  return <div className="p-4">{v ? v.title : "none"} <button onClick={save}>Add</button></div>;\n'
        "}\n"
        "</script>"
    )


def test_create_accepts_per_user_with_meaningless_rules_and_drops_them(page):
    prefs = {**PREFS, "create": "members", "modify": "members"}
    result = _create(page.report_id, {"code": _page_code(page.viz_id, "prefs"), "visualization_ids": [page.viz_id],
                                      "storage": {"collections": {"prefs": prefs}}})
    assert result["output"].get("success", True) is not False, result["observation"]
    assert _run(_content(result["output"]["artifact_id"]))["storage"] == {"collections": {"prefs": PREFS}}


def test_create_hints_when_storage_was_put_inside_prompt(page):
    prompt = 'Genre app. {"replaces_artifact_id":null,"storage":{"collections":{"prefs":{"scope":"per_user"}}}}'
    result = _create(page.report_id, {"prompt": prompt, "code": _page_code(page.viz_id, "prefs"),
                                      "visualization_ids": [page.viz_id]})
    assert result["output"]["success"] is False
    message = result["observation"]["error"]["message"]
    assert "placed inside `prompt`" in message and "separate top-level `storage` argument" in message
    assert "placed inside `prompt`" in result["observation"]["summary"]
    assert _run(_version_count(page.report_id)) == 0


def test_create_without_storage_in_prompt_has_no_misplaced_hint(page):
    result = _create(page.report_id, {"code": _page_code(page.viz_id, "prefs"), "visualization_ids": [page.viz_id]})
    assert result["output"]["success"] is False
    assert "placed inside `prompt`" not in result["observation"]["error"]["message"]


def test_create_success_echoes_rules_and_notes_unhandled_writes(page):
    result = _create(page.report_id, {
        "code": _writing_code(page.viz_id, handled=False), "visualization_ids": [page.viz_id],
        "storage": {"collections": {"comments": COMMENTS_OWNER, "prefs": PREFS}},
    })
    assert result["output"].get("success", True) is not False, result["observation"]
    summary = result["observation"]["summary"]
    assert ("Storage: comments — shared; add: owner only; edit/delete: owner only; public link: not visible. "
            "prefs — private per viewer.") in summary
    assert "Check these rules against the user's words" in summary
    assert "NOTE:" in summary and "catch" in summary


def test_create_with_handled_writes_has_no_write_note(page):
    result = _create(page.report_id, {
        "code": _writing_code(page.viz_id, handled=True), "visualization_ids": [page.viz_id],
        "storage": {"collections": {"comments": COMMENTS_OWNER, "prefs": PREFS}},
    })
    summary = result["observation"]["summary"]
    assert "Storage: comments" in summary
    assert "write rejections" not in summary


def test_create_without_storage_has_no_rules_line(page):
    result = _create(page.report_id, {"code": _page_code(page.viz_id), "visualization_ids": [page.viz_id]})
    assert "Storage:" not in result["observation"]["summary"]


def test_edit_success_echoes_rules_and_notes_unhandled_writes(page):
    created = _create(page.report_id, {
        "code": _writing_code(page.viz_id, handled=True), "visualization_ids": [page.viz_id],
        "storage": {"collections": {"comments": COMMENTS_OWNER, "prefs": PREFS}},
    })
    storage = {"collections": {"comments": {**COMMENTS_OWNER, "create": "members"}, "prefs": PREFS}}

    async def _approve(runtime_ctx, *, tool_name, changes, started_monotonic=None):
        yield {"approved": True, "reason": None}

    import app.ai.tools.implementations._artifact_storage as _st
    original = _st.confirm_storage_changes
    _st.confirm_storage_changes = _approve
    try:
        result = _edit(page.report_id, {
            "artifact_id": created["output"]["artifact_id"], "storage": storage,
            "edits": [{"find": ".catch(() => {})", "replace": ""}],
        })
    finally:
        _st.confirm_storage_changes = original
    assert result["output"]["success"] is True, result["observation"]
    summary = result["observation"]["summary"]
    assert ("Storage: comments — shared; add: members; edit/delete: owner only; public link: not visible. "
            "prefs — private per viewer.") in summary
    assert "Check these rules against the user's words" in summary
    assert "NOTE:" in summary and "catch" in summary


def test_create_echoes_public_link_visibility(page):
    published = {**COMMENTS_OWNER, "public_read": True}
    result = _create(page.report_id, {
        "code": _writing_code(page.viz_id, handled=True), "visualization_ids": [page.viz_id],
        "storage": {"collections": {"comments": published, "prefs": PREFS}},
    })
    assert result["output"].get("success", True) is not False, result["observation"]
    assert "comments — shared; add: owner only; edit/delete: owner only; public link: owner's records visible." in (
        result["observation"]["summary"])
    assert _run(_content(result["output"]["artifact_id"]))["storage"]["collections"]["comments"]["public_read"] is True


def test_public_read_on_a_members_collection_is_rejected_by_the_gate(page):
    bad = {"scope": "shared", "create": "members", "modify": "author", "public_read": True,
           "fields": {"text": {"type": "string", "required": True}}}
    result = _create(page.report_id, {
        "code": _writing_code(page.viz_id, handled=True), "visualization_ids": [page.viz_id],
        "storage": {"collections": {"comments": bad, "prefs": PREFS}},
    })
    assert result["output"]["success"] is False
    message = result["observation"]["error"]["message"]
    assert message.startswith("[storage]") and "public_read" in message and "owner" in message
    assert _run(_version_count(page.report_id)) == 0
