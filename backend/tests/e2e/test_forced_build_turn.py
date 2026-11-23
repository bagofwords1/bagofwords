"""The forced build step, through a real agent turn.

When a person asks for a page or a document and the turn is about to end with
nothing built, the agent makes one more planner call that may only build
(create_artifact / edit_artifact / create_doc) or clarify, with a tool call
required. These tests post the ask to the completions route and fake only the
LLM provider; the agent loop, the planner, the tools and the database run real.
What they check is what the provider was offered and what the person gets: a
saved page, a clarifying question, or a plain answer.

Run:
    cd backend
    TESTING=true uv run pytest tests/e2e/test_forced_build_turn.py -m e2e --db=sqlite
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from types import SimpleNamespace

import pytest

from tests.e2e import test_artifact_automatic_check as _tool_checks

stub_render = _tool_checks.stub_render

pytestmark = pytest.mark.e2e

BUILD_TOOLS = {"create_artifact", "edit_artifact", "create_doc", "edit_doc"}
PAGE_CODE = ('<script type="text/babel">function App() { return <main><h1>Revenue by month</h1>'
             '<p>Revenue was flat over the quarter.</p></main> }</script>')
CHAT_ANSWER = "Revenue was 1,200 last month."


def _page_call():
    return ("tool", "create_artifact", {"prompt": "Revenue by month", "mode": "page", "title": "Revenue",
                                        "code": PAGE_CODE, "visualization_ids": []})


class _Provider:
    """The LLM provider: answers each planner call from ``script`` and records what it was offered.

    A planner call is one that offers tools. Every other call (titles, the
    tools' own LLM) gets a short text answer.
    """

    def __init__(self):
        self.script = []
        self.offered = []          # per planner call: (sorted tool names, require_tool)
        self.on_first_call = None  # optional hook run (in a thread) before the first planner answer

    def planner_step(self, index):
        if index < len(self.script):
            return self.script[index]
        return ("text", CHAT_ANSWER)

    def stream(self):
        from app.ai.llm.types import (
            MessageStopEvent,
            TextDeltaEvent,
            ToolUseCompleteEvent,
            ToolUseStartEvent,
            UsageEvent,
        )
        provider = self

        async def scripted(self, *, model_id=None, messages, system=None, tools=None, require_tool=False, **kwargs):
            if not tools:
                yield TextDeltaEvent(text="ok")
                yield MessageStopEvent(stop_reason="end_turn", raw_stop_reason="end_turn")
                return
            index = len(provider.offered)
            provider.offered.append((sorted(t.name for t in tools), bool(require_tool)))
            if index == 0 and provider.on_first_call is not None:
                import asyncio
                await asyncio.to_thread(provider.on_first_call)
            step = provider.planner_step(index)
            if step[0] == "fail":
                raise RuntimeError(step[1])
            if step[0] == "tool":
                tid = f"tu_{uuid.uuid4().hex[:8]}"
                events = [ToolUseStartEvent(id=tid, name=step[1]),
                          ToolUseCompleteEvent(id=tid, name=step[1], input=step[2]),
                          MessageStopEvent(stop_reason="tool_use", raw_stop_reason="tool_use")]
            else:
                events = [TextDeltaEvent(text=step[1]), MessageStopEvent(stop_reason="end_turn", raw_stop_reason="end_turn")]
            for event in events + [UsageEvent(input_tokens=10, output_tokens=5)]:
                yield event

        return scripted

    def restricted_calls(self):
        """Planner calls that offered only build tools and clarify, with a tool call required."""
        return [names for names, required in self.offered
                if required and set(names) <= BUILD_TOOLS | {"clarify"}]


@pytest.fixture
def provider(monkeypatch):
    from app.ai.llm.llm import LLM

    p = _Provider()

    async def _stream(self, prompt, **kwargs):
        yield "ok"

    monkeypatch.setattr(LLM, "inference_stream_v2", p.stream())
    monkeypatch.setattr(LLM, "inference_stream", _stream)
    monkeypatch.setattr(LLM, "inference", lambda self, prompt, *a, **k: "[]")
    return p


def _headers(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


@pytest.fixture
def env(test_client, create_user, login_user, whoami, create_llm_provider_and_models, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY_TEST", "dummy-local-provider-boundary")
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    create_llm_provider_and_models(user_token=token, org_id=org_id)
    return {"h": _headers(token, org_id), "token": token, "org_id": org_id}


def _new_report(client, env):
    r = client.post("/api/reports", headers=env["h"], json={"title": f"forced-{uuid.uuid4().hex[:6]}",
                                                            "files": [], "data_sources": []})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _ask(client, env, prompt, *, model_id=None, report_id=None):
    report_id = report_id or _new_report(client, env)
    body = {"prompt": {"content": prompt}}
    if model_id:
        body["prompt"]["model_id"] = model_id
    r = client.post(f"/api/reports/{report_id}/completions", headers=env["h"], params={"background": False}, json=body)
    assert r.status_code == 200, r.text
    return report_id, _turn(client, env, report_id)


def _turn(client, env, report_id):
    r = client.get(f"/api/reports/{report_id}/completions", headers=env["h"])
    assert r.status_code == 200, r.text
    systems = [c for c in r.json()["completions"] if c["role"] == "system"]
    assert systems, "no system completion recorded"
    return systems[-1]


def _saved_pages(client, env, report_id):
    r = client.get(f"/api/artifacts/report/{report_id}", headers=env["h"])
    assert r.status_code == 200, r.text
    return [a for a in r.json() if a["status"] == "completed"]


def _saves_nothing(client, env, report_id):
    return _saved_pages(client, env, report_id) == []


def _tools_run(turn):
    return [(b.get("tool_execution") or {}).get("tool_name") for b in turn.get("completion_blocks") or []
            if b.get("tool_execution")]


def _says(turn, text):
    return text in json.dumps(turn, default=str)


# --- the forced step -------------------------------------------------------

def test_a_page_ask_answered_in_text_gets_one_build_only_call_and_the_page_is_built(
    test_client, env, provider, stub_render,
):
    provider.script = [("text", "The dashboard is built: revenue was flat."), _page_call(), ("text", "Done.")]
    report_id, turn = _ask(test_client, env, "Build a dashboard of revenue by month.")

    assert provider.restricted_calls() == [["clarify", "create_artifact", "edit_artifact"]]
    assert provider.offered[0][1] is False, "the first call is not forced"
    assert len(_saved_pages(test_client, env, report_id)) == 1, "the forced call built the page"
    assert turn["status"] != "error", turn


def test_the_forced_step_may_ask_the_person_instead_of_building(test_client, env, provider, stub_render):
    question = "Which months should the dashboard cover?"
    provider.script = [("text", "Here is the dashboard."), ("tool", "clarify", {"questions": [{"text": question}]})]
    report_id, turn = _ask(test_client, env, "Build a dashboard of revenue by month.")

    assert len(provider.restricted_calls()) == 1
    assert "clarify" in _tools_run(turn)
    assert _saves_nothing(test_client, env, report_id)


@pytest.mark.parametrize("prompt", [
    "What was revenue by country last quarter?",
    "Do not create a dashboard; answer in chat only. What was revenue last month?",
    "אל תבנה דשבורד, רק תענה: מה היו ההכנסות בחודש שעבר?",
    "Escribe la respuesta aquí, sin panel ni informe: ¿cuánto fue el ingreso del mes pasado?",
])
def test_a_question_or_a_refused_build_ends_in_the_chat_answer(prompt, test_client, env, provider, stub_render):
    provider.script = [("text", CHAT_ANSWER)]
    report_id, turn = _ask(test_client, env, prompt)

    assert len(provider.offered) == 1, "the turn ended on the text answer"
    assert provider.restricted_calls() == []
    assert _says(turn, CHAT_ANSWER)
    assert _saves_nothing(test_client, env, report_id)


def test_a_tool_that_was_not_offered_gets_one_more_forced_call(test_client, env, provider, stub_render):
    provider.script = [("text", "Here is the dashboard."),
                       ("tool", "create_data", {"prompt": "revenue by month"}),
                       _page_call(), ("text", "Done.")]
    report_id, turn = _ask(test_client, env, "Build a dashboard of revenue by month.")

    assert len(provider.restricted_calls()) == 2, "one retry after the stray tool"
    assert len(_saved_pages(test_client, env, report_id)) == 1


def test_after_two_stray_tools_the_turn_goes_on_unforced(test_client, env, provider, stub_render):
    stray = ("tool", "create_data", {"prompt": "revenue by month"})
    provider.script = [("text", "Here is the dashboard."), stray, stray, ("text", CHAT_ANSWER)]
    report_id, turn = _ask(test_client, env, "Build a dashboard of revenue by month.")

    assert len(provider.restricted_calls()) == 2
    names, required = provider.offered[3]
    assert not required and "create_data" in names, "the call after the retry is not forced"
    assert len(provider.offered) == 4, "the turn ended on the unforced answer"
    assert turn["status"] != "error", turn


def test_a_forced_call_the_provider_rejects_is_retried_unforced(test_client, env, provider, stub_render):
    rejected = "Error code: 400 - tool_choice 'required' is not supported with this model"
    provider.script = [("text", "Here is the dashboard."), ("fail", rejected), ("text", CHAT_ANSWER)]
    report_id, turn = _ask(test_client, env, "Build a dashboard of revenue by month.")

    assert len(provider.restricted_calls()) == 1, "the rejected forced call is not sent again"
    names, required = provider.offered[2]
    assert not required and "create_data" in names, "the call after the failure is not forced"
    assert len(provider.offered) == 3, "the turn ended on the unforced answer"
    assert turn["status"] != "error", turn
    assert _says(turn, CHAT_ANSWER)


# --- a steer mid-turn ------------------------------------------------------

@pytest.fixture
def live_client():
    """One client whose background run and steer requests share a server."""
    from fastapi.testclient import TestClient

    from main import app

    with TestClient(app) as client:
        yield client


def _wait_until(predicate, timeout=30.0, interval=0.3):
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(interval)
    return None


@pytest.mark.parametrize("steer,forced", [
    ("Never mind the dashboard, just answer in chat.", []),
    ("Actually make it a written report instead.", [["clarify", "create_doc"]]),
])
def test_the_latest_steer_decides_whether_the_build_is_forced(steer, forced, live_client, env, provider, stub_render):
    reached, release = threading.Event(), threading.Event()

    def hold():
        reached.set()
        release.wait(30)

    provider.on_first_call = hold
    provider.script = [("text", CHAT_ANSWER)] * 2 + [("tool", "clarify", {"questions": [{"text": "Which period?"}]})]
    report_id = _new_report(live_client, env)
    try:
        r = live_client.post(f"/api/reports/{report_id}/completions", headers=env["h"],
                             params={"background": True},
                             json={"prompt": {"content": "Build a dashboard of revenue by month."}})
        assert r.status_code == 200, r.text
        system = next(c for c in r.json()["completions"] if c["role"] == "system")
        assert reached.wait(20), "the run never reached the provider"
        r = live_client.post(f"/api/completions/{system['id']}/steer", headers=env["h"], json={"content": steer})
        assert r.status_code == 200 and r.json()["status"] == "steered", r.text
    finally:
        release.set()

    def finished():
        return _turn(live_client, env, report_id)["status"] not in ("in_progress", "queued")

    assert _wait_until(finished), "the steered run never finished"
    assert provider.restricted_calls() == forced


# --- providers that reject a forced tool_choice ----------------------------

class _AnthropicAPI:
    """The Anthropic Messages API: records each tool-offering request and answers
    text first, then a create_artifact call."""

    def __init__(self):
        self.requests = []

    def create(self):
        api = self

        def chunk(kind, **fields):
            return SimpleNamespace(type=kind, **fields)

        def text(value, stop="end_turn"):
            return [chunk("content_block_start", index=0, content_block=SimpleNamespace(type="text")),
                    chunk("content_block_delta", index=0, delta=SimpleNamespace(type="text_delta", text=value)),
                    chunk("content_block_stop", index=0),
                    chunk("message_delta", delta=SimpleNamespace(stop_reason=stop), usage=None)]

        def tool(name, args):
            block = SimpleNamespace(type="tool_use", id=f"toolu_{uuid.uuid4().hex[:8]}", name=name)
            return [chunk("content_block_start", index=0, content_block=block),
                    chunk("content_block_delta", index=0,
                          delta=SimpleNamespace(type="input_json_delta", partial_json=json.dumps(args))),
                    chunk("content_block_stop", index=0),
                    chunk("message_delta", delta=SimpleNamespace(stop_reason="tool_use"), usage=None)]

        async def fake_create(_self, **kwargs):
            if kwargs.get("tools"):
                api.requests.append(kwargs)
                chunks = (text("The dashboard is built.") if len(api.requests) == 1
                          else tool("create_artifact", _page_call()[2]) if len(api.requests) == 2
                          else text("Done."))
            else:
                chunks = text("ok")

            async def stream():
                for c in chunks:
                    yield c
            return stream()

        return fake_create

    def forced_request(self):
        return self.requests[1]


@pytest.mark.parametrize("model_id,tool_choice,thinking", [
    # Answers a forced tool_choice with HTTP 400, and cannot switch reasoning fully off.
    ("claude-sonnet-5-5", "auto", "between_tools"),
    # A required tool call is not allowed together with extended thinking.
    ("claude-haiku-4-5-20251001", "any", "disabled"),
])
def test_the_forced_call_uses_the_tool_choice_the_model_accepts(
    model_id, tool_choice, thinking, test_client, create_user, login_user, whoami, monkeypatch, stub_render,
):
    from anthropic.resources.messages import AsyncMessages

    from app.ai.llm.llm import LLM

    api = _AnthropicAPI()
    monkeypatch.setattr(AsyncMessages, "create", api.create())

    async def _stream(self, prompt, **kwargs):
        yield "ok"
    monkeypatch.setattr(LLM, "inference_stream", _stream)
    monkeypatch.setattr(LLM, "inference", lambda self, prompt, *a, **k: "[]")

    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    headers = _headers(token, org_id)
    r = test_client.post("/api/llm/providers", headers=headers, json={
        "name": "anthropic provider", "provider_type": "anthropic",
        "credentials": {"api_key": "sk-ant-test-not-used"},
        "models": [{"model_id": model_id, "name": model_id, "is_custom": False}]})
    assert r.status_code == 200, r.text
    env = {"h": headers}

    report_id, turn = _ask(test_client, env, "Build a dashboard of revenue by month.")

    assert len(api.requests) >= 2, "no forced call reached the provider"
    forced = api.forced_request()
    assert sorted(t["name"] for t in forced["tools"]) == ["clarify", "create_artifact", "edit_artifact"]
    # No tool_choice in the request is the API's default, "auto".
    sent = ((forced.get("extra_body") or {}).get("tool_choice") or {"type": "auto"})["type"]
    assert sent == tool_choice
    assert ((forced.get("extra_body") or {}).get("thinking") or {}).get("type") == thinking
    assert len(_saved_pages(test_client, env, report_id)) == 1, "the forced call built the page"
