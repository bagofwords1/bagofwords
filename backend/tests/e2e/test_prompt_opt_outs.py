"""Per-request opt-outs on the completion prompt: ``follow_ups`` and
``suggest_instructions``.

An API caller that doesn't render follow-up chips or instruction-suggestion
blocks can decline them per request with ``prompt.<key>: false``. The contract:
  * false skips that extra for this turn only;
  * absent keeps today's behavior (org settings decide);
  * true never turns on what the org disabled.

The LLM boundary is stubbed (``LLM.inference_stream_v2`` for planner calls,
``LLM.inference`` for small-model one-shots), plus the harness trigger verdict
in the harness tests (see ``harness_triggers``); routes, services, the agent
loop, and persistence run real. Whether the knowledge harness ran is
observed at that boundary: it is the only caller that sends a planner request
with "running the Knowledge Harness" in its system prompt.

Run: cd backend && uv run pytest tests/e2e/test_prompt_opt_outs.py -q --db=sqlite
"""
from __future__ import annotations

import json

import pytest

HARNESS_MARKER = "running the Knowledge Harness"
FOLLOW_UPS = ["Break revenue down by region", "Compare with the same month last year"]


def _scripted_stream(calls: list):
    from app.ai.llm.types import MessageStopEvent, TextDeltaEvent, UsageEvent

    async def scripted(self, *, model_id=None, messages, system=None, tools=None, **kwargs):
        calls.append(system or "")
        for e in (
            TextDeltaEvent(text="Revenue grew 12% month over month, led by the enterprise plan."),
            MessageStopEvent(stop_reason="end_turn", raw_stop_reason="end_turn"),
            UsageEvent(input_tokens=100, output_tokens=20),
        ):
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
def planner_calls(monkeypatch):
    from app.ai.llm.llm import LLM

    calls: list = []
    monkeypatch.setattr(LLM, "inference_stream_v2", _scripted_stream(calls))
    monkeypatch.setattr(LLM, "inference_stream", _one_shot_stream)
    monkeypatch.setattr(LLM, "inference", _one_shot)
    return calls


@pytest.fixture
def env(create_user, login_user, whoami, create_report, create_llm_provider_and_models, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY_TEST", "dummy-local-provider-boundary")
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    create_llm_provider_and_models(user_token=token, org_id=org_id)
    headers = {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}

    def new_report(title="Opt-outs"):
        return create_report(title=title, user_token=token, org_id=org_id, data_sources=[])["id"]

    return {"token": token, "org_id": org_id, "headers": headers, "new_report": new_report}


def _ask(client, report_id, headers, content, **prompt_flags):
    body = {"prompt": {"content": content, "widget_id": None, "step_id": None, "mentions": [{}], **prompt_flags}}
    r = client.post(f"/api/reports/{report_id}/completions", json=body, headers=headers, params={"background": False})
    assert r.status_code == 200, r.text


def _last_answer(client, report_id, headers):
    r = client.get(f"/api/reports/{report_id}/completions", headers=headers)
    assert r.status_code == 200, r.text
    systems = [c for c in r.json()["completions"] if c["role"] == "system"]
    assert systems, "no system completion recorded"
    return systems[-1]


def _harness_ran(calls, since: int) -> bool:
    return any(HARNESS_MARKER in s for s in calls[since:])


QUESTION = "How did monthly revenue change across the last quarter?"
CORRECTION = "No, I meant net revenue instead of gross revenue for that quarter"


def test_follow_ups_false_skips_suggestions_for_that_turn_only(test_client, env, planner_calls):
    rid = env["new_report"]()
    h = env["headers"]

    _ask(test_client, rid, h, QUESTION)
    assert _last_answer(test_client, rid, h)["follow_ups"], "control: follow-ups are on by default"

    _ask(test_client, rid, h, "And which plan contributed most to that growth?", follow_ups=False)
    assert not _last_answer(test_client, rid, h)["follow_ups"]

    # The opt-out is per request: the next turn without it gets suggestions again.
    _ask(test_client, rid, h, "Show the same breakdown for the previous quarter")
    assert _last_answer(test_client, rid, h)["follow_ups"]


def test_follow_ups_true_does_not_override_org_setting_off(
    test_client, env, planner_calls, update_organization_settings,
):
    update_organization_settings(
        {"enable_follow_ups": {"value": False}}, user_token=env["token"], org_id=env["org_id"],
    )
    rid = env["new_report"]()
    _ask(test_client, rid, env["headers"], QUESTION, follow_ups=True)
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


@pytest.mark.parametrize("opt_out", [False, True], ids=["default", "suggest_instructions_false"])
def test_suggest_instructions_false_skips_knowledge_harness(
    test_client, env, planner_calls, harness_triggers, opt_out,
):
    rid = env["new_report"]()
    since = len(planner_calls)
    flags = {"suggest_instructions": False} if opt_out else {}
    _ask(test_client, rid, env["headers"], CORRECTION, **flags)

    assert _harness_ran(planner_calls, since) is (not opt_out)


@pytest.mark.parametrize("field", ["follow_ups", "suggest_instructions"])
def test_opt_out_rejects_non_boolean(test_client, env, planner_calls, field):
    rid = env["new_report"]()
    body = {"prompt": {"content": QUESTION, field: "sometimes"}}
    r = test_client.post(
        f"/api/reports/{rid}/completions", json=body, headers=env["headers"], params={"background": False},
    )
    assert r.status_code == 422
