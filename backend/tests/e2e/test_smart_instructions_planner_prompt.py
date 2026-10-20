"""Smart (intelligent) instructions reach the planner like skills.

Contract, checked on the prompt a real completion run sends to the LLM:
  * an intelligent instruction that STRONGLY matches the user's message
    (a keyword hits its title / labels / table names) is inlined in full,
  * one that matches only in its body is advertised in
    <available_instructions> (short id + title), its body NOT in the prompt,
  * a strong match that is LONG (over the size limit) is advertised with
    matched=..., never inlined,
  * 'always' instructions are inlined regardless.

The LLM boundary is stubbed and records every prompt; routes, services,
context building and the agent loop run real.
"""
from __future__ import annotations

import re
import uuid

import pytest

pytestmark = pytest.mark.e2e


@pytest.fixture
def prompts(monkeypatch):
    from app.ai.llm.llm import LLM
    from app.ai.llm.types import MessageStopEvent, TextDeltaEvent, UsageEvent

    seen: list[str] = []

    async def scripted(self, *, model_id=None, messages, system=None, tools=None, **kwargs):
        seen.append(f"{system}\n{messages}")
        for e in (
            TextDeltaEvent(text="The answer."),
            MessageStopEvent(stop_reason="end_turn", raw_stop_reason="end_turn"),
            UsageEvent(input_tokens=10, output_tokens=5),
        ):
            yield e

    async def _stream(self, prompt, **kwargs):
        seen.append(str(prompt))
        yield "ok"

    def _inference(self, prompt, *a, **k):
        seen.append(str(prompt))
        return "[]"

    monkeypatch.setattr(LLM, "inference_stream_v2", scripted)
    monkeypatch.setattr(LLM, "inference_stream", _stream)
    monkeypatch.setattr(LLM, "inference", _inference)
    return seen


@pytest.fixture
def env(test_client, create_user, login_user, whoami, create_llm_provider_and_models, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY_TEST", "dummy-local-provider-boundary")
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    create_llm_provider_and_models(user_token=token, org_id=org_id)
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


def _instruction(client, h, **fields):
    r = client.post("/api/instructions", headers=h, json={"status": "published", **fields})
    assert r.status_code == 200, r.text
    return r.json()


def test_planner_prompt_inlines_strong_matches_and_advertises_the_rest(test_client, env, prompts):
    strong_marker = f"STRONG{uuid.uuid4().hex[:8]}"
    weak_marker = f"WEAK{uuid.uuid4().hex[:8]}"
    always_marker = f"ALWAYS{uuid.uuid4().hex[:8]}"

    strong = _instruction(
        test_client, env, title="Revenue reporting", load_mode="intelligent",
        text=f"Report revenue net of tax. {strong_marker}",
    )
    # Body mentions revenue, title does not: a weak match.
    weak = _instruction(
        test_client, env, title="Net figures", load_mode="intelligent",
        # The catalog shows the body's first line as the description; the
        # marker sits on the second line, which only an inline load carries.
        text=f"Net figures rule.\nNet revenue always excludes refunds. {weak_marker}",
    )
    # Strong (title) match, but over the default size limit: listed, not inlined.
    long_marker = f"LONG{uuid.uuid4().hex[:8]}"
    filler = " ".join(f"step{i}" for i in range(900))
    long = _instruction(
        test_client, env, title="Revenue playbook", load_mode="intelligent",
        text=f"Detailed procedure.\n{filler} {long_marker} {filler}",
    )
    _instruction(
        test_client, env, title="Currency", load_mode="always",
        text=f"Answer in USD. {always_marker}",
    )

    r = test_client.post("/api/reports", headers=env, json={"title": "smart", "files": [], "data_sources": []})
    assert r.status_code == 200, r.text
    rid = r.json()["id"]
    r = test_client.post(
        f"/api/reports/{rid}/completions", headers=env,
        json={"prompt": {"content": "What was revenue last month?"}},
    )
    assert r.status_code == 200, r.text

    planner = [p for p in prompts if always_marker in p]
    assert planner, "no LLM call carried the organization instructions"
    prompt = planner[0]

    assert strong_marker in prompt, "strong (title) match must be inlined"
    assert weak_marker not in prompt, "body-only match must not be inlined"
    # The system text also mentions the tag in prose; the real catalog block
    # is the one holding <instruction_ref> entries.
    blocks = [
        b for b in re.findall(r"<available_instructions>(.*?)</available_instructions>", prompt, re.S)
        if "<instruction_ref" in b
    ]
    assert blocks, "planner prompt must carry the instruction catalog"
    catalog = blocks[-1]
    assert f'short_id="{weak["id"][:8]}"' in catalog, "body-only match must be advertised"
    assert f'short_id="{strong["id"][:8]}"' not in catalog, "inlined match must not be advertised too"
    assert long_marker not in prompt, "long instruction must not be inlined"
    long_ref = re.search(rf'<instruction_ref[^>]*short_id="{long["id"][:8]}"[^>]*>', catalog)
    assert long_ref and 'matched="title"' in long_ref.group(0), "long match must be flagged"
