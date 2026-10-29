"""Per-conversation API opt-outs on the report: follow_ups, suggest_instructions, tools.

An API integration sets them once on the conversation (POST / PUT
/api/reports). Contract:
  * missing or null = the org setting decides; false = off; true = on only if
    the org allows it (a report can never turn on what the org turned off);
  * every turn on the report honors them, including the turns the agent
    continues on its own (sub-agents, fan-in, wait wakes, steers), with no
    per-path forwarding;
  * tools keys are a short public list; "mcp" and "lists" switch off a group
    (gateways, their native per-tool forms, and the <mcp_tools> block);
  * a disabled tool is hidden from the planner, refused if called anyway, and
    named to the model in a <disabled_tools> line, inside the harness too;
  * the 0.0.588 per-message prompt.follow_ups / prompt.suggest_instructions
    keep working unchanged.

The LLM boundary is stubbed (``LLM.inference_stream_v2`` for planner calls,
``LLM.inference`` for small-model one-shots), plus the harness trigger verdict
(see ``harness_triggers``), the MCP provider client, and APScheduler's add_job
(to fire a wait wake on demand); routes, services, the agent loop and
persistence run real. Every planner request is recorded with the tool names it
offered and its full text, so what the model was shown is asserted at that
boundary.

Run: cd backend && uv run pytest tests/e2e/test_report_api_settings.py -q --db=sqlite
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import time
import uuid
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.e2e

HARNESS_MARKER = "running the Knowledge Harness"
FOLLOW_UPS = ["Break revenue down by region", "Compare with the same month last year"]
ANSWER = "Revenue grew 12% month over month, led by the enterprise plan."
QUESTION = "How did monthly revenue change across the last quarter?"
CORRECTION = "No, I meant net revenue instead of gross revenue for that quarter"
CLARIFY_ARGS = {"questions": [{"text": "Which revenue definition?", "options": ["gross", "net"]}]}
MCP_TOOLS = {"execute_mcp", "search_tools", "call_tool", "list_mcp_resources", "read_mcp_resource"}


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
    yet): answer in text (default), call ``force_tool`` once, or ``delegate``."""

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


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def env(test_client, create_user, login_user, whoami, create_llm_provider_and_models, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY_TEST", "dummy-local-provider-boundary")
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    create_llm_provider_and_models(user_token=token, org_id=org_id)
    headers = {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}

    def new_report(client=None, data_sources=(), **settings):
        r = (client or test_client).post("/api/reports", headers=headers, json={
            "title": "API settings", "files": [], "data_sources": list(data_sources), **settings,
        })
        assert r.status_code == 200, r.text
        return r.json()

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


def _wait_for(pred, timeout_s=120.0, what="condition"):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        found = pred()
        if found:
            return found
        time.sleep(0.5)
    raise AssertionError(f"timed out waiting for {what}")


# ─────────────────────────────────────────────────────────────────────────────
# The API contract
# ─────────────────────────────────────────────────────────────────────────────

def test_settings_are_stored_on_the_report_and_returned(test_client, env):
    report = env["new_report"](follow_ups=False, suggest_instructions=False,
                               tools={"clarify": False, "mcp": False, "delegate": None})
    assert report["follow_ups"] is False and report["suggest_instructions"] is False
    # A null entry means "not set" and is not stored.
    assert report["tools"] == {"clarify": False, "mcp": False}

    r = test_client.get(f"/api/reports/{report['id']}", headers=env["headers"])
    assert r.status_code == 200, r.text
    assert r.json()["tools"] == {"clarify": False, "mcp": False}


def test_update_leaves_omitted_fields_merges_tools_and_null_resets(test_client, env):
    report = env["new_report"](follow_ups=False, tools={"clarify": False, "mcp": False})
    url, h = f"/api/reports/{report['id']}", env["headers"]

    r = test_client.put(url, headers=h, json={"title": "Renamed"})
    assert r.status_code == 200 and r.json()["follow_ups"] is False, r.text

    r = test_client.put(url, headers=h, json={"tools": {"mcp": None, "lists": False}})
    assert r.json()["tools"] == {"clarify": False, "lists": False}

    r = test_client.put(url, headers=h, json={"follow_ups": None, "tools": None})
    assert r.json()["follow_ups"] is None and r.json()["tools"] is None


def test_a_report_without_settings_has_them_unset(env):
    report = env["new_report"]()
    assert report["follow_ups"] is None and report["suggest_instructions"] is None and report["tools"] is None


@pytest.mark.parametrize("settings, where", [
    ({"tools": {"not_a_tool": False}}, "not_a_tool"),
    ({"tools": {"execute_mcp": False}}, "execute_mcp"),  # internal name, not a public key
    ({"tools": {"finish_subtask": False}}, "finish_subtask"),
    ({"tools": {"clarify": "no"}}, "clarify"),
    ({"tools": ["clarify"]}, "tools"),
    ({"follow_ups": "sometimes"}, "follow_ups"),
    ({"follow_ups": "no"}, "follow_ups"),  # strict: no string coercion, same rule as tools values
], ids=["unknown-key", "internal-name", "internal-tool", "non-boolean-value", "list-shape", "non-boolean-flag", "string-bool"])
def test_invalid_settings_are_422(test_client, env, settings, where):
    r = test_client.post("/api/reports", headers=env["headers"],
                         json={"title": "x", "files": [], "data_sources": [], **settings})
    assert r.status_code == 422, r.text
    assert where in r.text


# ─────────────────────────────────────────────────────────────────────────────
# follow_ups / suggest_instructions
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("off", [False, True], ids=["control", "report-off"])
def test_report_follow_ups_false_applies_to_every_turn(test_client, env, llm, off):
    rid = env["new_report"](**({"follow_ups": False} if off else {}))["id"]
    h = env["headers"]
    for content in (QUESTION, "And which plan contributed most to that growth?"):
        _ask(test_client, rid, h, content)
        assert bool(_last_answer(test_client, rid, h)["follow_ups"]) is (not off)


def test_report_follow_ups_true_does_not_override_org_setting_off(
    test_client, env, llm, update_organization_settings,
):
    update_organization_settings({"enable_follow_ups": {"value": False}}, user_token=env["token"], org_id=env["org_id"])
    rid = env["new_report"](follow_ups=True)["id"]
    _ask(test_client, rid, env["headers"], QUESTION)
    assert not _last_answer(test_client, rid, env["headers"])["follow_ups"]


def test_per_message_follow_ups_false_from_0588_still_works(test_client, env, llm):
    rid = env["new_report"]()["id"]
    h = env["headers"]
    _ask(test_client, rid, h, QUESTION, follow_ups=False)
    assert not _last_answer(test_client, rid, h)["follow_ups"]
    _ask(test_client, rid, h, "Show the same breakdown for the previous quarter")
    assert _last_answer(test_client, rid, h)["follow_ups"], "per message: the next turn is unaffected"


@pytest.fixture
def harness_triggers(monkeypatch):
    """Every harness trigger needs a successful create_data in the turn, which
    the API can't produce without a real code-gen LLM and sandbox in the loop.
    Stand in for the trigger verdict only: the harness itself, and the gates in
    front of the verdict, run real."""
    from app.ai.agents.suggest_instructions import InstructionTriggerEvaluator

    async def fired(self, prev_tool_name_before_last_user=None):
        return {
            "decision": True,
            "conditions": [{"name": "user_explicit_correction", "hint": "User corrected the metric."}],
            "session_maturity": "training",
        }

    monkeypatch.setattr(InstructionTriggerEvaluator, "evaluate", fired)


@pytest.mark.parametrize("where", ["control", "report", "message"])
def test_suggest_instructions_false_skips_the_harness(test_client, env, llm, harness_triggers, where):
    rid = env["new_report"](**({"suggest_instructions": False} if where == "report" else {}))["id"]
    since = len(llm.calls)
    fields = {"suggest_instructions": False} if where == "message" else {}
    _ask(test_client, rid, env["headers"], CORRECTION, **fields)
    assert any(c["kind"] == "harness" for c in llm.calls[since:]) is (where == "control")


# ─────────────────────────────────────────────────────────────────────────────
# tools
# ─────────────────────────────────────────────────────────────────────────────

def test_disabled_tools_are_not_offered_and_are_named_in_the_prompt(test_client, env, llm):
    h = env["headers"]
    _ask(test_client, env["new_report"]()["id"], h, QUESTION)
    control = _main_calls(llm.calls)[0]
    assert {"clarify", "delegate"} <= control["tools"], "control: offered by default"
    assert "<disabled_tools>" not in control["text"]

    since = len(llm.calls)
    _ask(test_client, env["new_report"](tools={"clarify": False, "delegate": False})["id"], h, QUESTION)
    call = _main_calls(llm.calls, since)[0]
    # Switching off delegate also hides the tools that only act on sub-agents.
    gone = {"clarify", "delegate", "read_sub_agent", "stop_sub_agent", "message_sub_agent"}
    assert not (gone & call["tools"])
    assert control["tools"] - gone <= call["tools"], "nothing else was taken away"
    assert "<disabled_tools>" in call["text"] and "clarify" in call["text"]
    assert "<disabled_tools>" not in call["system"], "per-conversation text stays out of the cached system prompt"
    # A switched-off tool must not keep its prompt directives either.
    assert "DELEGATION" in control["system"] and "DELEGATION" not in call["system"]


def test_web_fetch_off_removes_its_prompt_directives(test_client, env, llm, update_organization_settings):
    update_organization_settings({"enable_web_fetch": {"value": True}}, user_token=env["token"], org_id=env["org_id"])
    h = env["headers"]
    _ask(test_client, env["new_report"]()["id"], h, QUESTION)
    control = _main_calls(llm.calls)[0]
    assert "web_fetch" in control["tools"] and "web_fetch is enabled" in control["system"]

    since = len(llm.calls)
    _ask(test_client, env["new_report"](tools={"web_fetch": False})["id"], h, QUESTION)
    call = _main_calls(llm.calls, since)[0]
    assert "web_fetch" not in call["tools"] and "web_fetch is enabled" not in call["system"]


@pytest.mark.parametrize("off", [False, True], ids=["control", "notify-off"])
def test_no_check_in_is_planned_when_notify_is_off(test_client, env, llm, monkeypatch, off):
    """A check-in reaches the user only through notify; with notify off it
    could never deliver, so the turn must not plan one. The planning entry
    point is recorded instead of run (it is a background LLM task)."""
    from app.services.checkin_service import checkin_service

    planned = []

    def record(**kwargs):
        planned.append(kwargs)

        async def noop():
            return None
        return noop()

    monkeypatch.setattr(checkin_service, "dispatch_after_turn", record)
    rid = env["new_report"](**({"tools": {"notify": False}} if off else {}))["id"]
    _ask(test_client, rid, env["headers"], QUESTION)
    assert bool(planned) is (not off)


@pytest.mark.parametrize("off", [False, True], ids=["control", "disabled"])
def test_disabled_tool_called_anyway_is_refused_not_run(test_client, env, llm, off):
    """The model may still name a hidden tool (the policy prompt mentions
    clarify by name). With clarify off the call is refused before it runs and
    the loop goes on to an answer; without it the same call runs."""
    llm.force_tool = ("clarify", CLARIFY_ARGS)
    rid = env["new_report"](**({"tools": {"clarify": False}} if off else {}))["id"]
    since = len(llm.calls)
    completions = _ask(test_client, rid, env["headers"], QUESTION)

    system = [c for c in completions if c["role"] == "system"][-1]
    clarify_ran = any(
        (b.get("tool_execution") or {}).get("tool_name") == "clarify"
        and (b.get("tool_execution") or {}).get("status") == "success"
        for b in (system.get("completion_blocks") or [])
    )
    assert clarify_ran is (not off)
    if off:
        later = _main_calls(llm.calls, since)[1:]
        assert later and "disabled" in later[0]["text"].lower(), "the refusal reached the model"


@pytest.mark.parametrize("off", [False, True], ids=["control", "disabled"])
def test_disabled_tools_also_apply_inside_the_harness(test_client, env, llm, harness_triggers, off):
    # inspect_data is a harness tool (it verifies facts), so the conversation's
    # switch must reach it too.
    rid = env["new_report"](**({"tools": {"inspect_data": False}} if off else {}))["id"]
    since = len(llm.calls)
    _ask(test_client, rid, env["headers"], CORRECTION)
    harness = [c for c in llm.calls[since:] if c["kind"] == "harness"]
    assert harness, "the harness ran"
    assert ("inspect_data" in harness[0]["tools"]) is (not off)
    assert ("<disabled_tools>" in harness[0]["text"]) is off


# ── "mcp" and "lists" switch off a whole group ───────────────────────────────

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
    return {"ds_id": str(ds["id"])}


def _is_mcp(name):
    return name in MCP_TOOLS or name.startswith("mcp__")


def _is_list_tool(name):
    return name == "submit_list" or name.startswith("submit_")


def _turn_on(test_client, env, llm, ds_id, **settings):
    since = len(llm.calls)
    with _mock_mcp_provider():
        _ask(test_client, env["new_report"](data_sources=[ds_id], **settings)["id"], env["headers"], QUESTION)
    return _main_calls(llm.calls, since)[0]


@pytest.mark.parametrize("group, hidden, kept", [
    ("mcp", _is_mcp, _is_list_tool),
    ("lists", _is_list_tool, _is_mcp),
], ids=["mcp", "lists"])
def test_group_key_switches_off_the_whole_group(test_client, env, llm, tool_agent, group, hidden, kept):
    control = _turn_on(test_client, env, llm, tool_agent["ds_id"])
    assert any(hidden(n) for n in control["tools"]) and any(kept(n) for n in control["tools"]), sorted(control["tools"])
    if group == "mcp":
        assert any(n.startswith("mcp__") for n in control["tools"]) and "<mcp_tools" in control["text"]

    call = _turn_on(test_client, env, llm, tool_agent["ds_id"], tools={group: False})
    assert not any(hidden(n) for n in call["tools"])
    assert any(kept(n) for n in call["tools"])
    if group == "mcp":
        assert "<mcp_tools" not in call["text"], "the schema context stops advertising MCP tools too"


def test_mcp_off_on_one_conversation_does_not_leak_into_another(test_client, env, llm, tool_agent):
    """Schemas are cached process-wide for minutes. A conversation that hid its
    MCP tools must not hand that trimmed schema to another conversation."""
    _turn_on(test_client, env, llm, tool_agent["ds_id"], tools={"mcp": False})
    call = _turn_on(test_client, env, llm, tool_agent["ds_id"])
    # MCP tools come back as native tools; the execute_mcp gateway is never offered.
    assert any(n.startswith("mcp__") for n in call["tools"]) and "execute_mcp" not in call["tools"]
    assert "<mcp_tools" in call["text"]


# ─────────────────────────────────────────────────────────────────────────────
# Turns the agent continues on its own read the same report
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def live_client():
    """Keeps one event loop for the whole test, so background runs the request
    spawns (sub-agents, fan-in, a queued steer) are not cancelled."""
    from fastapi.testclient import TestClient
    from main import app

    with TestClient(app) as client:
        yield client


def _run_wave(client, env, llm, **settings):
    since = len(llm.calls)
    rid = env["new_report"](client=client, **settings)["id"]
    _ask(client, rid, env["headers"], "Why did revenue drop in September?")
    child = _wait_for(lambda: next((c for c in llm.calls[since:] if c["kind"] == "child"), None), what="child run")
    fan_in = _wait_for(lambda: next((c for c in llm.calls[since:] if c["kind"] == "fan_in"), None), what="fan-in turn")
    return child, fan_in


def test_sub_agents_and_fan_in_honor_the_report(live_client, env, llm):
    llm.delegate = True
    child, fan_in = _run_wave(live_client, env, llm)
    assert "web_fetch" in child["tools"] and "web_fetch" in fan_in["tools"], "control: offered to both"

    child, fan_in = _run_wave(live_client, env, llm, tools={"web_fetch": False})
    assert "web_fetch" not in child["tools"] and "web_fetch" not in fan_in["tools"]
    assert "finish_subtask" in child["tools"], "the child can still finish"


@pytest.mark.parametrize("off", [False, True], ids=["control", "disabled"])
def test_wait_resume_honors_the_report(test_client, env, llm, monkeypatch, off):
    from app.services import wait_service

    armed = []
    monkeypatch.setattr(wait_service.scheduler, "add_job", lambda **job: armed.append(job["kwargs"]))
    llm.force_tool = ("wait", {"reason": "Retry the revenue breakdown", "delay_minutes": 5})
    rid = env["new_report"](**({"tools": {"clarify": False}} if off else {}))["id"]
    _ask(test_client, rid, env["headers"], QUESTION)
    assert armed, "the wait was armed"

    since = len(llm.calls)
    asyncio.run(wait_service.run_wait_wake(**armed[0]))  # the scheduler firing
    resumed = _main_calls(llm.calls, since)
    assert resumed, "the wake ran a turn"
    assert ("clarify" in resumed[0]["tools"]) is (not off)


@pytest.mark.parametrize("off", [False, True], ids=["control", "disabled"])
def test_steer_after_the_run_ended_honors_the_report(live_client, env, llm, off):
    h = env["headers"]
    rid = env["new_report"](client=live_client, **({"tools": {"clarify": False}} if off else {}))["id"]
    completions = _ask(live_client, rid, h, QUESTION)
    system_id = [c for c in completions if c["role"] == "system"][-1]["id"]

    since = len(llm.calls)
    r = live_client.post(f"/api/completions/{system_id}/steer", json={"content": "Also split it by region"}, headers=h)
    assert r.status_code == 200 and r.json().get("status") == "queued", r.text
    call = _wait_for(lambda: next(iter(_main_calls(llm.calls, since)), None), what="steered turn")
    assert ("clarify" in call["tools"]) is (not off)
