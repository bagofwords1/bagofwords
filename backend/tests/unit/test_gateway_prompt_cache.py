"""Claude behind an OpenAI-compatible gateway (custom provider: LiteLLM,
OpenRouter) caches only what the request marks, and reports the cache in the
OpenAI usage shape.

Without the marks every call re-billed the whole prompt at the full input
rate; without reading the gateway's cache-write fields a write priced as plain
input. Usage fixtures are LiteLLM 1.104 responses captured from live calls.
"""
from types import SimpleNamespace

import pytest

from app.ai.llm.clients import openai_client
from app.ai.llm.clients.openai_client import OpenAi
from app.ai.llm.llm import LLM
from app.ai.llm.types import Message, ToolSpec
from tests.unit.test_llm_provider_headers import make_model

TOOLS = [ToolSpec(name="lookup", description="Look up an item", input_schema={"type": "object", "properties": {}})]

# Second call of a cached prefix through LiteLLM: prompt_tokens already holds the read.
LITELLM_READ = {
    "prompt_tokens": 11_244, "completion_tokens": 7, "total_tokens": 11_251,
    "prompt_tokens_details": {"cached_tokens": 11_233, "cache_creation_tokens": 0, "text_tokens": 11},
    "cache_read_input_tokens": 11_233, "cache_creation_input_tokens": 0,
}
# First call: the write, split by TTL.
LITELLM_WRITE = {
    "prompt_tokens": 15_336, "completion_tokens": 4, "total_tokens": 15_340,
    "prompt_tokens_details": {
        "cached_tokens": 0, "cache_creation_tokens": 15_321,
        "cache_creation_token_details": {"ephemeral_5m_input_tokens": 3_455, "ephemeral_1h_input_tokens": 11_866},
    },
    "cache_read_input_tokens": 0, "cache_creation_input_tokens": 15_321,
}


def _client(markers):
    return OpenAi(api_key="sk-test", base_url="http://gateway.invalid/v1", prompt_cache_markers=markers)


def _capture_one_shot(client, usage):
    sent = {}

    def create(**params):
        sent.update(params)
        return SimpleNamespace(usage=usage, choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))])

    client.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    return sent


async def _capture_stream(client, monkeypatch, messages):
    sent = {}

    async def fake_stream(_async_client, request_kwargs, _efforts):
        sent.update(request_kwargs)

        async def chunks():
            yield SimpleNamespace(usage=LITELLM_WRITE, choices=[])
        return chunks()

    monkeypatch.setattr(openai_client, "create_chat_stream", fake_stream)
    async for _ in client.inference_stream_v2("claude-haiku-4-5", messages, system="stable prefix", tools=TOOLS):
        pass
    return sent


def _controls(payload):
    """Every cache_control mark in a request, by where it sits."""
    marks = []
    for i, msg in enumerate(payload["messages"]):
        content = msg.get("content")
        if isinstance(content, list):
            marks += [(f"message[{i}]:{msg['role']}", block["cache_control"]) for block in content if "cache_control" in block]
    marks += [("tool", tool["cache_control"]) for tool in payload.get("tools", []) if "cache_control" in tool]
    return marks


CONVERSATION = [
    Message(role="user", content="Find item 7."),
    Message(role="assistant", content=[{"type": "tool_use", "id": "call_1", "name": "lookup", "input": {}}]),
    Message(role="user", content=[{"type": "tool_result", "tool_use_id": "call_1", "content": "rows"}]),
    Message(role="user", content="And now?"),
]


@pytest.mark.parametrize("model_id, expected", [
    ("claude-haiku-4-5", True), ("my-litellm-alias-claude-sonnet-5", True),
    ("gpt-4.1", False), ("llama-3.3-70b", False),
])
def test_custom_provider_marks_only_claude_models(model_id, expected):
    model = make_model("custom", {"base_url": "http://gateway.invalid/v1"})
    model.model_id = model_id
    assert LLM(model).client.prompt_cache_markers is expected


def test_an_admin_can_turn_the_markers_off():
    model = make_model("custom", {"base_url": "http://gateway.invalid/v1", "prompt_cache_markers": False})
    model.model_id = "claude-haiku-4-5"
    assert LLM(model).client.prompt_cache_markers is False


def test_unmarked_requests_are_unchanged():
    client = _client(markers=False)
    sent = _capture_one_shot(client, LITELLM_READ)
    client.inference("claude-haiku-4-5", "Say ok", system="stable prefix")
    assert sent["messages"][0] == {"role": "system", "content": "stable prefix"}
    assert _controls(sent) == []


def test_one_shot_marks_the_system_prefix():
    client = _client(markers=True)
    sent = _capture_one_shot(client, LITELLM_READ)
    client.inference("claude-haiku-4-5", "Say ok", system="stable prefix")
    marks = _controls(sent)
    assert [where for where, _ in marks] == ["message[0]:system"]
    assert sent["messages"][0]["content"][0]["text"] == "stable prefix"


@pytest.mark.asyncio
async def test_agent_stream_marks_tools_system_and_the_settled_turn(monkeypatch):
    sent = await _capture_stream(_client(markers=True), monkeypatch, CONVERSATION)
    marks = dict(_controls(sent))
    settled = len(sent["messages"]) - 2
    assert set(marks) == {"tool", "message[0]:system", f"message[{settled}]:{sent['messages'][settled]['role']}"}
    # The newest turn changes every iteration and stays outside the cache.
    assert isinstance(sent["messages"][-1]["content"], str)
    # Anthropic allows at most four breakpoints per request.
    assert len(marks) <= 4


@pytest.mark.asyncio
async def test_agent_stream_without_markers_sends_no_cache_control(monkeypatch):
    sent = await _capture_stream(_client(markers=False), monkeypatch, CONVERSATION)
    assert _controls(sent) == []


def test_gateway_cache_reads_are_parsed():
    usage = OpenAi._extract_usage(LITELLM_READ)
    assert (usage.prompt_tokens, usage.cache_read_tokens, usage.cache_creation_tokens) == (11_244, 11_233, 0)


@pytest.mark.parametrize("shape", ["dict", "object"])
def test_gateway_cache_writes_keep_their_ttl_split(shape):
    raw = LITELLM_WRITE if shape == "dict" else SimpleNamespace(**{
        k: SimpleNamespace(**{kk: (SimpleNamespace(**vv) if isinstance(vv, dict) else vv) for kk, vv in v.items()})
        if isinstance(v, dict) else v for k, v in LITELLM_WRITE.items()})
    usage = OpenAi._extract_usage(raw)
    assert usage.cache_creation_tokens == 15_321
    assert (usage.cache_write_5m_tokens, usage.cache_write_1h_tokens) == (3_455, 11_866)
    assert usage.cache_write_5m_tokens + usage.cache_write_1h_tokens == usage.cache_creation_tokens


def test_a_write_without_a_ttl_split_bills_at_the_default_ttl():
    usage = OpenAi._extract_usage({"prompt_tokens": 500, "completion_tokens": 1, "cache_creation_input_tokens": 400})
    assert (usage.cache_creation_tokens, usage.cache_write_5m_tokens, usage.cache_write_1h_tokens) == (400, 400, 0)


@pytest.mark.asyncio
async def test_streamed_usage_carries_the_cache_write(monkeypatch):
    client = _client(markers=True)
    await _capture_stream(client, monkeypatch, CONVERSATION)
    usage = client.pop_last_usage()
    assert (usage.cache_write_5m_tokens, usage.cache_write_1h_tokens) == (3_455, 11_866)


def test_plain_openai_usage_is_read_as_before():
    usage = OpenAi._extract_usage({
        "prompt_tokens": 9_570, "completion_tokens": 21,
        "prompt_tokens_details": {"cached_tokens": 9_553},
        "completion_tokens_details": {"reasoning_tokens": 12},
    })
    assert (usage.prompt_tokens, usage.cache_read_tokens, usage.reasoning_tokens) == (9_570, 9_553, 12)
    assert usage.cache_creation_tokens == 0
