"""Per-request opt-outs on the completion prompt: ``prompt.disable``.

An API caller turns things off for one turn with
``prompt.disable = {follow_ups, suggest_instructions, tools: [...]}``. Contract:
  * disable.follow_ups / disable.suggest_instructions skip that end-of-turn
    extra for this turn only; an org setting that is off stays off;
  * disable.tools hides built-in tools from the planner, refuses them if the
    model calls one anyway, and tells the model they are unavailable;
    execute_mcp / submit_list also cover their per-tool native forms;
  * sub-agents and their fan-in turn inherit the request's disable;
  * the 0.0.588 top-level fields, unknown tools and internal tools are 422.

The LLM boundary is stubbed (``LLM.inference_stream_v2`` for planner calls,
``LLM.inference`` for small-model one-shots), plus the harness trigger verdict
in the harness tests (see ``harness_triggers``) and the MCP provider client;
routes, services, the agent loop, and persistence run real. Every planner
request is recorded with the tool names it offered and its full text, so what
the model was shown is observed at that boundary.

Run: cd backend && uv run pytest tests/e2e/test_prompt_disable.py -q --db=sqlite
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import time
import uuid
from unittest.mock import patch

import pytest

HARNESS_MARKER = "running the Knowledge Harness"
FOLLOW_UPS = ["Break revenue down by region", "Compare with the same month last year"]
ANSWER = "Revenue grew 12% month over month, led by the enterprise plan."
QUESTION = "How did monthly revenue change across the last quarter?"
CORRECTION = "No, I meant net revenue instead of gross revenue for that quarter"
CLARIFY_ARGS = {"questions": [{"text": "Which revenue definition?", "options": ["gross", "net"]}]}


# ─────────────────────────────────────────────────────────────────────────────
# Scripted LLM
# ─────────────────────────────────────────────────────────────────────────────

def _text(messages) -> str:
    out = []
    for m in messages or []:
        c = getattr(m, "content", None) if not isinstance(m, dict) else m.get("content")
        if isinstance(c, str):
            out.append(c)
        elif isinstance(c, list):
            for b in c:
                if not isinstance(b, dict):
                    continue
                if isinstance(b.get("text"), str):
                    out.append(b["text"])
                inner = b.get("content")
                if isinstance(inner, str):
                    out.append(inner)
                elif isinstance(inner, list):
                    out += [x["text"] for x in inner if isinstance(x, dict) and isinstance(x.get("text"), str)]
    return "\n".join(out)


def _has_tool_result(messages) -> bool:
    for m in messages or []:
        c = getattr(m, "content", None) if not isinstance(m, dict) else m.get("content")
        if isinstance(c, list) and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in c):
            return True
    return False


class Script:
    """What the planner stub does on a request's FIRST step (no tool result
    yet): answer in text (default), call ``force_tool``, or ``delegate``."""

    def __init__(self):
        self.calls: list = []
        self.force_tool: tuple | None = None  # (name, args); used once
        self.delegate = False

    def stream(self):
        from app.ai.llm.types import (
            MessageStopEvent, TextDeltaEvent, ToolUseCompleteEvent, ToolUseStartEvent, UsageEvent,
        )
        script = self

        def tool(name, args):
            tid = f"tu_{uuid.uuid4().hex[:8]}"
            return [ToolUseStartEvent(id=tid, name=name), ToolUseCompleteEvent(id=tid, name=name, input=args)]

        def end(reason="end_turn"):
            return [MessageStopEvent(stop_reason=reason, raw_stop_reason=reason), UsageEvent(input_tokens=100, output_tokens=20)]

        async def scripted(self, *, model_id=None, messages, system=None, tools=None, **kwargs):
            sys_text = system or ""
            text = _text(messages)
            names = {getattr(t, "name", None) or (t.get("name") if isinstance(t, dict) else None) for t in (tools or [])}
            names.discard(None)
            first_step = not _has_tool_result(messages)
            if "Mode: Sub-agent" in sys_text:
                kind = "child"
            elif "<sub_agent_results" in text:
                kind = "fan_in"
            elif HARNESS_MARKER in sys_text:
                kind = "harness"
            else:
                kind = "main"
            script.calls.append({"kind": kind, "tools": names, "text": text, "system": sys_text})

            if kind == "child":
                events = tool("finish_subtask", {
                    "outcome": "refuted", "confidence": 0.8, "summary": "No effect on revenue.",
                    "findings": [], "ruled_out": [], "evidence": [], "caveats": [],
                    "unknown": None, "report_note_id": None,
                }) + end("tool_use")
            elif kind == "main" and first_step and script.delegate and "delegate" in names:
                events = tool("delegate", {
                    "goal": "Check whether pricing changed monthly revenue.",
                    "context": "Revenue = sum of paid invoices.", "title": "Pricing hypothesis",
                }) + end("tool_use")
            elif kind == "main" and first_step and script.force_tool:
                events = tool(*script.force_tool) + end("tool_use")
                script.force_tool = None
            else:
                events = [TextDeltaEvent(text=ANSWER)] + end()
            for e in events:
                yield e

        return scripted


def _one_shot(self, prompt, *args, usage_scope=None, **kwargs):
    # Follow-up generation parses a JSON list; every other small-model call
    # (title, judge, check-in planner) just needs a harmless string.
    if usage_scope == "report.follow_ups":
        return json.dumps(FOLLOW_UPS)
    return "ok"


async def _one_shot_stream(self, prompt, **kwargs):
    yield "ok"


@pytest.fixture
def llm(monkeypatch):
    from app.ai.llm.llm import LLM

    script = Script()
    monkeypatch.setattr(LLM, "inference_stream_v2", script.stream())
    monkeypatch.setattr(LLM, "inference_stream", _one_shot_stream)
    monkeypatch.setattr(LLM, "inference", _one_shot)
    return script


@pytest.fixture
def env(create_user, login_user, whoami, create_report, create_llm_provider_and_models, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY_TEST", "dummy-local-provider-boundary")
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    create_llm_provider_and_models(user_token=token, org_id=org_id)
    headers = {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}

    def new_report(title="Opt-outs", data_sources=()):
        return create_report(title=title, user_token=token, org_id=org_id, data_sources=list(data_sources))["id"]

    return {"token": token, "org_id": org_id, "headers": headers, "new_report": new_report}


def _post(client, report_id, headers, prompt: dict):
    return client.post(f"/api/reports/{report_id}/completions", json={"prompt": prompt},
                       headers=headers, params={"background": False})


def _ask(client, report_id, headers, content, **prompt_fields):
    r = _post(client, report_id, headers, {"content": content, "mentions": [{}], **prompt_fields})
    assert r.status_code == 200, r.text
    return r.json()["completions"]


def _last_answer(client, report_id, headers):
    r = client.get(f"/api/reports/{report_id}/completions", headers=headers)
    assert r.status_code == 200, r.text
    systems = [c for c in r.json()["completions"] if c["role"] == "system"]
    assert systems, "no system completion recorded"
    return systems[-1]


def _main_calls(calls, since=0):
    return [c for c in calls[since:] if c["kind"] == "main"]


# ─────────────────────────────────────────────────────────────────────────────
# disable.follow_ups / disable.suggest_instructions
# ─────────────────────────────────────────────────────────────────────────────

def test_disable_follow_ups_skips_suggestions_for_that_turn_only(test_client, env, llm):
    rid = env["new_report"]()
    h = env["headers"]

    _ask(test_client, rid, h, QUESTION)
    assert _last_answer(test_client, rid, h)["follow_ups"], "control: follow-ups are on by default"

    _ask(test_client, rid, h, "And which plan contributed most to that growth?", disable={"follow_ups": True})
    assert not _last_answer(test_client, rid, h)["follow_ups"]

    # Per request: the next turn without it gets suggestions again.
    _ask(test_client, rid, h, "Show the same breakdown for the previous quarter")
    assert _last_answer(test_client, rid, h)["follow_ups"]


def test_disable_follow_ups_false_does_not_override_org_setting_off(
    test_client, env, llm, update_organization_settings,
):
    update_organization_settings(
        {"enable_follow_ups": {"value": False}}, user_token=env["token"], org_id=env["org_id"],
    )
    rid = env["new_report"]()
    _ask(test_client, rid, env["headers"], QUESTION, disable={"follow_ups": False})
    assert not _last_answer(test_client, rid, env["headers"])["follow_ups"]


@pytest.fixture
def harness_triggers(monkeypatch):
    """Every harness trigger needs a successful create_data in the turn, which
    the API can't produce without a real code-gen LLM and sandbox in the loop.
    Stand in for the trigger verdict only: the harness itself, and the opt-out
    gate in front of the verdict, run real."""
    from app.ai.agents.suggest_instructions import InstructionTriggerEvaluator

    async def fired(self, prev_tool_name_before_last_user=None):
        return {
            "decision": True,
            "conditions": [{"name": "user_explicit_correction", "hint": "User corrected the metric."}],
            "session_maturity": "training",
        }

    monkeypatch.setattr(InstructionTriggerEvaluator, "evaluate", fired)


@pytest.mark.parametrize("disabled", [False, True], ids=["default", "disable_suggest_instructions"])
def test_disable_suggest_instructions_skips_knowledge_harness(test_client, env, llm, harness_triggers, disabled):
    rid = env["new_report"]()
    since = len(llm.calls)
    fields = {"disable": {"suggest_instructions": True}} if disabled else {}
    _ask(test_client, rid, env["headers"], CORRECTION, **fields)

    harness_ran = any(c["kind"] == "harness" for c in llm.calls[since:])
    assert harness_ran is (not disabled)


@pytest.mark.parametrize("disabled", [False, True], ids=["control", "disabled"])
def test_disabled_tools_also_apply_inside_the_harness(test_client, env, llm, harness_triggers, disabled):
    rid = env["new_report"]()
    since = len(llm.calls)
    fields = {"disable": {"tools": ["search_instructions"]}} if disabled else {}
    _ask(test_client, rid, env["headers"], CORRECTION, **fields)

    harness = [c for c in llm.calls[since:] if c["kind"] == "harness"]
    assert harness, "the harness ran"
    assert ("search_instructions" in harness[0]["tools"]) is (not disabled)
    assert ("<disabled_tools>" in harness[0]["text"]) is disabled


# ─────────────────────────────────────────────────────────────────────────────
# disable.tools
# ─────────────────────────────────────────────────────────────────────────────

DISABLED = ["clarify", "search_instructions"]


def test_disabled_tools_are_not_offered_and_are_named_in_the_prompt(test_client, env, llm):
    rid = env["new_report"]()
    h = env["headers"]

    _ask(test_client, rid, h, QUESTION)
    control = _main_calls(llm.calls)[0]
    assert set(DISABLED) <= control["tools"], "control: both tools are offered by default"
    assert "<disabled_tools>" not in control["text"]

    since = len(llm.calls)
    _ask(test_client, rid, h, "Now split it by region", disable={"tools": DISABLED})
    call = _main_calls(llm.calls, since)[0]
    assert not (set(DISABLED) & call["tools"])
    # Nothing else was taken away.
    assert control["tools"] - set(DISABLED) <= call["tools"]
    assert "<disabled_tools>" in call["text"] and all(t in call["text"] for t in DISABLED)
    assert "<disabled_tools>" not in call["system"], "per-request text stays out of the cached system prompt"


@pytest.mark.parametrize("disabled", [False, True], ids=["control", "disabled"])
def test_disabled_tool_called_anyway_is_refused_not_run(test_client, env, llm, disabled):
    """The model may still name a hidden tool (the policy prompt mentions
    clarify by name). With clarify disabled the call is refused before it runs
    and the loop goes on to an answer; without it the same call runs."""
    llm.force_tool = ("clarify", CLARIFY_ARGS)
    rid = env["new_report"]()
    since = len(llm.calls)
    fields = {"disable": {"tools": ["clarify"]}} if disabled else {}
    completions = _ask(test_client, rid, env["headers"], QUESTION, **fields)

    system = [c for c in completions if c["role"] == "system"][-1]
    clarify_ran = any(
        (b.get("tool_execution") or {}).get("tool_name") == "clarify"
        and (b.get("tool_execution") or {}).get("status") == "success"
        for b in (system.get("completion_blocks") or [])
    )
    assert clarify_ran is (not disabled)
    if disabled:
        later = _main_calls(llm.calls, since)[1:]
        assert later and "disabled" in later[0]["text"].lower(), "the refusal reached the model"


def test_replaced_field_sent_as_null_is_ignored(test_client, env, llm):
    rid = env["new_report"]()
    r = _post(test_client, rid, env["headers"], {"content": QUESTION, "follow_ups": None, "suggest_instructions": None})
    assert r.status_code == 200, r.text


@pytest.mark.parametrize("prompt_fields, where", [
    ({"follow_ups": False}, "disable.follow_ups"),
    ({"suggest_instructions": False}, "disable.suggest_instructions"),
    ({"disable": {"tools": ["not_a_tool"]}}, "not_a_tool"),
    ({"disable": {"tools": ["mcp__Intercom__search"]}}, "mcp__Intercom__search"),
    ({"disable": {"tools": ["finish_subtask"]}}, "finish_subtask"),
    ({"disable": {"follow_ups": "sometimes"}}, "follow_ups"),
    ({"disable": {"tool": ["clarify"]}}, "tool"),
], ids=["replaced-follow_ups", "replaced-suggest_instructions", "unknown-tool", "native-mcp-name", "internal-tool", "non-boolean", "misspelled-key"])
def test_invalid_disable_is_422(test_client, env, llm, prompt_fields, where):
    rid = env["new_report"]()
    r = _post(test_client, rid, env["headers"], {"content": QUESTION, **prompt_fields})
    assert r.status_code == 422, r.text
    assert where in r.text
    assert not llm.calls, "a rejected request never reaches the agent"


# ── execute_mcp / submit_list also cover their native per-tool forms ─────────

@contextlib.contextmanager
def _mock_mcp_provider():
    from tests.mocks.mock_mcp_server import MockToolProviderClient

    client = MockToolProviderClient()

    async def _construct(self, db, connection, current_user=None, **kwargs):
        return client

    with patch("app.services.connection_service.ConnectionService.construct_client", _construct):
        yield


@pytest.fixture
def tool_agent(test_client, env, enable_mcp, create_mcp_connection, create_domain_from_connection,
               update_organization_settings):
    """An agent backed by an MCP connection (tools registered natively) that
    also owns an Agent List (one submit_<list> tool)."""
    token, org_id, h = env["token"], env["org_id"], env["headers"]
    enable_mcp(user_token=token, org_id=org_id)
    update_organization_settings(
        {"enable_mcp_native_tools": {"value": True}, "mcp_native_tools_threshold": {"value": 0}},
        user_token=token, org_id=org_id,
    )
    conn = create_mcp_connection(name="Mock MCP", server_url="http://mock:3000/mcp", user_token=token, org_id=org_id)
    with _mock_mcp_provider():
        r = test_client.post(f"/api/connections/{conn['id']}/refresh-tools", headers=h)
        assert r.status_code == 200, r.text
    ds = create_domain_from_connection(name="Mock Agent", connection_id=conn["id"], user_token=token, org_id=org_id)
    r = test_client.post(f"/api/data_sources/{ds['id']}/lists", headers=h, json={
        "name": "Contracts", "description": "Customer contracts", "key_field": "counterparty",
        "fields": [{"name": "counterparty", "type": "string", "required": True, "description": "Other party"}],
    })
    assert r.status_code == 201, r.text
    return {"report": env["new_report"](data_sources=[str(ds["id"])])}


def _is_native_mcp(name):
    return name.startswith("mcp__")


def _is_list_tool(name):
    return name.startswith("submit_") and name != "submit_list"


@pytest.mark.parametrize("gateway, hidden, kept", [
    ("execute_mcp", _is_native_mcp, _is_list_tool),
    ("submit_list", _is_list_tool, _is_native_mcp),
], ids=["execute_mcp", "submit_list"])
def test_disabling_a_gateway_hides_its_native_tools(test_client, env, llm, tool_agent, gateway, hidden, kept):
    rid, h = tool_agent["report"], env["headers"]
    with _mock_mcp_provider():
        _ask(test_client, rid, h, QUESTION)
        control = _main_calls(llm.calls)[0]["tools"]
        assert any(hidden(n) for n in control) and any(kept(n) for n in control), sorted(control)

        since = len(llm.calls)
        _ask(test_client, rid, h, "Now split it by region", disable={"tools": [gateway]})
        offered = _main_calls(llm.calls, since)[0]["tools"]
    assert gateway not in offered
    assert not any(hidden(n) for n in offered)
    assert any(kept(n) for n in offered)
    if gateway == "execute_mcp":
        # The schema context must not keep advertising MCP tools either.
        texts = [c["text"] for c in _main_calls(llm.calls)]
        assert "<mcp_tools" in texts[0] and "<mcp_tools" not in texts[-1]


def test_disabling_execute_mcp_does_not_leak_into_the_next_request(test_client, env, llm, tool_agent):
    """Schemas are cached process-wide for minutes. A request that hid its MCP
    tools must not hand that trimmed schema to the next, ordinary request."""
    rid, h = tool_agent["report"], env["headers"]
    with _mock_mcp_provider():
        _ask(test_client, rid, h, QUESTION, disable={"tools": ["execute_mcp"]})
        since = len(llm.calls)
        _ask(test_client, rid, h, "Now split it by region")
    call = _main_calls(llm.calls, since)[0]
    assert "execute_mcp" in call["tools"] and any(_is_native_mcp(n) for n in call["tools"])
    assert "<mcp_tools" in call["text"]


# ── sub-agents and their fan-in inherit the request's disable ───────────────

@pytest.fixture
def live_client():
    """Keeps one event loop for the whole test, so the sub-agent runs and the
    fan-in the request spawns in the background are not cancelled."""
    from fastapi.testclient import TestClient
    from main import app

    with TestClient(app) as client:
        yield client


def _wait_for(pred, timeout_s=120.0, what="condition"):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        found = pred()
        if found:
            return found
        time.sleep(0.5)
    raise AssertionError(f"timed out waiting for {what}")


def _run_wave(client, env, llm, **prompt_fields):
    since = len(llm.calls)
    rid = env["new_report"]()
    _ask(client, rid, env["headers"], "Why did revenue drop in September?", **prompt_fields)
    child = _wait_for(lambda: next((c for c in llm.calls[since:] if c["kind"] == "child"), None), what="child run")
    fan_in = _wait_for(lambda: next((c for c in llm.calls[since:] if c["kind"] == "fan_in"), None), what="fan-in turn")
    return child, fan_in


def test_sub_agents_and_fan_in_inherit_disabled_tools(live_client, env, llm):
    llm.delegate = True
    child, fan_in = _run_wave(live_client, env, llm)
    # A tool both the child (research profile) and the fan-in turn are offered.
    shared = sorted((child["tools"] & fan_in["tools"]) - {"finish_subtask"})
    assert shared, "control: child and fan-in share at least one tool"
    target = shared[0]

    child, fan_in = _run_wave(live_client, env, llm, disable={"tools": [target]})
    assert target not in child["tools"]
    assert target not in fan_in["tools"]
    assert "finish_subtask" in child["tools"], "the child can still finish"


# ── turns that continue the request inherit its disable ─────────────────────

@pytest.mark.parametrize("disabled", [False, True], ids=["control", "disabled"])
def test_wait_resume_inherits_disable(test_client, env, llm, monkeypatch, disabled):
    from app.services import wait_service

    armed = []
    monkeypatch.setattr(wait_service.scheduler, "add_job", lambda **job: armed.append(job["kwargs"]))
    llm.force_tool = ("wait", {"reason": "Retry the revenue breakdown", "delay_minutes": 5})
    rid = env["new_report"]()
    fields = {"disable": {"tools": DISABLED}} if disabled else {}
    _ask(test_client, rid, env["headers"], QUESTION, **fields)
    assert armed, "the wait was armed"

    since = len(llm.calls)
    asyncio.run(wait_service.run_wait_wake(**armed[0]))  # the scheduler firing
    resumed = _main_calls(llm.calls, since)
    assert resumed, "the wake ran a turn"
    assert (not (set(DISABLED) & resumed[0]["tools"])) is disabled


@pytest.mark.parametrize("disabled", [False, True], ids=["control", "disabled"])
def test_steer_after_the_run_ended_inherits_disable(live_client, env, llm, disabled):
    rid = env["new_report"]()
    h = env["headers"]
    fields = {"disable": {"tools": DISABLED}} if disabled else {}
    completions = _ask(live_client, rid, h, QUESTION, **fields)
    system_id = [c for c in completions if c["role"] == "system"][-1]["id"]

    since = len(llm.calls)
    r = live_client.post(f"/api/completions/{system_id}/steer", json={"content": "Also split it by region"}, headers=h)
    assert r.status_code == 200 and r.json().get("status") == "queued", r.text
    call = _wait_for(lambda: next(iter(_main_calls(llm.calls, since)), None), what="steered turn")
    assert (not (set(DISABLED) & call["tools"])) is disabled


def test_wait_resume_survives_a_tool_removed_before_the_wake(test_client, env, llm, monkeypatch):
    """A release can rename or drop a tool between arming a wait and its wake;
    the resume must still run, without the stale name."""
    from app.services import wait_service

    armed = []
    monkeypatch.setattr(wait_service.scheduler, "add_job", lambda **job: armed.append(job["kwargs"]))
    llm.force_tool = ("wait", {"reason": "Retry the revenue breakdown", "delay_minutes": 5})
    rid = env["new_report"]()
    _ask(test_client, rid, env["headers"], QUESTION, disable={"tools": DISABLED})
    job = dict(armed[0])
    job["disable"] = {**job["disable"], "tools": job["disable"]["tools"] + ["tool_removed_in_a_later_release"]}

    since = len(llm.calls)
    asyncio.run(wait_service.run_wait_wake(**job))
    resumed = _main_calls(llm.calls, since)
    assert resumed, "the wake still ran a turn"
    assert not (set(DISABLED) & resumed[0]["tools"])
