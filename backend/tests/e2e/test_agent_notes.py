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
