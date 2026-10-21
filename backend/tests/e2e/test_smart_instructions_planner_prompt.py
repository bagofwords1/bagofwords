"""Smart (intelligent) instructions reach the agents like skills.

Contract, checked on the prompts a real completion run sends to the LLM:
  * the planner never gets a smart instruction's full text up front — every
    one is advertised in <available_instructions>; one whose title matches
    the user's message is listed first, flagged matched="title",
  * 'always' instructions are inlined regardless,
  * once the planner reads a smart instruction (read_instruction), the
    tool-less coder behind create_data gets its full text.

The LLM boundary is stubbed: it records every prompt and, for the planner,
scripts the tool calls. Routes, services, context building, the tools and
the agent loop run real.
"""
from __future__ import annotations

import re
import uuid
from pathlib import Path

import pytest

pytestmark = pytest.mark.e2e

_CHINOOK = (Path(__file__).resolve().parent.parent / "config" / "chinook.sqlite").resolve()


def _is_planner_call(tools) -> bool:
    return "read_instruction" in str(tools or "")


def _tool_results(messages) -> int:
    count = 0
    for m in messages or []:
        content = m.get("content") if isinstance(m, dict) else getattr(m, "content", None)
        if isinstance(content, list):
            count += sum(1 for b in content if isinstance(b, dict) and b.get("type") == "tool_result")
    return count


@pytest.fixture
def llm(monkeypatch):
    """Records every prompt. Planner calls follow `script` (one tool call per
    completed tool result); every other call (coder, judges) gets plain text."""
    from app.ai.llm.llm import LLM
    from app.ai.llm.types import (
        MessageStopEvent, TextDeltaEvent, ToolUseCompleteEvent, ToolUseStartEvent, UsageEvent,
    )

    # "base": tool results already in history when the current turn started
    # (reset to None between turns), so the script indexes per turn.
    state = {"planner": [], "other": [], "script": [], "base": None}

    async def scripted(self, *, model_id=None, messages, system=None, tools=None, **kwargs):
        text = f"{system}\n{messages}"
        if _is_planner_call(tools):
            state["planner"].append(text)
            seen = _tool_results(messages)
            if state["base"] is None:
                state["base"] = seen
            step = seen - state["base"]
            if step < len(state["script"]):
                name, args = state["script"][step]
                tid = f"tu_{uuid.uuid4().hex[:8]}"
                events = [ToolUseStartEvent(id=tid, name=name),
                          ToolUseCompleteEvent(id=tid, name=name, input=args),
                          MessageStopEvent(stop_reason="tool_use", raw_stop_reason="tool_use")]
            else:
                events = [TextDeltaEvent(text="Done."),
                          MessageStopEvent(stop_reason="end_turn", raw_stop_reason="end_turn")]
        else:
            state["other"].append(text)
            events = [TextDeltaEvent(text="The answer."),
                      MessageStopEvent(stop_reason="end_turn", raw_stop_reason="end_turn")]
        for e in events + [UsageEvent(input_tokens=10, output_tokens=5)]:
            yield e

    async def _stream(self, prompt, **kwargs):
        state["other"].append(str(prompt))
        yield "ok"

    def _inference(self, prompt, *a, **k):
        state["other"].append(str(prompt))
        return "[]"

    monkeypatch.setattr(LLM, "inference_stream_v2", scripted)
    monkeypatch.setattr(LLM, "inference_stream", _stream)
    monkeypatch.setattr(LLM, "inference", _inference)
    return state


@pytest.fixture
def env(test_client, create_user, login_user, whoami, create_llm_provider_and_models, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY_TEST", "dummy-local-provider-boundary")
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    create_llm_provider_and_models(user_token=token, org_id=org_id)
    return {"headers": {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)},
            "token": token, "org_id": org_id}


def _instruction(client, h, **fields):
    r = client.post("/api/instructions", headers=h, json={"status": "published", **fields})
    assert r.status_code == 200, r.text
    return r.json()


def _run(client, h, prompt, data_sources=()):
    r = client.post("/api/reports", headers=h,
                    json={"title": "smart", "files": [], "data_sources": list(data_sources)})
    assert r.status_code == 200, r.text
    rid = r.json()["id"]
    r = client.post(f"/api/reports/{rid}/completions", headers=h, json={"prompt": {"content": prompt}})
    assert r.status_code == 200, r.text
    return rid


def _catalog_block(prompt: str) -> str:
    # The system text also mentions the tag in prose; the real catalog block is
    # the one holding <instruction_ref> entries.
    blocks = [b for b in re.findall(r"<available_instructions>(.*?)</available_instructions>", prompt, re.S)
              if "<instruction_ref" in b]
    assert blocks, "planner prompt must carry the instruction catalog"
    return blocks[-1]


def test_planner_gets_catalog_only_with_matches_first(test_client, env, llm):
    h = env["headers"]
    title_marker = f"TITLE{uuid.uuid4().hex[:8]}"
    body_marker = f"BODY{uuid.uuid4().hex[:8]}"
    always_marker = f"ALWAYS{uuid.uuid4().hex[:8]}"

    by_title = _instruction(test_client, h, title="Revenue reporting", load_mode="intelligent",
                            text=f"Report revenue net of tax.\n{title_marker}")
    by_body = _instruction(test_client, h, title="Net figures", load_mode="intelligent",
                           text=f"Net figures rule.\nNet revenue always excludes refunds. {body_marker}")
    _instruction(test_client, h, title="Currency", load_mode="always", text=f"Answer in USD. {always_marker}")

    _run(test_client, h, "What was revenue last month?")

    planner = [p for p in llm["planner"] if always_marker in p]
    assert planner, "no planner call carried the organization instructions"
    prompt = planner[0]
    assert title_marker not in prompt, "a title match must not be inlined"
    assert body_marker not in prompt, "a body match must not be inlined"

    catalog = _catalog_block(prompt)
    refs = re.findall(r'<instruction_ref[^>]*>', catalog)
    ids = [re.search(r'short_id="([^"]+)"', r).group(1) for r in refs]
    assert ids.index(by_title["id"][:8]) < ids.index(by_body["id"][:8]), "title match must be listed first"
    title_ref = next(r for r in refs if by_title["id"][:8] in r)
    assert 'matched="title"' in title_ref


def test_instruction_read_earlier_in_the_report_reaches_the_coder(
    test_client, env, llm, create_data_source, refresh_schema, bulk_update_tables
):
    """Turn 1: the planner reads a smart rule. Turn 2 (same report): it runs
    create_data without re-reading — the coder must still get the rule (within
    one run the coder also sees it through past observations; across turns
    only the hand-off brings it)."""
    h = env["headers"]
    ds = create_data_source(name="chinook", type="sqlite", config={"database": str(_CHINOOK)},
                            credentials={}, user_token=env["token"], org_id=env["org_id"])
    refresh_schema(data_source_id=ds["id"], user_token=env["token"], org_id=env["org_id"])
    bulk_update_tables(data_source_id=ds["id"], action="activate", user_token=env["token"], org_id=env["org_id"])
    marker = f"CENTS{uuid.uuid4().hex[:8]}"
    unread_marker = f"UNREAD{uuid.uuid4().hex[:8]}"
    # No word shared with the question, so only the agent's read can bring
    # this rule to the coder (never a keyword match).
    rule = _instruction(test_client, h, title="Money storage", load_mode="intelligent",
                        text=f"Money storage rule.\nTotals are stored in cents; divide by 100. {marker}")
    _instruction(test_client, h, title="Shipping rule", load_mode="intelligent",
                 text=f"Shipping rule.\nShipping is free over 50. {unread_marker}")

    llm["script"] = [("read_instruction", {"id": rule["id"][:8]})]
    rid = _run(test_client, h, "How are totals stored?", data_sources=[ds["id"]])
    assert marker not in llm["planner"][0], "the rule must not be inlined up front"

    llm["other"].clear()
    llm["base"] = None
    llm["script"] = [("create_data", {
        "title": "Invoice totals", "interpreted_prompt": "Total invoice amount by country",
        "tables_by_source": [{"data_source_id": ds["id"], "tables": ["Invoice"]}],
    })]
    r = test_client.post(f"/api/reports/{rid}/completions", headers=h,
                         json={"prompt": {"content": "Now total invoice amount by country"}})
    assert r.status_code == 200, r.text

    coder = [p for p in llm["other"] if "ds_clients" in p]
    assert coder, "create_data never reached the coder"
    assert any(marker in p for p in coder), "the coder must get the rule read earlier in this report"
    assert not any(unread_marker in p for p in coder), "the coder must not get unread smart instructions"
