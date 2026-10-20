"""Sub-agents writing into the parent's Agent Lists (docs/design/sub-agents.md A6).

Contract:
- A ``validate`` child holds the parent's ``submit_<list>`` tools; its rows land
  in the list with the child's completion as provenance.
- An ``investigate`` child holds them only when the org allows children to
  create data (``sub_agent_allow_create_data``); with the default (off) its
  catalog never advertises the tool, so it cannot even attempt the call.
- Children never hold ``create_list`` / ``edit_list``.

Only the LLM boundary is scripted; routes, the agent loop, the wave tracker,
list registration and the submit gateway run real.

Run: cd backend && uv run pytest tests/e2e/rbac/test_sub_agent_list_submit.py -q --db=sqlite
"""
from __future__ import annotations

import re
import time
import uuid

import pytest

pytestmark = pytest.mark.e2e


def _h(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


def _schema():
    return {"name": "Contracts", "description": "Customer contracts", "key_field": "counterparty", "fields": [
        {"name": "counterparty", "type": "string", "required": True},
        {"name": "annual_value", "type": "number"},
        {"name": "currency", "type": "enum", "enum": ["USD", "EUR"]},
    ]}


def _env(v):
    return {"value": v, "status": "found", "evidence": [], "note": None}


def _all_text(messages) -> str:
    out = []
    for m in messages or []:
        c = getattr(m, "content", None) if not isinstance(m, dict) else m.get("content")
        if isinstance(c, str):
            out.append(c)
        elif isinstance(c, list):
            for b in c:
                if isinstance(b, dict):
                    if isinstance(b.get("text"), str):
                        out.append(b["text"])
                    cc = b.get("content")
                    if isinstance(cc, str):
                        out.append(cc)
                    elif isinstance(cc, list):
                        out.extend(x["text"] for x in cc if isinstance(x, dict) and isinstance(x.get("text"), str))
    return "\n".join(out)


def _n_results(messages) -> int:
    n = 0
    for m in messages or []:
        c = getattr(m, "content", None) if not isinstance(m, dict) else m.get("content")
        if isinstance(c, list):
            n += sum(1 for b in c if isinstance(b, dict) and b.get("type") == "tool_result")
    return n


def make_scripted_stream(*, profile: str, calls: list):
    """Parent: one delegate call with ``profile``, then a closing line.
    Child: submit one contract when a submit_contracts tool is offered, then
    finish; without the tool, finish as blocked. Fan-in: a sentence."""
    from app.ai.llm.types import (
        MessageStopEvent, TextDeltaEvent, ToolUseCompleteEvent, ToolUseStartEvent, UsageEvent,
    )

    def _tool(name, args):
        tid = f"tu_{uuid.uuid4().hex[:8]}"
        return [ToolUseStartEvent(id=tid, name=name), ToolUseCompleteEvent(id=tid, name=name, input=args)]

    def _end(reason="end_turn"):
        return [MessageStopEvent(stop_reason=reason, raw_stop_reason=reason), UsageEvent(input_tokens=100, output_tokens=20)]

    async def scripted(self, *, model_id=None, messages, system=None, tools=None, **kwargs):
        sys_text = system or ""
        text = _all_text(messages)
        n = _n_results(messages)
        names = {getattr(t, "name", None) or (t.get("name") if isinstance(t, dict) else None) for t in (tools or [])}
        names.discard(None)
        child = "Mode: Sub-agent" in sys_text
        calls.append({"child": child, "tools": sorted(names), "n_results": n})
        events = []
        if child:
            submit = next((x for x in names if x.startswith("submit_contracts")), None)
            if submit and n == 0:
                events += _tool(submit, {"records": [{"row_id": None, "fields": {
                    "counterparty": _env("Acme Ltd"), "annual_value": _env(120000), "currency": _env("USD")}}]}) + _end("tool_use")
            else:
                saved = re.search(r'"inserted"\s*:\s*(\d+)', text)
                events += _tool("finish_subtask", {
                    "outcome": "done" if submit else "blocked",
                    "confidence": 0.9 if submit else 0.3,
                    "summary": f"Saved {saved.group(1) if saved else 0} contract row(s)." if submit else "No list tool offered; nothing saved.",
                    "findings": [], "ruled_out": [], "evidence": [], "caveats": [],
                    "unknown": None if submit else "a submit_contracts tool",
                }) + _end("tool_use")
        elif "<sub_agent_results" in text:
            events += [TextDeltaEvent(text="The extraction child finished.")] + _end()
        elif "delegate" in names and n == 0:
            events += _tool("delegate", {
                "goal": "Extract the Acme contract into the Contracts list and report the saved row count.",
                "context": "The list's key is counterparty.", "title": "Extract Acme", "profile": profile,
            }) + _end("tool_use")
        elif "delegate" in names:
            events += [TextDeltaEvent(text="Launched one extraction sub-agent; I will report back.")] + _end()
        else:
            events += [TextDeltaEvent(text="ok")] + _end()
        for e in events:
            yield e

    return scripted


async def _plain_text_stream(self, prompt, **kwargs):
    yield "Contract extraction"


def _plain_text(self, prompt, *args, **kwargs):
    return "ok"


@pytest.fixture
def live_client():
    """One event loop for the whole test, so the background wave survives the request (see test_sub_agents.py)."""
    from fastapi.testclient import TestClient
    from main import app
    with TestClient(app) as client:
        yield client


@pytest.fixture
def world(bootstrap_admin, sqlite_data_source, create_report, create_llm_provider_and_models, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY_TEST", "dummy-local-provider-boundary")
    admin = bootstrap_admin("admin")
    org_id = admin["org_id"]
    create_llm_provider_and_models(user_token=admin["token"], org_id=org_id)
    agent = sqlite_data_source(name=f"Legal {uuid.uuid4().hex[:4]}", user_token=admin["token"], org_id=org_id)
    return {"org_id": org_id, "admin": admin, "agent": agent, "h": _h(admin["token"], org_id),
            "report": create_report(title="Extraction", user_token=admin["token"], org_id=org_id, data_sources=[agent["id"]])}


def _wave(client, rid, h, timeout_s=180.0):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        r = client.get(f"/api/reports/{rid}/sub-agents", headers=h)
        assert r.status_code == 200, r.text
        d = r.json()
        if d["waves"] and d["waves"][-1]["status"] in ("done", "timed_out", "stopped"):
            return d["waves"][-1]
        time.sleep(1)
    raise AssertionError("wave did not fan in")


def _run(client, world, monkeypatch, *, profile: str):
    from app.ai.llm.llm import LLM
    calls: list = []
    monkeypatch.setattr(LLM, "inference_stream_v2", make_scripted_stream(profile=profile, calls=calls))
    monkeypatch.setattr(LLM, "inference_stream", _plain_text_stream)
    monkeypatch.setattr(LLM, "inference", _plain_text)
    r = client.post(f"/api/data_sources/{world['agent']['id']}/lists", json=_schema(), headers=world["h"])
    assert r.status_code == 201, r.text
    lst = r.json()
    rid = world["report"]["id"]
    r = client.post(f"/api/reports/{rid}/completions", params={"background": False}, headers=world["h"],
                    json={"prompt": {"content": "Extract the Acme contract into the Contracts list.", "widget_id": None,
                                     "step_id": None, "mentions": [{}]}})
    assert r.status_code == 200, r.text
    wave = _wave(client, rid, world["h"])
    assert wave["status"] == "done" and wave["total"] == 1, wave
    child = wave["sub_agents"][0]
    detail = client.get(f"/api/reports/{rid}/sub-agents/{child['sub_agent_id']}", headers=world["h"]).json()
    rows = client.get(f"/api/data_sources/{world['agent']['id']}/lists/{lst['id']}/rows", headers=world["h"]).json()["rows"]
    child_calls = [c for c in calls if c["child"]]
    assert child_calls, calls
    return child, detail, rows, child_calls


def test_validate_child_submits_rows_into_the_parents_list(live_client, world, monkeypatch):
    child, detail, rows, child_calls = _run(live_client, world, monkeypatch, profile="validate")
    # The child's catalog carried the list tool, and never the schema tools.
    assert any(t.startswith("submit_contracts") for t in child_calls[0]["tools"]), child_calls[0]["tools"]
    assert not {"create_list", "edit_list", "delegate", "clarify"} & set(child_calls[0]["tools"])
    assert child["status"] == "finished" and child["exit_reason"] == "finish_call" and child["outcome"] == "done", child
    assert "1 contract row" in (detail["record"]["summary"] or "")
    tool_names = [c["tool"] for c in detail["calls"]]
    assert tool_names[0] in ("submit_list", "submit_contracts") and tool_names[-1] == "finish_subtask", tool_names
    # The row exists, keyed, with the child's run as provenance (report + the child's completion).
    assert len(rows) == 1 and rows[0]["key_value"] == "acme ltd"  # keys are normalized (casefolded)
    assert rows[0]["report_id"] == world["report"]["id"]


def test_investigate_child_without_create_data_never_sees_the_list_tool(live_client, world, monkeypatch):
    child, detail, rows, child_calls = _run(live_client, world, monkeypatch, profile="investigate")
    assert not any(t.startswith("submit_") for c in child_calls for t in c["tools"]), child_calls[0]["tools"]
    assert child["status"] == "finished" and child["outcome"] == "blocked", child
    assert [c["tool"] for c in detail["calls"]] == ["finish_subtask"]
    assert rows == []
