"""A non-streaming POST /completions returns once the answer is saved.

Contract:
  * the response carries the saved answer (status success) while the run's
    post-answer work — instruction-suggestion check / knowledge harness,
    follow-ups, final snapshot, late scoring — is still going. That tail used
    to hold the HTTP response for a median 1.4 s after the answer;
  * the tail still runs to the end afterwards: the agent execution finishes;
  * a caller that explicitly asks for follow-ups (prompt.follow_ups=true)
    still gets the response only after the whole run.

The LLM boundary is stubbed; routes, services and the agent loop run real.
The tail is held open by gating the run's first post-answer step.
"""
from __future__ import annotations

import asyncio
import threading
import time

import pytest
from sqlalchemy import select

pytestmark = pytest.mark.e2e


@pytest.fixture
def live_client(monkeypatch):
    """One event loop for the whole test, so the tail is not cancelled with the request."""
    from fastapi.testclient import TestClient

    from app.services.completion_service import CompletionService
    from main import app

    monkeypatch.setattr(CompletionService, "RETURN_ON_ANSWER_ENABLED", True)
    with TestClient(app) as client:
        yield client


@pytest.fixture
def llm(monkeypatch):
    from app.ai.llm.llm import LLM
    from app.ai.llm.types import MessageStopEvent, TextDeltaEvent, UsageEvent

    async def scripted(self, *, model_id=None, messages, system=None, tools=None, **kwargs):
        for e in (TextDeltaEvent(text="The answer."), MessageStopEvent(stop_reason="end_turn", raw_stop_reason="end_turn"),
                  UsageEvent(input_tokens=10, output_tokens=5)):
            yield e

    async def _stream(self, prompt, **kwargs):
        yield "ok"

    monkeypatch.setattr(LLM, "inference_stream_v2", scripted)
    monkeypatch.setattr(LLM, "inference_stream", _stream)
    monkeypatch.setattr(LLM, "inference", lambda self, prompt, *a, **k: "[]")


@pytest.fixture
def tail_gate(monkeypatch):
    """Hold the post-answer tail open until released (or 5 s pass)."""
    from app.ai.agent_v2 import AgentV2

    gate = {"open": threading.Event(), "entered": threading.Event()}

    async def gated(self, *a, **k):
        gate["entered"].set()
        deadline = time.monotonic() + 5
        while not gate["open"].is_set() and time.monotonic() < deadline:
            await asyncio.sleep(0.02)
        gate["open"].set()
        return {"decision": False}

    monkeypatch.setattr(AgentV2, "_should_suggest_instructions", gated)
    return gate


@pytest.fixture
def env(test_client, create_user, login_user, whoami, create_llm_provider_and_models, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY_TEST", "dummy-local-provider-boundary")
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    create_llm_provider_and_models(user_token=token, org_id=org_id)
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


def _post(client, h, prompt):
    r = client.post("/api/reports", headers=h, json={"title": "nonstream", "files": [], "data_sources": []})
    assert r.status_code == 200, r.text
    rid = r.json()["id"]
    r = client.post(f"/api/reports/{rid}/completions", headers=h, json={"prompt": prompt})
    assert r.status_code == 200, r.text
    return rid, r.json()


def _system(body):
    systems = [c for c in body["completions"] if c.get("role") == "system"]
    assert systems, body
    return systems[-1]


def _text(completion) -> str:
    return " ".join(
        str(b.get("content") or "") for b in (completion.get("completion_blocks") or [])
    ) + str((completion.get("completion") or {}).get("content") or "")


async def _execution_status(system_completion_id: str):
    from app.dependencies import async_session_maker
    from app.models.agent_execution import AgentExecution

    async with async_session_maker() as s:
        res = await s.execute(select(AgentExecution.status).where(AgentExecution.completion_id == system_completion_id))
        return res.scalars().first()


def test_response_returns_with_the_answer_before_the_tail_finishes(live_client, env, llm, tail_gate):
    rid, body = _post(live_client, env, {"content": "Tell me something about the data, please."})

    released_before_return = tail_gate["open"].is_set()
    system = _system(body)
    assert not released_before_return, "the response waited for the post-answer tail"
    assert system["status"] == "success", system
    assert "The answer." in _text(system), system

    # The tail still runs to the end.
    tail_gate["open"].set()
    deadline = time.monotonic() + 15
    status = None
    while time.monotonic() < deadline:
        status = live_client.portal.call(_execution_status, system["id"]) if live_client.portal else None
        if status not in (None, "in_progress", "running"):
            break
        time.sleep(0.1)
    assert status == "success", status


def test_explicitly_requested_follow_ups_keep_the_response_waiting(live_client, env, llm, tail_gate):
    _, body = _post(live_client, env, {"content": "Tell me something about the data, please.", "follow_ups": True})
    assert tail_gate["entered"].is_set()
    assert tail_gate["open"].is_set(), "the response returned before the run finished"
    assert _system(body)["status"] == "success"
