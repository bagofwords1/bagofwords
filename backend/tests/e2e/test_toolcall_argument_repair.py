"""A tool call whose arguments a parser split apart still runs as the model meant it.

Gemma 4 served by vLLM (``--tool-call-parser gemma4``) sometimes quotes a
tool-call string without its ``<|"|>`` delimiter. The parser then returns
valid JSON with the next member glued onto that string under a garbage key,
so required fields go missing and the call fails validation.

Contract, end to end through the real agent loop: the tool runs with the
repaired arguments, the persisted ToolExecution row holds those same
arguments, and the transcript replayed to the model on the next step carries
them too — never the damaged form.

Only the LLM boundary is scripted; routes, the agent loop, the tool and the
database run real.

Run: cd backend && TESTING=true uv run pytest tests/e2e/test_toolcall_argument_repair.py -m e2e --db=sqlite
"""
from __future__ import annotations

import asyncio
import json
import uuid

import pytest

pytestmark = pytest.mark.e2e

D = '<|"|>'


def _h(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


def _tool_results(messages) -> int:
    n = 0
    for m in messages or []:
        c = getattr(m, "content", None) if not isinstance(m, dict) else m.get("content")
        if isinstance(c, list):
            n += sum(1 for b in c if isinstance(b, dict) and b.get("type") == "tool_result")
    return n


def _scripted(damaged_args: dict, seen: list):
    """First step: one create_memory call with ``damaged_args`` as the parser
    returned them. Next step: a closing sentence. Records each step's input."""
    from app.ai.llm.types import (
        MessageStopEvent, TextDeltaEvent, ToolUseCompleteEvent, ToolUseStartEvent, UsageEvent,
    )

    async def stream(self, *, model_id=None, messages, system=None, tools=None, **kwargs):
        seen.append({"messages": json.loads(json.dumps(messages, default=str)), "results": _tool_results(messages)})
        if _tool_results(messages) == 0:
            tid = f"tu_{uuid.uuid4().hex[:8]}"
            events = [ToolUseStartEvent(id=tid, name="create_memory"),
                      ToolUseCompleteEvent(id=tid, name="create_memory", input=damaged_args),
                      MessageStopEvent(stop_reason="tool_use", raw_stop_reason="tool_use")]
        else:
            events = [TextDeltaEvent(text="Noted."), MessageStopEvent(stop_reason="end_turn", raw_stop_reason="end_turn")]
        for e in events + [UsageEvent(input_tokens=100, output_tokens=20)]:
            yield e

    return stream


async def _plain_text_stream(self, prompt, **kwargs):
    yield "Memory"


def _plain_text(self, prompt, *args, **kwargs):
    return "ok"


@pytest.fixture
def member(monkeypatch, create_user, login_user, whoami, create_llm_provider_and_models, create_report):
    monkeypatch.setenv("OPENAI_API_KEY_TEST", "dummy-local-provider-boundary")
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    create_llm_provider_and_models(token, org_id)
    report = create_report(title="Repair", user_token=token, org_id=org_id, data_sources=[])
    return {"token": token, "org_id": org_id, "report": report, "h": _h(token, org_id)}


async def _stored_arguments(report_id: str, tool_name: str) -> list:
    # Read-only check of the persisted row: no API returns a tool call's raw
    # arguments (the timeline hides memory text by design).
    from sqlalchemy import select
    from app.dependencies import async_session_maker
    from app.models.agent_execution import AgentExecution
    from app.models.tool_execution import ToolExecution

    async with async_session_maker() as db:
        rows = (await db.execute(
            select(ToolExecution.arguments_json)
            .join(AgentExecution, AgentExecution.id == ToolExecution.agent_execution_id)
            .where(AgentExecution.report_id == report_id, ToolExecution.tool_name == tool_name)
        )).scalars().all()
    return list(rows)


@pytest.mark.parametrize("damaged, expected", [
    # text closed with a backtick: the title key vanished, its value glued onto "tags".
    ({"text": "Leads the Q3 churn project`,title:", "Noting your project" + D + ",tags": ["q3-churn"]},
     {"text": "Leads the Q3 churn project", "title": "Noting your project", "tags": ["q3-churn"]}),
    # title closed with a backtick: "text" swallowed into the next key.
    ({"tags": ["board"], "title": "Saving it`,text:", "Presents to the board monthly" + D + ",date": None},
     {"tags": ["board"], "title": "Saving it", "text": "Presents to the board monthly", "date": None}),
])
def test_a_split_tool_call_runs_and_is_recorded_as_the_model_meant_it(
    test_client, member, monkeypatch, damaged, expected,
):
    from app.ai.llm.llm import LLM

    seen: list = []
    monkeypatch.setattr(LLM, "inference_stream_v2", _scripted(damaged, seen))
    monkeypatch.setattr(LLM, "inference_stream", _plain_text_stream)
    monkeypatch.setattr(LLM, "inference", _plain_text)

    rid = member["report"]["id"]
    r = test_client.post(f"/api/reports/{rid}/completions", params={"background": False}, headers=member["h"],
                         json={"prompt": {"content": "Remember this about me.", "mentions": [{}]}})
    assert r.status_code == 200, r.text

    # The tool ran with every required field: the fact is saved.
    entries = test_client.get("/api/users/me/memory", headers=member["h"]).json()["entries"]
    assert [e["text"] for e in entries] == [expected["text"]], entries

    # The persisted row holds the repaired arguments.
    [stored] = asyncio.run(_stored_arguments(rid, "create_memory"))
    assert not any(D in k for k in stored), stored
    for key, value in expected.items():
        assert stored.get(key) == value, (key, stored)

    # The next step's transcript replays the repaired call, not the damage.
    later = [s for s in seen if s["results"] > 0]
    assert later, "the model was never called again after the tool ran"
    replay = json.dumps(later[0]["messages"], ensure_ascii=False)
    assert D not in replay and json.dumps(expected["text"])[1:-1] in replay
