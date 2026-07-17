"""E2E tests for the instruction generality gate (anti-overfit).

The knowledge harness and training mode capture instructions through the
create_instruction / edit_instruction tools. The gate rejects instructions
whose substance is a record-level fact (a person's attribute, a hardcoded
row id, an observed count/value) with ``rejected_reason="overfit"`` so the
planner can generalize or skip. Contract under test:

1. an overfit critic verdict rejects the create (nothing persisted),
2. a general verdict lets the create proceed unchanged,
3. the gate FAILS OPEN — critic errors / missing LLM never block capture,
4. the same gate guards edit_instruction text changes,
5. the critic-output parsing is robust (unit tests, fake LLM only).

The LLM boundary is stubbed (fake llm / monkeypatched critic) per the
sandbox-feedback-loop rules — Loop A must run without credentials. The
behavioral (real-LLM) leg lives in tests/evals/test_overfit_benchmark.py.
"""
import uuid
from types import SimpleNamespace

import pytest


def _auth(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


def _new_admin(create_user, login_user, whoami):
    email = f"overfit_{uuid.uuid4().hex[:6]}@test.com"
    create_user(email=email, password="test123")
    token = login_user(email=email, password="test123")
    me = whoami(token)
    return token, me["id"], me["organizations"][0]["id"]


async def _run_tool(tool, tool_input, *, user_id, org_id, extra_ctx=None):
    from app.dependencies import async_session_maker

    async with async_session_maker() as db:
        ctx = {
            "db": db,
            "user": SimpleNamespace(id=user_id),
            "organization": SimpleNamespace(id=org_id),
            "mode": "knowledge",
            **(extra_ctx or {}),
        }
        end = None
        async for evt in tool.run_stream(tool_input, ctx):
            if evt.type == "tool.error":
                pytest.fail(f"tool errored: {evt.payload}")
            if evt.type == "tool.end":
                end = evt
        assert end is not None, "expected a tool.end event"
        return end.payload["output"], ctx


class _FakeLLM:
    """Stands in for app.ai.llm.LLM at the inference boundary."""

    def __init__(self, response=None, exc=None):
        self._response = response
        self._exc = exc
        self.prompts = []

    def inference(self, prompt, **kwargs):
        self.prompts.append(prompt)
        if self._exc:
            raise self._exc
        return self._response


# ---------------------------------------------------------------------------
# Critic contract (pure unit tests, fake LLM)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_critic_parses_overfit_and_general_verdicts():
    from app.ai.instruction_quality import check_instruction_generality

    ok, reason = await check_instruction_generality(
        "Joe's last name is Cohen.",
        _FakeLLM('{"verdict": "overfit", "reason": "fact about one person"}'),
    )
    assert ok is False
    assert "person" in reason

    ok, _ = await check_instruction_generality(
        "Exclude cancelled orders from revenue.",
        _FakeLLM('{"verdict": "general", "reason": "reusable filter rule"}'),
    )
    assert ok is True

    # JSON embedded in prose still parses.
    ok, _ = await check_instruction_generality(
        "x" * 30,
        _FakeLLM('Sure! Here is my verdict:\n{"verdict": "overfit", "reason": "r"}'),
    )
    assert ok is False


@pytest.mark.asyncio
async def test_critic_fails_open_on_garbage_error_and_missing_llm():
    from app.ai.instruction_quality import check_instruction_generality

    ok, reason = await check_instruction_generality("some rule", _FakeLLM("not json at all"))
    assert (ok, reason) == (True, None)

    ok, reason = await check_instruction_generality("some rule", _FakeLLM(exc=RuntimeError("api down")))
    assert (ok, reason) == (True, None)

    ok, reason = await check_instruction_generality("some rule", None)
    assert (ok, reason) == (True, None)


# ---------------------------------------------------------------------------
# Tool plumbing (real tools + test DB, critic verdict forced via fake LLM)
# ---------------------------------------------------------------------------

def _gate_ctx(monkeypatch, response=None, exc=None):
    """Route the tools' gate LLM to a fake critic. The tools resolve their
    LLM via instruction_quality.resolve_gate_llm(runtime_ctx), which we
    point at the fake through a sentinel model object."""
    import app.ai.instruction_quality as iq

    fake = _FakeLLM(response=response, exc=exc)
    monkeypatch.setattr(iq, "resolve_gate_llm", lambda ctx: fake if ctx.get("model") else None)
    return {"model": object()}, fake


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_overfit_create_is_rejected_and_not_persisted(
    test_client, create_user, login_user, whoami, monkeypatch
):
    from app.ai.tools.implementations.create_instruction import CreateInstructionTool

    token, user_id, org_id = _new_admin(create_user, login_user, whoami)
    extra_ctx, fake = _gate_ctx(
        monkeypatch, response='{"verdict": "overfit", "reason": "fact about a single customer"}'
    )

    output, _ = await _run_tool(
        CreateInstructionTool(),
        {
            "text": "The customer Ana Petrov changed her last name to Ivanova; show her as Ana Ivanova.",
            "category": "general",
            "confidence": 0.95,
            "evidence": "User stated the name change.",
        },
        user_id=user_id, org_id=org_id, extra_ctx=extra_ctx,
    )
    assert output["success"] is False, output
    assert output["rejected_reason"] == "overfit"
    assert "generaliz" in output["message"].lower() or "record-level" in output["message"].lower()
    assert output.get("instruction_id") is None
    # The critic saw the instruction text, not something else.
    assert "Ana Petrov" in fake.prompts[0]

    # Nothing persisted for the org beyond seed data.
    listing = test_client.get("/api/instructions", headers=_auth(token, org_id)).json()
    items = listing["items"] if isinstance(listing, dict) and "items" in listing else listing
    assert all("Ivanova" not in (i.get("text") or "") for i in items)


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_general_create_passes_gate_and_persists(
    test_client, create_user, login_user, whoami, monkeypatch
):
    from app.ai.tools.implementations.create_instruction import CreateInstructionTool

    token, user_id, org_id = _new_admin(create_user, login_user, whoami)
    extra_ctx, _ = _gate_ctx(
        monkeypatch, response='{"verdict": "general", "reason": "reusable filter rule"}'
    )

    output, _ = await _run_tool(
        CreateInstructionTool(),
        {
            "text": "When calculating revenue, exclude orders with a cancelled or refunded status.",
            "category": "code_gen",
            "confidence": 0.9,
            "evidence": "User correction in session.",
        },
        user_id=user_id, org_id=org_id, extra_ctx=extra_ctx,
    )
    assert output["success"] is True, output
    assert output["instruction_id"]


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_gate_fails_open_when_critic_errors(
    create_user, login_user, whoami, monkeypatch
):
    from app.ai.tools.implementations.create_instruction import CreateInstructionTool

    _token, user_id, org_id = _new_admin(create_user, login_user, whoami)
    extra_ctx, _ = _gate_ctx(monkeypatch, exc=RuntimeError("LLM unavailable"))

    output, _ = await _run_tool(
        CreateInstructionTool(),
        {
            "text": "Treat unit prices as USD amounts in all revenue calculations.",
            "category": "general",
            "confidence": 0.85,
        },
        user_id=user_id, org_id=org_id, extra_ctx=extra_ctx,
    )
    assert output["success"] is True, output


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_gate_skipped_entirely_without_model_in_ctx(
    create_user, login_user, whoami
):
    """Tools driven outside an agent run (no model in runtime_ctx) keep
    working exactly as before — the gate only arms when an LLM exists."""
    from app.ai.tools.implementations.create_instruction import CreateInstructionTool

    _token, user_id, org_id = _new_admin(create_user, login_user, whoami)

    output, _ = await _run_tool(
        CreateInstructionTool(),
        {
            "text": "Use bar charts for 'top N by metric' questions by default.",
            "category": "visualization",
            "confidence": 0.9,
        },
        user_id=user_id, org_id=org_id,
    )
    assert output["success"] is True, output


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_overfit_edit_is_rejected_and_leaves_instruction_unchanged(
    test_client, create_global_instruction, create_user, login_user, whoami, monkeypatch
):
    from app.ai.tools.implementations.edit_instruction import EditInstructionTool

    token, user_id, org_id = _new_admin(create_user, login_user, whoami)
    instr = create_global_instruction(
        text="Count customers with COUNT DISTINCT customer_id.",
        user_token=token, org_id=org_id, status="published",
    )
    iid = instr["id"]

    extra_ctx, _ = _gate_ctx(
        monkeypatch, response='{"verdict": "overfit", "reason": "hardcodes one record"}'
    )
    output, _ = await _run_tool(
        EditInstructionTool(),
        {
            "instruction_id": iid,
            "text": "Count customers with COUNT DISTINCT customer_id, and remember there are exactly 59 customers.",
            "evidence": "Observed count in session.",
        },
        user_id=user_id, org_id=org_id, extra_ctx=extra_ctx,
    )
    assert output["success"] is False, output
    assert output["rejected_reason"] == "overfit"

    # No pending AI suggestion was staged.
    review = test_client.get(
        f"/api/instructions/{iid}/review-hunks", headers=_auth(token, org_id)
    ).json()
    assert [s for s in review["suggestions"] if s["source"] == "ai"] == []


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_edit_without_text_change_skips_gate(
    create_global_instruction, create_user, login_user, whoami, monkeypatch
):
    """Metadata-only edits (confidence, category, scoping) never carry new
    text, so the gate must not fire — even with an overfit-happy critic."""
    from app.ai.tools.implementations.edit_instruction import EditInstructionTool

    token, user_id, org_id = _new_admin(create_user, login_user, whoami)
    instr = create_global_instruction(
        text="Exclude test accounts from user counts.",
        user_token=token, org_id=org_id, status="published",
    )

    extra_ctx, fake = _gate_ctx(
        monkeypatch, response='{"verdict": "overfit", "reason": "should never be consulted"}'
    )
    output, _ = await _run_tool(
        EditInstructionTool(),
        {"instruction_id": instr["id"], "category": "code_gen"},
        user_id=user_id, org_id=org_id, extra_ctx=extra_ctx,
    )
    assert output["success"] is True, output
    assert fake.prompts == []
