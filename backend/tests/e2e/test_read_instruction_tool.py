"""E2E tests for the read_instruction planner tool.

read_instruction lets the CHAT agent pull the full text of a single
skill/instruction by its SHORT id prefix (the first part of the UUID shown in
<available_skills>). It's scoped to the report's data sources (global rows are
always in scope) and is chat-mode-only.

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


async def _run(tool_input, *, user_id, org_id, scope_ds_ids=None):
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
        if scope_ds_ids is not None:
            ctx["report"] = SimpleNamespace(
                data_sources=[SimpleNamespace(id=d) for d in scope_ds_ids]
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
async def test_read_instruction_by_short_id_prefix(
    create_user, login_user, whoami, test_client
):
    token, uid, org_id = _new_admin(create_user, login_user, whoami)
    instr = _create_instruction(
        test_client, token, org_id,
        text="Revenue excludes refunds and chargebacks.",
        title="Revenue definition",
        kind="skill",
    )
    short_id = instr["id"][:8]

    out = await _run({"id": short_id}, user_id=uid, org_id=org_id)
    assert out["success"] is True, out
    assert out["id"] == instr["id"]
    assert out["short_id"] == short_id
    assert out["kind"] == "skill"
    assert "refunds" in out["text"]


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_read_instruction_full_uuid_also_works(
    create_user, login_user, whoami, test_client
):
    token, uid, org_id = _new_admin(create_user, login_user, whoami)
    instr = _create_instruction(
        test_client, token, org_id, text="Body text here.", title="T", kind="instruction",
    )
    out = await _run({"id": instr["id"]}, user_id=uid, org_id=org_id)
    assert out["success"] is True, out
    assert out["id"] == instr["id"]


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_read_instruction_no_match_is_soft_error(
    create_user, login_user, whoami, test_client
):
    token, uid, org_id = _new_admin(create_user, login_user, whoami)
    # A prefix that cannot exist (UUIDs are hex; 'zzzz' is not).
    out = await _run({"id": "zzzz9999"}, user_id=uid, org_id=org_id)
    assert out["success"] is False, out
    assert "No instruction found" in (out["message"] or "")


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_read_instruction_scoped_to_report_data_sources(
    create_user, login_user, whoami, test_client, create_data_source
):
    """A skill attached to data source A is not readable from a report whose
    scope is data source B; it IS readable when the report includes A."""
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
        text="Skill scoped to ds_a.", title="Scoped", kind="skill",
        data_source_ids=[ds_a["id"]],
    )
    short_id = instr["id"][:8]

    # Out of scope (report only sees ds_b) -> not readable.
    out_b = await _run({"id": short_id}, user_id=uid, org_id=org_id, scope_ds_ids=[ds_b["id"]])
    assert out_b["success"] is False, out_b

    # In scope (report includes ds_a) -> readable.
    out_a = await _run({"id": short_id}, user_id=uid, org_id=org_id, scope_ds_ids=[ds_a["id"]])
    assert out_a["success"] is True, out_a
    assert out_a["id"] == instr["id"]
