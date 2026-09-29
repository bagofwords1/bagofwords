"""User-selectable reasoning effort: what each provider receives.

The provider SDK is the mocked boundary; the request it receives is the
contract. Levels are the product's (low / medium / high / max); each client
must translate them into something that model's API accepts — never a value
it rejects (every rejection here was reproduced live against the provider).
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.ai.llm.clients.anthropic_client import Anthropic
from app.ai.llm.clients.bedrock_client import BedrockClient
from app.ai.llm.clients.chat_effort import create_chat_stream
from app.ai.llm.clients.google_client import Google
from app.ai.llm.clients.openai_client import OpenAi
from app.ai.llm.clients.openai_responses_client import OpenAIResponsesClient
from app.ai.llm.reasoning import (
    USER_EFFORTS,
    _effort_to_thinking_config,
    clamp_effort,
    model_efforts,
    native_efforts,
    reasoning_info,
)
from app.ai.llm.types import Message, ToolSpec
from app.models.llm_model import LLM_MODEL_DETAILS
from app.utils.reasoning_effort import normalize_effort

TOOL = ToolSpec(name="get_rows", description="Get rows", input_schema={"type": "object", "properties": {}})


async def _empty_stream():
    if False:
        yield


def _thinking(level, model_id):
    return _effort_to_thinking_config(level, model_id)


def _drain(agen):
    async def run():
        async for _ in agen:
            pass
    asyncio.run(run())


# ── Capability: every level maps onto something the model accepts ────────

@pytest.mark.parametrize("detail", [d for d in LLM_MODEL_DETAILS if not d.get("supports_image_generation")],
                         ids=lambda d: f"{d['provider_type']}:{d['model_id']}")
@pytest.mark.parametrize("level", USER_EFFORTS)
def test_every_catalog_level_runs_as_an_accepted_effort(detail, level):
    efforts = model_efforts(detail["model_id"])
    assert efforts, f"catalog chat model {detail['model_id']} has no reasoning capability"
    runs_as = clamp_effort(level, efforts)
    assert runs_as in efforts and runs_as != "none"


@pytest.mark.parametrize("model_id,top", [
    ("gpt-6-luna", "max"), ("gpt-6.1-sol", "max"), ("gpt-5.6-terra", "max"), ("gpt-5.5", "xhigh"), ("gpt-5.4-mini", "xhigh"),
    ("claude-sonnet-5", "max"), ("claude-opus-4-6", "max"), ("gemini-3.6-flash", "high"),
    ("eu.anthropic.claude-sonnet-5", "max"), ("us.anthropic.claude-opus-4-6-v1:0", "max"),
])
def test_max_means_the_strongest_level_the_model_has(model_id, top):
    assert clamp_effort("max", native_efforts(model_id)) == top


def test_gpt_6_1_sol_has_no_none_effort():
    """GPT-6.1 Sol dropped reasoning effort "none"; GPT-6 Sol still has it."""
    assert "none" not in native_efforts("gpt-6.1-sol")
    assert "none" in native_efforts("gpt-6-sol")


@pytest.mark.parametrize("model_id", ["gpt-4.1", "gpt-image-1", "gpt-image-2.5-sunburst", "claude-3-haiku-20240307"])
def test_non_reasoning_models_get_nothing(model_id):
    info = reasoning_info(model_id)
    assert info["supported"] is False and info["levels"] == {}
    assert clamp_effort("high", model_efforts(model_id)) is None


def test_modes_override_the_catalog():
    assert reasoning_info("prod-gpt", {"reasoning_mode": "like", "reasoning_model_id": "gpt-5.4"})["levels"]["max"] == "xhigh"
    assert reasoning_info("llama3.3", {"reasoning_mode": "generic"})["levels"]["max"] == "high"
    assert reasoning_info("gpt-6-luna", {"reasoning_mode": "off"})["supported"] is False
    custom = reasoning_info("my-model", {"reasoning_mode": "custom", "reasoning_params": {"high": {"think": "high"}}})
    assert custom["supported"] and set(custom["levels"].values()) == {"high"}
    # Configs written before modes existed: a capability model means "like".
    assert reasoning_info("opaque", {"reasoning_model_id": "claude-sonnet-5"})["mode"] == "like"


@pytest.mark.parametrize("raw,expected", [
    (None, None), ("", None), ("Default", None), ("auto", None),
    ("HIGH", "high"), (" max ", "max"), ("xhigh", "xhigh"), ("off", "off"), ("none", "off"),
])
def test_normalize_effort_accepts_the_vocabulary(raw, expected):
    assert normalize_effort(raw) == expected


@pytest.mark.parametrize("raw", ["ultra", "hi", "5", "maximum"])
def test_normalize_effort_rejects_anything_else(raw):
    with pytest.raises(ValueError):
        normalize_effort(raw)


# ── OpenAI Responses API ────────────────────────────────────────────────

def _responses_request(model_id, level, **attrs):
    client = OpenAIResponsesClient(api_key="test-key")
    for k, v in attrs.items():
        setattr(client, k, v)
    create = AsyncMock(side_effect=lambda **kw: _empty_stream())
    client.async_client.responses.create = create
    _drain(client.inference_stream_v2(
        model_id, [Message(role="user", content="hi")], tools=[TOOL],
        thinking=_thinking(level, model_id) if level else None,
    ))
    return create.call_args.kwargs


@pytest.mark.parametrize("model_id,level,sent", [
    ("gpt-6-luna", "low", "low"), ("gpt-6-luna", "max", "max"),
    ("gpt-5.4-mini", "max", "xhigh"), ("gpt-5.5", "high", "high"),
])
def test_responses_sends_the_clamped_effort(model_id, level, sent):
    params = _responses_request(model_id, level)
    assert params["reasoning"]["effort"] == sent
    assert "temperature" not in params


def test_responses_default_sends_no_effort():
    params = _responses_request("gpt-6-luna", None)
    assert "effort" not in params.get("reasoning", {})


def test_responses_raw_fields_merge_into_the_request():
    params = _responses_request(
        "gpt-5.5", "high",
        reasoning_params={"high": {"reasoning": {"summary": "detailed"}, "text": {"verbosity": "low"}}},
    )
    # Existing keys deep-merge (our effort survives); new keys pass through.
    assert params["reasoning"] == {"summary": "detailed", "effort": "high"}
    assert params["extra_body"] == {"text": {"verbosity": "low"}}


def test_custom_mode_sends_only_the_raw_fields():
    params = _responses_request(
        "my-gateway-model", "high",
        reasoning_mode="custom", reasoning_params={"high": {"reasoning_effort": "high"}},
    )
    assert "effort" not in params.get("reasoning", {})
    assert params["extra_body"] == {"reasoning_effort": "high"}


def test_off_mode_sends_no_reasoning():
    params = _responses_request("gpt-6-luna", "high", reasoning_mode="off")
    assert "reasoning" not in params


# ── Chat Completions (OpenAI-compatible, Azure) ─────────────────────────

def _chat_request(model_id, level, **attrs):
    client = OpenAi(api_key="test-key", base_url="https://gateway.example/v1")
    for k, v in attrs.items():
        setattr(client, k, v)
    create = AsyncMock(side_effect=lambda **kw: _empty_stream())
    client.async_client.chat.completions.create = create
    _drain(client.inference_stream_v2(
        model_id, [Message(role="user", content="hi")], tools=[TOOL],
        thinking=_thinking(level, model_id) if level else None,
    ))
    return create


def test_chat_completions_has_no_max_level():
    # Chat Completions rejects "max" (verified live); the strongest it takes is xhigh.
    assert _chat_request("gpt-6-luna", "max").call_args.kwargs["reasoning_effort"] == "xhigh"


def test_generic_mode_opts_an_unknown_model_in():
    kwargs = _chat_request("llama3.3", "max", reasoning_mode="generic").call_args.kwargs
    assert kwargs["reasoning_effort"] == "high"
    assert "reasoning_effort" not in _chat_request("llama3.3", "max").call_args.kwargs


def test_chat_retries_with_none_when_tools_and_effort_are_rejected():
    calls = []

    async def create(**kwargs):
        calls.append(dict(kwargs))
        if kwargs.get("reasoning_effort") not in (None, "none"):
            raise Exception("Error code: 400 - Function tools with reasoning_effort are not supported "
                            "for gpt-5.6-sol in /v1/chat/completions")
        return _empty_stream()

    async_client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    asyncio.run(create_chat_stream(
        async_client, {"model": "gpt-5.6-sol", "tools": [{}], "reasoning_effort": "high"},
        ("none", "low", "medium", "high", "xhigh"),
    ))
    assert [c["reasoning_effort"] for c in calls] == ["high", "none"]


def test_chat_other_errors_are_not_swallowed():
    async def create(**kwargs):
        raise Exception("Error code: 401 - invalid api key")

    async_client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    with pytest.raises(Exception, match="401"):
        asyncio.run(create_chat_stream(async_client, {"model": "gpt-5.6-sol", "tools": [{}], "reasoning_effort": "high"}, None))


# ── Anthropic Messages API ──────────────────────────────────────────────

class _Sent(Exception):
    pass


def _anthropic_request(model_id, level, **attrs):
    client = Anthropic.__new__(Anthropic)
    client.max_tokens = 32768
    client.temperature = 0.3
    for k, v in attrs.items():
        setattr(client, k, v)

    class _Messages:
        async def create(self, **kwargs):
            raise _Sent(kwargs)

    client.async_client = SimpleNamespace(messages=_Messages())

    async def run():
        try:
            async for _ in client.inference_stream_v2(
                model_id, [Message(role="user", content="hi")], tools=[TOOL],
                thinking=_thinking(level, model_id) if level else None,
            ):
                pass
        except _Sent as sent:
            return sent.args[0]

    return asyncio.run(run())


@pytest.mark.parametrize("model_id", ["claude-sonnet-5", "claude-opus-4-8", "claude-opus-4-6"])
@pytest.mark.parametrize("level", USER_EFFORTS)
def test_adaptive_claude_gets_an_accepted_effort(model_id, level):
    body = _anthropic_request(model_id, level)["extra_body"]
    assert body["thinking"]["type"] == "adaptive"
    assert "budget_tokens" not in body["thinking"]
    assert body["output_config"]["effort"] in native_efforts(model_id)


def test_budget_claude_max_gets_the_largest_budget_under_max_tokens():
    req = _anthropic_request("claude-haiku-4-5-20251001", "max")
    budget = req["extra_body"]["thinking"]["budget_tokens"]
    assert req["extra_body"]["thinking"]["type"] == "enabled"
    assert budget > 15000 and req["max_tokens"] > budget
    assert "output_config" not in req["extra_body"]


def test_anthropic_raw_fields_merge_into_the_body():
    req = _anthropic_request("claude-sonnet-5", "high", reasoning_params={"high": {"thinking": {"display": "omitted"}}})
    assert req["extra_body"]["thinking"] == {"type": "adaptive", "display": "omitted"}
    assert req["extra_body"]["output_config"] == {"effort": "high"}


# ── Bedrock Converse ────────────────────────────────────────────────────

def _bedrock_request(model_id, level):
    client = BedrockClient(region="eu-west-1", auth_mode="api_key", api_key="test-key")
    sent = {}

    class _Fake:
        def converse_stream(self, **kwargs):
            sent.update(kwargs)
            return {"stream": []}

    client.client = _Fake()
    _drain(client.inference_stream_v2(
        model_id, [Message(role="user", content="hi")], tools=[TOOL],
        thinking=_thinking(level, model_id) if level else None,
    ))
    return sent


@pytest.mark.parametrize("model_id", ["eu.anthropic.claude-sonnet-5", "global.anthropic.claude-opus-4-8"])
@pytest.mark.parametrize("level", USER_EFFORTS)
def test_bedrock_adaptive_claude_puts_effort_beside_thinking(model_id, level):
    fields = _bedrock_request(model_id, level)["additionalModelRequestFields"]
    # Sonnet 5 / Opus 4.7+ reject type=enabled, and effort inside `thinking`
    # is a ValidationException — both verified live on Bedrock.
    assert fields["thinking"]["type"] == "adaptive"
    assert "effort" not in fields["thinking"] and "budget_tokens" not in fields["thinking"]
    assert fields["output_config"]["effort"] in native_efforts(model_id)


def test_bedrock_opus_4_6_never_gets_xhigh():
    fields = _bedrock_request("eu.anthropic.claude-opus-4-6-v1", "max")["additionalModelRequestFields"]
    assert fields["output_config"]["effort"] == "max"


def test_bedrock_budget_claude_keeps_a_budget_below_max_tokens():
    sent = _bedrock_request("eu.anthropic.claude-haiku-4-5-20251001-v1:0", "high")
    thinking = sent["additionalModelRequestFields"]["thinking"]
    assert thinking["type"] == "enabled"
    assert sent["inferenceConfig"]["maxTokens"] > thinking["budget_tokens"]


# ── Gemini ──────────────────────────────────────────────────────────────

def _google_config(model_id, level):
    client = Google(api_key="test-key")
    sent = {}

    class _Models:
        def generate_content_stream(self, **kwargs):
            sent.update(kwargs)
            return []

    client.client = SimpleNamespace(models=_Models())
    _drain(client.inference_stream_v2(
        model_id, [Message(role="user", content="hi")], tools=[TOOL],
        thinking=_thinking(level, model_id) if level else None,
    ))
    return sent["config"].thinking_config


@pytest.mark.parametrize("level,sent", [("low", "LOW"), ("high", "HIGH"), ("max", "HIGH")])
def test_gemini_3_gets_a_thinking_level_not_a_budget(level, sent):
    tc = _google_config("gemini-3.6-flash", level)
    assert str(getattr(tc.thinking_level, "value", tc.thinking_level)) == sent
    assert tc.thinking_budget is None  # sending both is a 400


def test_gemini_2_5_gets_a_budget():
    tc = _google_config("gemini-2.5-flash", "high")
    assert tc.thinking_level is None and tc.thinking_budget and tc.thinking_budget > 1024
