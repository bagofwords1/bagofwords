"""E2E tests for the read_instruction planner tool.

read_instruction pulls the full text of a single instruction or skill by its
SHORT id prefix (the first part of the UUID shown in <available_skills> /
<available_instructions>). It resolves any published instruction and is
HARD-scoped to the report's data sources (global rows always in scope; no
report context → refusal).

It is available in chat AND in the editing modes: an anchored edit_instruction
can only match text the agent has actually read, so the read tool has to exist
where editing happens.

We exercise the tool directly via run_stream against the real test DB.
"""
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

_SQLITE_DB = (Path(__file__).resolve().parent.parent / "config" / "chinook.sqlite").resolve()


def _auth(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


def _new_admin(create_user, login_user, whoami):
    email = f"readinstr_{uuid.uuid4().hex[:6]}@test.com"
    create_user(email=email, password="test123")
    token = login_user(email=email, password="test123")
    me = whoami(token)
    return token, me["id"], me["organizations"][0]["id"]


def _create_instruction(test_client, token, org_id, **fields):
    payload = {"status": "published", **fields}
    resp = test_client.post("/api/instructions", json=payload, headers=_auth(token, org_id))
    assert resp.status_code == 200, resp.json()
    return resp.json()


async def _run(tool_input, *, user_id, org_id, scope_ds_ids=None, with_report=True, report_id=None):
    from app.dependencies import async_session_maker
    from app.ai.tools.implementations.read_instruction import ReadInstructionTool

    tool = ReadInstructionTool()
    async with async_session_maker() as db:
        ctx = {
            "db": db,
            "user": SimpleNamespace(id=user_id),
            "organization": SimpleNamespace(id=org_id),
            "mode": "chat",
        }
        if with_report:
            ctx["report"] = SimpleNamespace(
                id=report_id or str(uuid.uuid4()),
                data_sources=[SimpleNamespace(id=d) for d in (scope_ds_ids or [])],
            )
        end = None
        async for evt in tool.run_stream(tool_input, ctx):
            if evt.type == "tool.end":
                end = evt
            if evt.type == "tool.error":
                return {"success": False, "error": evt.payload}
        assert end is not None, "expected a tool.end event"
        return end.payload["output"]


def test_read_instruction_is_available_where_editing_happens():
    """edit_instruction runs in training/knowledge and its anchors must match
    text the agent has read — a chat-only read tool made that impossible."""
    from app.ai.tools.implementations.read_instruction import ReadInstructionTool
    from app.ai.tools.implementations.edit_instruction import EditInstructionTool

    md = ReadInstructionTool().metadata
    assert md.name == "read_instruction"
    assert set(EditInstructionTool().metadata.allowed_modes) <= set(md.allowed_modes)
    assert "chat" in md.allowed_modes


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_read_skill_by_short_id_prefix(create_user, login_user, whoami, test_client):
    token, uid, org_id = _new_admin(create_user, login_user, whoami)
    instr = _create_instruction(
        test_client, token, org_id,
        text="Revenue excludes refunds and chargebacks.",
        title="Revenue definition", description="How revenue is computed.", kind="skill",
    )
    short_id = instr["id"][:8]

    out = await _run({"id": short_id}, user_id=uid, org_id=org_id)
    assert out["success"] is True, out
    assert out["id"] == instr["id"]
    assert out["short_id"] == short_id
    assert out["description"] == "How revenue is computed."
    assert "refunds" in out["text"]
    assert out["kind"] == "skill"


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_read_full_uuid_also_works(create_user, login_user, whoami, test_client):
    token, uid, org_id = _new_admin(create_user, login_user, whoami)
    instr = _create_instruction(
        test_client, token, org_id, text="Body text here.", title="T", kind="skill",
    )
    out = await _run({"id": instr["id"]}, user_id=uid, org_id=org_id)
    assert out["success"] is True, out
    assert out["id"] == instr["id"]


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_read_regular_instruction(create_user, login_user, whoami, test_client):
    """Unlike the old read_skill, read_instruction resolves normal (non-skill)
    instructions — that's the point of the <available_instructions> catalog."""
    token, uid, org_id = _new_admin(create_user, login_user, whoami)
    instr = _create_instruction(
        test_client, token, org_id,
        text="Cancelled orders are excluded from KPIs.",
        title="Cancelled orders", kind="instruction", load_mode="intelligent",
    )
    out = await _run({"id": instr["id"][:8]}, user_id=uid, org_id=org_id)
    assert out["success"] is True, out
    assert out["kind"] == "instruction"
    assert "Cancelled orders are excluded" in out["text"]


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_read_records_on_demand_usage(create_user, login_user, whoami, test_client, create_report):
    token, uid, org_id = _new_admin(create_user, login_user, whoami)
    instr = _create_instruction(
        test_client, token, org_id,
        text="Usage tracked rule.", title="Tracked", kind="instruction", load_mode="intelligent",
    )
    # The usage event's report_id FK is enforced on postgres, so the tool must
    # run against a report that actually exists (a random UUID only survives
    # sqlite, where FKs aren't enforced).
    report = create_report(user_token=token, org_id=org_id)
    out = await _run({"id": instr["id"][:8]}, user_id=uid, org_id=org_id, report_id=report["id"])
    assert out["success"] is True, out

    from app.dependencies import async_session_maker
    from sqlalchemy import select
    from app.models.instruction_usage_event import InstructionUsageEvent

    async with async_session_maker() as db:
        rows = (await db.execute(
            select(InstructionUsageEvent).where(
                InstructionUsageEvent.instruction_id == instr["id"],
                InstructionUsageEvent.load_reason == "on_demand",
            )
        )).scalars().all()
    assert len(rows) == 1, "expected exactly one on_demand usage event"


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_no_report_context_is_refused(create_user, login_user, whoami, test_client):
    """Without a report in runtime_ctx the tool refuses (no org-wide fallback)."""
    token, uid, org_id = _new_admin(create_user, login_user, whoami)
    instr = _create_instruction(
        test_client, token, org_id, text="Sensitive rule.", title="S", kind="instruction",
    )
    out = await _run({"id": instr["id"][:8]}, user_id=uid, org_id=org_id, with_report=False)
    assert out["success"] is False, out
    assert "report session" in (out["message"] or "")


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_no_match_is_soft_error(create_user, login_user, whoami, test_client):
    token, uid, org_id = _new_admin(create_user, login_user, whoami)
    out = await _run({"id": "zzzz9999"}, user_id=uid, org_id=org_id)
    assert out["success"] is False, out
    assert "No instruction found" in (out["message"] or "")


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_read_scoped_to_report_data_sources(
    create_user, login_user, whoami, test_client, create_data_source
):
    """An instruction attached to data source A is not readable from a report
    whose scope is data source B; it IS readable when the report includes A."""
    token, uid, org_id = _new_admin(create_user, login_user, whoami)
    ds_a = create_data_source(
        name="ds_a", type="sqlite", config={"database": str(_SQLITE_DB)},
        credentials={}, user_token=token, org_id=org_id,
    )
    ds_b = create_data_source(
        name="ds_b", type="sqlite", config={"database": str(_SQLITE_DB)},
        credentials={}, user_token=token, org_id=org_id,
    )
    instr = _create_instruction(
        test_client, token, org_id,
        text="Rule scoped to ds_a.", title="Scoped", kind="instruction",
        load_mode="intelligent", data_source_ids=[ds_a["id"]],
    )
    short_id = instr["id"][:8]

    out_b = await _run({"id": short_id}, user_id=uid, org_id=org_id, scope_ds_ids=[ds_b["id"]])
    assert out_b["success"] is False, out_b

    out_a = await _run({"id": short_id}, user_id=uid, org_id=org_id, scope_ds_ids=[ds_a["id"]])
    assert out_a["success"] is True, out_a
    assert out_a["id"] == instr["id"]


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_pending_changes_are_reported_and_kept_out_of_the_body(
    create_user, login_user, whoami, test_client
):
    """A suggestion awaiting review is surfaced with who/when/what — but under
    its own key. `text` stays the LIVE instruction, because that is the only
    text an edit_instruction anchor can match."""
    from app.dependencies import async_session_maker
    from app.ai.tools.implementations.edit_instruction import EditInstructionTool

    token, uid, org_id = _new_admin(create_user, login_user, whoami)
    live = "Revenue excludes cancelled orders."
    instr = _create_instruction(test_client, token, org_id, text=live, title="Revenue")

    proposed = "Refunded orders are excluded as well."
    async with async_session_maker() as db:
        ctx = {"db": db, "user": SimpleNamespace(id=uid),
               "organization": SimpleNamespace(id=org_id), "mode": "training"}
        async for evt in EditInstructionTool().run_stream(
            {"instruction_id": instr["id"], "old_text": live,
             "text": f"{live} {proposed}", "evidence": "User clarified."}, ctx,
        ):
            if evt.type == "tool.error":
                pytest.fail(f"edit errored: {evt.payload}")

    out = await _run({"id": instr["id"][:8]}, user_id=uid, org_id=org_id)
    assert out["success"] is True, out
    # The body is the live instruction — the proposal is NOT folded into it.
    assert out["text"].strip() == live
    assert proposed not in (out["text"] or "")

    pending = out["pending_changes"]
    assert pending and len(pending) == 1, pending
    entry = pending[0]
    assert entry["build_id"] and entry["change_count"] >= 1
    assert entry["evidence"] == "User clarified."
    assert entry["proposed_at"]
    assert any(proposed[:20] in (c["with"] or "") for c in entry["changes"]), entry


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_no_pending_changes_reads_clean(create_user, login_user, whoami, test_client):
    token, uid, org_id = _new_admin(create_user, login_user, whoami)
    instr = _create_instruction(
        test_client, token, org_id, text="Revenue excludes cancelled orders.", title="Revenue")

    out = await _run({"id": instr["id"][:8]}, user_id=uid, org_id=org_id)
    assert out["success"] is True, out
    assert out["pending_changes"] is None
