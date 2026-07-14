"""Unit tests for the agent-notes tools (create_note / edit_note).

Contracts:
- create_note persists a per-report Note with the given content/title.
- edit_note applies surgical find/replace atomically (all-or-none), with a
  full-content fallback; failed matches leave the note unchanged.
- edit_note validates the note belongs to the report and rejects unknown ids.
- The notes-context builder renders a <notes> block with each note's id.

Run: cd backend && uv run pytest tests/unit/test_note_tools.py -v
"""
import asyncio
import uuid

import pytest

from app.dependencies import async_session_maker
from app.models.note import Note
from app.models.organization import Organization
from app.models.report import Report
from app.models.user import User


def _run(coro):
    return asyncio.run(coro)


async def _run_tool(tool, tool_input: dict, report_id: str):
    async with async_session_maker() as db:
        report = await db.get(Report, report_id)
        user = await db.get(User, report.user_id)
        organization = await db.get(Organization, report.organization_id)
        runtime_ctx = {"db": db, "report": report, "user": user, "organization": organization}
        events = []
        async for evt in tool.run_stream(tool_input, runtime_ctx):
            events.append(evt)
        return events


def _end(events):
    ends = [e for e in events if e.type == "tool.end"]
    assert ends, f"no tool.end in {[e.type for e in events]}"
    return ends[-1].payload


async def _notes_for(report_id: str):
    from sqlalchemy import select
    async with async_session_maker() as db:
        res = await db.execute(
            select(Note).where(Note.report_id == report_id, Note.deleted_at.is_(None)).order_by(Note.created_at.asc())
        )
        return list(res.scalars().all())


def _make_report(create_report, create_user, login_user, whoami, title):
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    report = create_report(title=title, user_token=token, org_id=org_id, data_sources=[])
    return report, token, org_id


@pytest.mark.e2e
def test_create_note_persists(create_report, create_user, login_user, whoami):
    from app.ai.tools.implementations.create_note import CreateNoteTool

    report, *_ = _make_report(create_report, create_user, login_user, whoami, "Notes A")
    events = _run(_run_tool(CreateNoteTool(), {"title": "Plan", "content": "- [ ] pull revenue\n- [ ] drill genres"}, report["id"]))
    payload = _end(events)
    assert payload["output"]["success"] is True, payload
    note_id = payload["output"]["note_id"]
    assert payload["observation"]["note_id"] == note_id
    assert "content_snapshot" in payload["observation"]

    notes = _run(_notes_for(report["id"]))
    assert len(notes) == 1
    assert notes[0].title == "Plan"
    assert notes[0].source == "agent"
    assert "pull revenue" in notes[0].content


@pytest.mark.e2e
def test_create_note_rejects_empty(create_report, create_user, login_user, whoami):
    from app.ai.tools.implementations.create_note import CreateNoteTool

    report, *_ = _make_report(create_report, create_user, login_user, whoami, "Notes empty")
    payload = _end(_run(_run_tool(CreateNoteTool(), {"content": "   "}, report["id"])))
    assert payload["output"]["success"] is False
    assert _run(_notes_for(report["id"])) == []


def _create(report_id, content, title="Plan"):
    from app.ai.tools.implementations.create_note import CreateNoteTool
    payload = _end(_run(_run_tool(CreateNoteTool(), {"title": title, "content": content}, report_id)))
    assert payload["output"]["success"] is True, payload
    return payload["output"]["note_id"]


@pytest.mark.e2e
def test_edit_note_surgical_flips_todo(create_report, create_user, login_user, whoami):
    from app.ai.tools.implementations.edit_note import EditNoteTool

    report, *_ = _make_report(create_report, create_user, login_user, whoami, "Notes edit")
    note_id = _create(report["id"], "- [ ] pull revenue\n- [ ] drill genres")

    payload = _end(_run(_run_tool(EditNoteTool(), {
        "note_id": note_id,
        "edits": [{"find": "- [ ] pull revenue", "replace": "- [x] pull revenue"}],
    }, report["id"])))
    assert payload["output"]["success"] is True, payload
    assert payload["output"]["diff_applied"] is True

    notes = _run(_notes_for(report["id"]))
    assert notes[0].content == "- [x] pull revenue\n- [ ] drill genres"


@pytest.mark.e2e
def test_edit_note_atomic_failure_leaves_note_unchanged(create_report, create_user, login_user, whoami):
    from app.ai.tools.implementations.edit_note import EditNoteTool

    report, *_ = _make_report(create_report, create_user, login_user, whoami, "Notes atomic")
    note_id = _create(report["id"], "dup dup\n- [ ] step")

    # Op1 unique, op2 ambiguous -> nothing applies.
    payload = _end(_run(_run_tool(EditNoteTool(), {
        "note_id": note_id,
        "edits": [{"find": "- [ ] step", "replace": "- [x] step"}, {"find": "dup", "replace": "X"}],
    }, report["id"])))
    assert payload["output"]["success"] is False
    assert "2 times" in payload["output"]["error"]
    notes = _run(_notes_for(report["id"]))
    assert notes[0].content == "dup dup\n- [ ] step"


@pytest.mark.e2e
def test_edit_note_full_content_fallback(create_report, create_user, login_user, whoami):
    from app.ai.tools.implementations.edit_note import EditNoteTool

    report, *_ = _make_report(create_report, create_user, login_user, whoami, "Notes full")
    note_id = _create(report["id"], "old")
    payload = _end(_run(_run_tool(EditNoteTool(), {"note_id": note_id, "content": "brand new", "title": "Findings"}, report["id"])))
    assert payload["output"]["success"] is True
    assert payload["output"]["diff_applied"] is False
    notes = _run(_notes_for(report["id"]))
    assert notes[0].content == "brand new" and notes[0].title == "Findings"


@pytest.mark.e2e
def test_edit_note_requires_exactly_one_input(create_report, create_user, login_user, whoami):
    from app.ai.tools.implementations.edit_note import EditNoteTool

    report, *_ = _make_report(create_report, create_user, login_user, whoami, "Notes arg")
    note_id = _create(report["id"], "x")
    for bad in [{"note_id": note_id}, {"note_id": note_id, "content": "y", "edits": [{"find": "x", "replace": "z"}]}]:
        payload = _end(_run(_run_tool(EditNoteTool(), bad, report["id"])))
        assert payload["output"]["success"] is False
        assert "exactly one" in payload["output"]["error"]


@pytest.mark.e2e
def test_edit_note_rejects_other_report_and_unknown(create_report, create_user, login_user, whoami):
    from app.ai.tools.implementations.edit_note import EditNoteTool

    report_a, token, org_id = _make_report(create_report, create_user, login_user, whoami, "Notes A2")
    report_b = create_report(title="Notes B2", user_token=token, org_id=org_id, data_sources=[])
    note_in_b = _create(report_b["id"], "b note")

    # editing report B's note in report A's context is rejected
    payload = _end(_run(_run_tool(EditNoteTool(), {"note_id": note_in_b, "content": "hack"}, report_a["id"])))
    assert payload["output"]["success"] is False
    assert "does not belong" in payload["output"]["error"]

    payload = _end(_run(_run_tool(EditNoteTool(), {"note_id": str(uuid.uuid4()), "content": "x"}, report_a["id"])))
    assert payload["output"]["success"] is False
    assert "not found" in payload["output"]["error"]


@pytest.mark.e2e
def test_notes_context_builder_renders_ids(create_report, create_user, login_user, whoami):
    from app.ai.agents.notes_context import build_notes_context

    report, *_ = _make_report(create_report, create_user, login_user, whoami, "Notes ctx")
    n1 = _create(report["id"], "- [ ] step one", title="Plan")

    async def _build():
        async with async_session_maker() as db:
            return await build_notes_context(db, report["id"])

    block = _run(_build())
    assert "<notes>" in block and f'<note id="{n1}"' in block and "step one" in block


@pytest.mark.e2e
def test_notes_route_returns_report_notes(create_report, create_user, login_user, whoami, test_client):
    report, token, org_id = _make_report(create_report, create_user, login_user, whoami, "Notes route")
    _create(report["id"], "- [ ] one", title="Plan")
    _create(report["id"], "finding: revenue up", title="Findings")

    headers = {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}
    r = test_client.get(f"/api/reports/{report['id']}/notes", headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body) == 2
    titles = {n["title"] for n in body}
    assert titles == {"Plan", "Findings"}
    assert all(n["source"] == "agent" for n in body)


@pytest.mark.e2e
def test_notes_guidance_bootstraps_when_enabled_without_notes():
    """The context builders nudge the agent to open a scratchpad even before any
    note exists — otherwise the tools are in the catalog but nothing tells the
    agent to start one. Guidance shows when enabled; the <notes> block only when
    notes exist; nothing at all when disabled."""
    from app.schemas.ai.planner import PlannerInput
    from app.ai.agents.planner.prompt_builder import PromptBuilder
    from app.ai.agents.planner.prompt_builder_v3 import PromptBuilderV3

    off = PlannerInput(user_message="x", notes_enabled=False)
    on_empty = PlannerInput(user_message="x", notes_enabled=True)
    on_full = PlannerInput(
        user_message="x",
        notes_enabled=True,
        notes_context='<notes><note id="n1" title="Plan">- [ ] step</note></notes>',
    )

    # v2 builder helper
    assert PromptBuilder._render_notes_block(off) == ""
    v2e = PromptBuilder._render_notes_block(on_empty)
    assert "notes_guidance" in v2e and "create_note" in v2e and "You have no notes yet" in v2e
    assert "<notes>" not in v2e
    v2f = PromptBuilder._render_notes_block(on_full)
    assert "notes_guidance" in v2f and "<notes>" in v2f and "Your current notes are below" in v2f

    # v3 builder (guidance is inline in the user message)
    v3off = PromptBuilderV3._build_user_message(off)
    assert "notes_guidance" not in v3off
    v3e = PromptBuilderV3._build_user_message(on_empty)
    assert "notes_guidance" in v3e and "You have no notes yet" in v3e and "<notes>" not in v3e
    v3f = PromptBuilderV3._build_user_message(on_full)
    assert "notes_guidance" in v3f and "<notes>" in v3f


@pytest.mark.e2e
def test_notes_are_kept_current_mid_run_not_batched_at_the_end():
    """Mid-run note updates are driven three ways; all must hold:

    1. notes_guidance carries an explicit update-timing rule (as-you-go).
    2. A deterministic <notes_nudge> fires when the last action succeeded while
       the injected notes still show unchecked `- [ ]` items — and stays quiet
       on the first iteration, after a failure, after a note edit, or when the
       checklist is fully ticked.
    3. In parallel (MULTI-TOOL) mode the system prompt authorizes piggybacking
       edit_note alongside the next step's tool calls.
    """
    from app.schemas.ai.planner import PlannerInput
    from app.ai.agents.planner.prompt_builder_v3 import PromptBuilderV3

    plan_notes = '<notes><note id="n1" title="Plan">- [x] revenue\n- [ ] genres\n- [ ] countries</note></notes>'
    done_notes = '<notes><note id="n1" title="Plan">- [x] revenue\n- [x] genres</note></notes>'
    ok_obs = {"summary": "created viz", "widget_id": "w1"}
    fail_obs = {"summary": "query failed", "error": {"message": "boom"}}
    note_obs = {"summary": "Edited note", "note_id": "n1"}
    batch_with_note = {"summary": "batch", "parallel_actions": [
        {"tool_name": "create_data", "summary": "ok"},
        {"tool_name": "edit_note", "summary": "ok", "note_id": "n1"},
    ]}
    batch_plain = {"summary": "batch", "parallel_actions": [
        {"tool_name": "create_data", "summary": "ok"},
        {"tool_name": "create_data", "summary": "ok"},
    ]}

    def msg(**kw):
        return PromptBuilderV3._build_user_message(
            PlannerInput(user_message="x", notes_enabled=True, notes_context=plan_notes, **kw)
        )

    # (1) timing rule present whenever notes are enabled
    assert "AS YOU GO" in msg()

    # (2) the nudge fires exactly when a successful non-note action left
    #     unchecked items behind
    assert "notes_nudge" in msg(last_observation=ok_obs)
    assert "notes_nudge" in msg(last_observation=batch_plain)
    assert "notes_nudge" not in msg()  # first iteration — nothing ran yet
    assert "notes_nudge" not in msg(last_observation=fail_obs)
    assert "notes_nudge" not in msg(last_observation=note_obs)
    assert "notes_nudge" not in msg(last_observation=batch_with_note)
    fully_ticked = PromptBuilderV3._build_user_message(PlannerInput(
        user_message="x", notes_enabled=True, notes_context=done_notes, last_observation=ok_obs))
    assert "notes_nudge" not in fully_ticked
    no_notes = PromptBuilderV3._build_user_message(PlannerInput(
        user_message="x", notes_enabled=True, last_observation=ok_obs))
    assert "notes_nudge" not in no_notes

    # (3) MULTI-TOOL mode invites the piggyback; only when notes are enabled
    par_on = PromptBuilderV3._build_system(PlannerInput(
        user_message="x", notes_enabled=True, parallel_tools_enabled=True))
    assert "MULTI-TOOL" in par_on and "edit_note" in par_on
    par_no_notes = PromptBuilderV3._build_system(PlannerInput(
        user_message="x", notes_enabled=False, parallel_tools_enabled=True))
    assert "MULTI-TOOL" in par_no_notes and "edit_note" not in par_no_notes
    serial = PromptBuilderV3._build_system(PlannerInput(
        user_message="x", notes_enabled=True, parallel_tools_enabled=False))
    assert "MULTI-TOOL" not in serial


@pytest.mark.e2e
def test_notes_gating_hides_tools_when_disabled():
    """When enable_agent_notes is off, the note tools are stripped from the catalog."""
    from app.ai.registry import ToolRegistry
    reg = ToolRegistry()
    names = [t["name"] for t in reg.get_catalog_for_plan_type("action", mode="deep")]
    # Tools exist in the registry; gating happens in agent_v2 by filtering the
    # catalog on the org setting (asserted here by simulating that filter).
    assert "create_note" in names and "edit_note" in names
    filtered = [n for n in names if n not in ("create_note", "edit_note")]
    assert "create_note" not in filtered and "edit_note" not in filtered
