"""The run-invariant prompt prefix must outlive the gap between user turns.

Anthropic's default ephemeral cache TTL is 5 minutes, measured from the start of
the request. Inside one agent run the planner iterates fast enough to keep the
entry warm, but between user turns a person reads the answer and types the next
question — routinely longer than that — so the whole tools+system prefix was
re-written at the 1.25x write rate on the first call of nearly every turn
instead of being read back at 0.1x.

The invariant this file pins:

  * the run-invariant prefix (tool catalog + system prompt) is marked for the
    long TTL, so it survives a gap between turns;
  * the per-turn message breakpoint is NOT, because it moves every iteration and
    a 2x write would buy an entry that is superseded seconds later;
  * consequently any long-TTL block precedes every short-TTL block, which
    Anthropic requires (the render order is tools -> system -> messages);
  * an operator can restore the old behavior with one env var, and a typo in
    that env var must not silently disable caching.

Asserted through `inference_stream_v2` — the client's public surface — with the
provider stubbed at the network boundary, so the test survives refactors of how
the marker is computed internally.
"""
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

import pytest  # noqa: E402

from app.ai.llm.clients.anthropic_client import Anthropic  # noqa: E402
from app.ai.llm.types import Message, ToolSpec  # noqa: E402

LONG_TTL = "1h"


class _EmptyStream:
    """A finished Anthropic stream: the client only needs it to be iterable."""

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration


def _messages():
    """A transcript mid-run: static context, the ask, then one settled step."""
    return [
        Message(role="user", content="STATIC CONTEXT " * 40),
        Message(role="user", content="the ask"),
        Message(role="assistant", content=[
            {"type": "tool_use", "id": "c0", "name": "create_data", "input": {}},
        ]),
        Message(role="user", content=[
            {"type": "tool_result", "tool_use_id": "c0", "content": "5 rows"},
        ]),
    ]


def _tools():
    return [
        ToolSpec(name="create_data", description="build a table", input_schema={"type": "object"}),
        ToolSpec(name="clarify", description="ask the user", input_schema={"type": "object"}),
    ]


async def _send(monkeypatch, ttl_env="__unset__"):
    """Run one request and return the body the client would have put on the wire."""
    if ttl_env == "__unset__":
        monkeypatch.delenv("BOW_PROMPT_CACHE_TTL", raising=False)
    else:
        monkeypatch.setenv("BOW_PROMPT_CACHE_TTL", ttl_env)

    client = Anthropic(api_key="test-key-not-used")
    captured = {}

    async def _fake_create(**kwargs):
        captured.update(kwargs)
        return _EmptyStream()

    # Boundary mock: the provider's HTTP call. Everything above it runs real.
    client.async_client.messages.create = _fake_create

    async for _ in client.inference_stream_v2(
        model_id="claude-haiku-4-5-20251001",
        messages=_messages(),
        system="You are an analyst.",
        tools=_tools(),
    ):
        pass
    return captured


def _marker(block):
    return (block or {}).get("cache_control")


def _message_markers(request):
    """Every cache_control marker carried by a message content block."""
    out = []
    for msg in request.get("messages", []):
        content = msg.get("content")
        if isinstance(content, list):
            out.extend(_marker(b) for b in content if isinstance(b, dict) and _marker(b))
    return out


@pytest.mark.asyncio
async def test_prefix_survives_a_gap_between_turns(monkeypatch):
    """Tools and system carry the long TTL, so a turn that starts minutes later
    reads the prefix instead of paying to write it again."""
    request = await _send(monkeypatch)

    assert _marker(request["system"][0]) == {"type": "ephemeral", "ttl": LONG_TTL}
    assert _marker(request["tools"][-1]) == {"type": "ephemeral", "ttl": LONG_TTL}


@pytest.mark.asyncio
async def test_moving_message_breakpoint_keeps_the_short_ttl(monkeypatch):
    """The per-turn breakpoint is superseded on the next iteration, so it must
    not pay the long-TTL write premium."""
    request = await _send(monkeypatch)
    markers = _message_markers(request)

    assert markers, "the settled part of the transcript must still be marked"
    assert all("ttl" not in m for m in markers), markers


@pytest.mark.asyncio
async def test_long_ttl_blocks_precede_short_ttl_blocks(monkeypatch):
    """Anthropic requires longer-TTL entries to appear before shorter ones.
    Render order is tools -> system -> messages, so the prefix must hold every
    long-TTL marker and the messages none."""
    request = await _send(monkeypatch)

    prefix = [_marker(request["tools"][-1]), _marker(request["system"][0])]
    assert all(m.get("ttl") == LONG_TTL for m in prefix)
    assert all(m.get("ttl") is None for m in _message_markers(request))


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["5m", "5min", "default", "ephemeral", "5M", " 5m "])
async def test_operator_can_restore_the_short_ttl(monkeypatch, value):
    """One env var rolls the behavior back without a deploy."""
    request = await _send(monkeypatch, ttl_env=value)

    assert _marker(request["system"][0]) == {"type": "ephemeral"}
    assert _marker(request["tools"][-1]) == {"type": "ephemeral"}


@pytest.mark.asyncio
async def test_unrecognised_override_still_caches(monkeypatch):
    """A typo must not silently drop caching altogether — the prefix stays
    marked, which is the property that actually costs money if lost."""
    request = await _send(monkeypatch, ttl_env="banana")

    assert _marker(request["system"][0]) is not None
    assert _marker(request["tools"][-1]) is not None


@pytest.mark.asyncio
async def test_caching_can_be_disabled_entirely(monkeypatch):
    """enable_cache=False means no markers anywhere, whatever the TTL setting."""
    monkeypatch.delenv("BOW_PROMPT_CACHE_TTL", raising=False)
    client = Anthropic(api_key="test-key-not-used")
    captured = {}

    async def _fake_create(**kwargs):
        captured.update(kwargs)
        return _EmptyStream()

    client.async_client.messages.create = _fake_create
    async for _ in client.inference_stream_v2(
        model_id="claude-haiku-4-5-20251001",
        messages=_messages(),
        system="You are an analyst.",
        tools=_tools(),
        enable_cache=False,
    ):
        pass

    assert isinstance(captured["system"], str)
    assert _marker(captured["tools"][-1]) is None
    assert _message_markers(captured) == []
