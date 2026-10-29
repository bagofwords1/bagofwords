"""Explicit effort overrides triggers/defaults and reaches the provider SDK."""
import asyncio

import pytest

from unittest.mock import AsyncMock

from app.ai.llm.clients.anthropic_client import Anthropic
from app.ai.llm.clients.openai_client import OpenAi
from app.ai.llm.clients.openai_responses_client import OpenAIResponsesClient
from app.ai.llm.reasoning import _effort_to_thinking_config, _resolve_reasoning_effort
from app.ai.llm.types import Message


class _Sent(Exception):
    pass


def _request_for(model_id: str, *, prompt: str = "revenue by month",
                 per_completion=None, model_default=None) -> dict:
    client = Anthropic.__new__(Anthropic)
    client.max_tokens = 32768
    client.temperature = 0.3

    class _Messages:
        async def create(self, **kwargs):
            raise _Sent(kwargs)

    class _Async:
        messages = _Messages()

    client.async_client = _Async()
    effort = _resolve_reasoning_effort(
        per_completion=per_completion, prompt_text=prompt, model_default=model_default,
    )
    thinking = _effort_to_thinking_config(effort, model_id)

    async def _run():
        try:
            async for _ in client.inference_stream_v2(
                model_id, [Message(role="user", content=prompt)], thinking=thinking,
            ):
                pass
        except _Sent as sent:
            return sent.args[0]

    return asyncio.run(_run())


def _effort(request: dict):
    return ((request.get("extra_body") or {}).get("output_config") or {}).get("effort")


CLAUDE_MODELS = ["claude-sonnet-5-5", "claude-sonnet-5", "claude-opus-4-8", "claude-fable-5-1"]


@pytest.mark.parametrize("model_id", ["claude-fable-5-1", "claude-mythos-5", "claude-opus-5-5"])
def test_off_uses_low_when_model_cannot_disable_thinking(model_id):
    request = _request_for(model_id)
    assert request["extra_body"]["thinking"]["type"] == "adaptive"
    assert _effort(request) == "low"


@pytest.mark.parametrize("model_id", CLAUDE_MODELS)
@pytest.mark.parametrize("prompt", ["think hard about churn", "please be thorough here"])
def test_trigger_phrase_still_raises_effort(model_id, prompt):
    assert _effort(_request_for(model_id, prompt=prompt)) == "high"


@pytest.mark.parametrize("model_id", CLAUDE_MODELS)
@pytest.mark.parametrize("source", ["per_completion", "model_default"])
def test_an_explicit_effort_is_never_lowered(model_id, source):
    assert _effort(_request_for(model_id, **{source: "medium"})) == "medium"


@pytest.mark.parametrize("model_id", ["claude-haiku-4-5-20251001", "claude-sonnet-4-6"])
def test_models_that_can_stop_thinking_receive_explicit_disable(model_id):
    request = _request_for(model_id)
    assert request["extra_body"]["thinking"] == {"type": "disabled"}
    assert _effort(request) is None


# ── OpenAI (Responses and Chat Completions) ─────────────────────────────

async def _empty_stream():
    if False:
        yield


def _openai_request(api: str, model_id: str, *, prompt: str = "revenue by month",
                    per_completion=None, model_default=None, **attrs) -> dict:
    if api == "responses":
        client = OpenAIResponsesClient(api_key="test-key")
        create = AsyncMock(side_effect=lambda **kw: _empty_stream())
        client.async_client.responses.create = create
    else:
        client = OpenAi(api_key="test-key", base_url="https://gateway.example/v1")
        create = AsyncMock(side_effect=lambda **kw: _empty_stream())
        client.async_client.chat.completions.create = create
    for k, v in attrs.items():
        setattr(client, k, v)
    effort = _resolve_reasoning_effort(
        per_completion=per_completion, prompt_text=prompt, model_default=model_default,
    )

    async def _run():
        async for _ in client.inference_stream_v2(
            model_id, [Message(role="user", content=prompt)],
            thinking=_effort_to_thinking_config(effort, model_id),
        ):
            pass

    asyncio.run(_run())
    return create.call_args.kwargs


def _openai_effort(api: str, request: dict):
    if api == "responses":
        return (request.get("reasoning") or {}).get("effort")
    return request.get("reasoning_effort")


APIS = ["responses", "chat"]


@pytest.mark.parametrize("api", APIS)
@pytest.mark.parametrize("model_id,lightest", [
    ("gpt-5.6-terra", "none"), ("gpt-5.6-luna", "none"), ("gpt-6-sol", "none"),
    ("gpt-5.1", "none"),
    ("gpt-6.1-sol", "low"),   # has no "none"
    ("gpt-5", "minimal"),     # lightest is "minimal"
    ("o3", "low"),
])
def test_off_asks_openai_models_for_their_lightest_effort(api, model_id, lightest):
    assert _openai_effort(api, _openai_request(api, model_id)) == lightest


@pytest.mark.parametrize("model_id", ["gpt-5.6-terra", "gpt-6-luna"])
def test_off_requests_no_reasoning_summary_when_nothing_is_reasoned(model_id):
    request = _openai_request("responses", model_id)
    assert request["reasoning"] == {"effort": "none"}


@pytest.mark.parametrize("api", APIS)
@pytest.mark.parametrize("model_id", ["gpt-5.6-terra", "gpt-6.1-sol"])
@pytest.mark.parametrize("source", ["per_completion", "model_default"])
def test_an_explicit_openai_effort_is_never_lowered(api, model_id, source):
    request = _openai_request(api, model_id, **{source: "high"})
    assert _openai_effort(api, request) == "high"


@pytest.mark.parametrize("api", APIS)
def test_trigger_phrase_still_raises_openai_effort(api):
    request = _openai_request(api, "gpt-5.6-terra", prompt="think hard about churn")
    assert _openai_effort(api, request) == "high"


@pytest.mark.parametrize("api", APIS)
@pytest.mark.parametrize("attrs", [
    {"reasoning_mode": "generic"},
    {"reasoning_mode": "custom", "reasoning_params": {"high": {"reasoning_effort": "high"}}},
])
def test_off_leaves_admin_configured_endpoints_alone(api, attrs):
    # Generic/custom endpoints only get what the admin configured for a level.
    assert _openai_effort(api, _openai_request(api, "my-gateway-model", **attrs)) is None
@pytest.mark.parametrize("model_id", ["claude-sonnet-5", "claude-opus-4-8"])
def test_explicit_off_overrides_trigger_and_model_default(model_id):
    request = _request_for(model_id, per_completion="off", prompt="think hard about revenue", model_default="high")
    assert request["extra_body"]["thinking"] == {"type": "disabled"}
