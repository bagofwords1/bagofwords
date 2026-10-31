"""The automatic artifact check inside a real agent run.

The LLM boundary is a scripted planner (as in ``test_agent_context_refresh``)
and Chromium is the fake beneath the real session manager from
``test_artifact_automatic_check``. Routes, the agent loop, the tool runner,
create_artifact, the guarded browser tools and the policy run real.
"""
from __future__ import annotations

import json
import re
import uuid

import pytest

from tests.e2e import test_artifact_automatic_check as _tool_checks

# Shared fixtures: the fake Chromium beneath the real session manager, and the
# headless render-validation stub.
chromium = _tool_checks.chromium
stub_render = _tool_checks.stub_render

pytestmark = pytest.mark.e2e

FORBIDDEN = ("saved and working", "nothing is broken", "never describe the artifact as failed or broken",
             "it is intact")


def _interactive(i):
    return (f'<script type="text/babel">function App() {{ const [n, setN] = React.useState({i}); '
            f'return <main><h1>Explorer {i}</h1><button onClick={{() => setN(n + {i})}}>Go {{n}}</button></main> }}</script>')


def _tool_results(messages):
    found = []
    for message in messages or []:
        content = message.get("content") if isinstance(message, dict) else getattr(message, "content", None)
        for block in content if isinstance(content, list) else []:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                found.append(block)
    return found


def _text(block):
    content = block.get("content")
    return content if isinstance(content, str) else json.dumps(content, default=str)


class _Planner:
    """Issues the scripted tool calls one per planner step, then answers."""

    def __init__(self):
        self.calls = []
        self.messages = []

    def stream(self):
        from app.ai.llm.types import (
            MessageStopEvent,
            TextDeltaEvent,
            ToolUseCompleteEvent,
            ToolUseStartEvent,
            UsageEvent,
        )
        planner = self

        async def scripted(self, *, model_id=None, messages, system=None, tools=None, **kwargs):
            planner.messages.append(list(messages or []))
            step = len(_tool_results(messages))
            if step < len(planner.calls):
                name, args = planner.calls[step]
                if name == "edit_artifact":
                    args = _edit_input(messages, args)
                tid = f"tu_{uuid.uuid4().hex[:8]}"
                events = [ToolUseStartEvent(id=tid, name=name), ToolUseCompleteEvent(id=tid, name=name, input=args),
                          MessageStopEvent(stop_reason="tool_use", raw_stop_reason="tool_use")]
            else:
                events = [TextDeltaEvent(text="Done."), MessageStopEvent(stop_reason="end_turn", raw_stop_reason="end_turn")]
            for event in events + [UsageEvent(input_tokens=10, output_tokens=5)]:
                yield event

        return scripted


@pytest.fixture
def planner(monkeypatch):
    from app.ai.llm.llm import LLM

    p = _Planner()

    async def _stream(self, prompt, **kwargs):
        yield "ok"

    monkeypatch.setattr(LLM, "inference_stream_v2", p.stream())
    monkeypatch.setattr(LLM, "inference_stream", _stream)
    monkeypatch.setattr(LLM, "inference", lambda self, prompt, *a, **k: "[]")
    return p


@pytest.fixture
def env(test_client, create_user, login_user, whoami, create_llm_provider_and_models,
        update_organization_settings, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY_TEST", "dummy-local-provider-boundary")
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    create_llm_provider_and_models(user_token=token, org_id=org_id)
    update_organization_settings({"enable_artifact_verification": True, "allow_llm_see_data": True},
                                 user_token=token, org_id=org_id)
    headers = {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}
    return {"h": headers}


def _ask(test_client, env, prompt):
    r = test_client.post("/api/reports", headers=env["h"], json={"title": "autocheck", "files": [], "data_sources": []})
    assert r.status_code == 200, r.text
    report_id = r.json()["id"]
    r = test_client.post(f"/api/reports/{report_id}/completions", headers=env["h"],
                         params={"background": False}, json={"prompt": {"content": prompt}})
    assert r.status_code == 200, r.text
    r = test_client.get(f"/api/reports/{report_id}/completions", headers=env["h"])
    assert r.status_code == 200, r.text
    systems = [c for c in r.json()["completions"] if c["role"] == "system"]
    assert systems, "no system completion recorded"
    return systems[-1]


def _edit_input(messages, k):
    """A functional edit of the latest saved version named in the tool results."""
    ids = re.findall(r'"artifact_id": "([0-9a-f-]{36})"', " ".join(_text(b) for b in _tool_results(messages)))
    assert ids, "no saved artifact in the tool results"
    return {"artifact_id": ids[-1], "title": f"Explorer tune {k}",
            "edits": [{"find": f"setN(n + {k})", "replace": f"setN(n + {k + 1})"}]}


def _create_call(i):
    return ("create_artifact", {"prompt": f"Build explorer {i}", "mode": "page", "title": f"Explorer {i}",
                                "code": _interactive(i), "visualization_ids": []})


def test_check_result_reaches_the_planner_in_the_same_run(test_client, env, planner, chromium, stub_render):
    chromium.behavior = "crash_on_load"
    planner.calls = [_create_call(1)]
    answer = _ask(test_client, env, "Build an explorer app.")

    assert len(planner.messages) >= 2, "the planner was not called after create_artifact"
    results = _tool_results(planner.messages[1])
    assert results, "no tool_result reached the planner"
    observed = _text(results[-1])
    assert "automatic_check" in observed
    assert "problems_found" in observed
    assert len(chromium.navigations) == 1
    assert answer.get("status") != "error", answer


@pytest.mark.parametrize("behavior", ["crash_on_load", "healthy"])
def test_budget_exhausted_answer_is_honest_about_check_results(
    behavior, test_client, env, planner, chromium, stub_render,
):
    chromium.behavior = behavior
    # One create and five functional edits: the budget (4 per turn) refuses
    # the last two. Edits record the saved version, so today's wording names
    # "vN is saved and working".
    planner.calls = [_create_call(1)] + [("edit_artifact", k) for k in range(1, 6)]
    answer = _ask(test_client, env, "Build an explorer app and tune it five times.")

    assert len(chromium.navigations) == 4  # the 5th and 6th calls are refused before dispatch
    refusal = next(block for block in _tool_results(planner.messages[-1])
                   if "artifact_budget_exhausted" in _text(block))
    refusal_text, final_text = _text(refusal).lower(), json.dumps(answer, default=str).lower()
    if behavior == "crash_on_load":
        for phrase in FORBIDDEN:
            assert phrase not in refusal_text, phrase
            assert phrase not in final_text, phrase
        assert "automatic check found" in final_text
    else:
        assert "saved and working" in final_text
        assert "nothing is broken and nothing was lost" in final_text


def _refusal_and_answer(planner, answer):
    refusal = next(block for block in _tool_results(planner.messages[-1])
                   if "artifact_budget_exhausted" in _text(block))
    return _text(refusal).lower(), json.dumps(answer, default=str).lower()


def test_a_later_unavailable_check_does_not_clear_recorded_problems(
    test_client, env, planner, chromium, stub_render,
):
    # The create crashes on load; the next check cannot reach the preview
    # (unavailable, which trips the breaker), and every later check is skipped.
    # None of those confirms a fix, so the refusal must not call it working.
    gotos = {"n": 0}

    async def first_crash_then_outage(_page):
        gotos["n"] += 1
        chromium.behavior = "crash_on_load" if gotos["n"] == 1 else "nav_refused"

    chromium.on_goto = first_crash_then_outage
    planner.calls = [_create_call(1)] + [("edit_artifact", k) for k in range(1, 6)]
    answer = _ask(test_client, env, "Build an explorer app and tune it five times.")

    assert gotos["n"] == 2  # the breaker skipped the remaining checks
    refusal_text, final_text = _refusal_and_answer(planner, answer)
    for phrase in FORBIDDEN:
        assert phrase not in refusal_text, phrase
        assert phrase not in final_text, phrase
    assert "not confirmed fixed" in refusal_text
    assert "automatic check found" in final_text


def _chat(i):
    return ('<script type="text/babel">function App() { const [q, setQ] = React.useState(""); '
            f'const [n, setN] = React.useState({i}); '
            'const ask = async () => { const c = await bow.conversations.create({}); await c.send(q); }; '
            f'return <main><h1>Chat {i}</h1><input value={{q}} onChange={{(e) => setQ(e.target.value)}} />'
            f'<button onClick={{() => setN(n + {i})}}>Go {{n}}</button>'
            '<button onClick={ask}>Send</button></main> }</script>')


def test_a_chat_that_never_replies_is_not_called_working(test_client, env, planner, chromium, stub_render):
    chromium.behavior, chromium.snapshot = "healthy", _tool_checks.CHAT_SNAPSHOT
    planner.calls = [("create_artifact", {"prompt": "Build chat", "mode": "page", "title": "Chat 1",
                                          "code": _chat(1), "visualization_ids": []})] + [
        ("edit_artifact", k) for k in range(1, 6)]
    answer = _ask(test_client, env, "Build a chat app and tune it five times.")

    first = _text(_tool_results(planner.messages[1])[-1])
    assert "not_verified" in first and "no_reply" in first
    refusal_text, final_text = _refusal_and_answer(planner, answer)
    for phrase in FORBIDDEN:
        assert phrase not in refusal_text, phrase
        assert phrase not in final_text, phrase
