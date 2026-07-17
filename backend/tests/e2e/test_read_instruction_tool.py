"""E2E tests for the read_instruction planner tool.

read_instruction lets the CHAT agent pull the full text of a single instruction
or skill by its SHORT id prefix (the first part of the UUID shown in
<available_skills> / <available_instructions>). It resolves any published
instruction, is HARD-scoped to the report's data sources (global rows always in
scope; no report context → refusal), and is chat-mode-only.

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


def test_read_instruction_is_chat_only():
    from app.ai.tools.implementations.read_instruction import ReadInstructionTool

    md = ReadInstructionTool().metadata
    assert md.allowed_modes == ["chat"]
    assert md.name == "read_instruction"


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
