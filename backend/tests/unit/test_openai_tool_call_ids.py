"""Tool-call ids on the OpenAI-compatible client.

openai_client serves OpenAI itself AND every OpenAI-compatible endpoint —
custom providers, LiteLLM, vLLM, Ollama. OpenAI always sends a tool-call id;
several compatible servers send none. The id used to fall back to "", which is
survivable for one call and not for a batch: parallel tool calls are on by
default (ai_tool_concurrency=4), so N calls would all key on "" and the
tool_use -> tool_result pairing on replay becomes ambiguous.
"""
import types

import pytest

from app.ai.llm.clients.openai_client import OpenAi
from app.ai.llm.types import (
    Message,
    ToolSpec,
    ToolUseCompleteEvent,
    ToolUseStartEvent,
)


def _delta(index, *, call_id, name=None, args=""):
    return types.SimpleNamespace(
        index=index,
        id=call_id,
        function=types.SimpleNamespace(name=name, arguments=args),
    )


def _chunk(tool_calls=None, content=None, finish_reason=None):
    delta = types.SimpleNamespace(content=content, tool_calls=tool_calls)
    choice = types.SimpleNamespace(delta=delta, finish_reason=finish_reason)
    return types.SimpleNamespace(choices=[choice], usage=None)


def _client_with_stream(chunks):
    """An OpenAi client whose completions.create replays `chunks`."""
    client = OpenAi.__new__(OpenAi)  # skip __init__ (builds a real SDK client)

    async def _acreate(**kwargs):
        async def _gen():
            for c in chunks:
                yield c
        return _gen()

    client.async_client = types.SimpleNamespace(
        chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=_acreate))
    )
    return client


async def _run(client):
    events = []
    async for evt in client.inference_stream_v2(
        model_id="m", messages=[Message(role="user", content="hi")],
        tools=[ToolSpec(name="t", description="d", input_schema={"type": "object"})],
    ):
        events.append(evt)
    return events


_TOOL_ARGS = '{"a":1}'


@pytest.mark.asyncio
async def test_provider_ids_are_used_verbatim():
    """The normal OpenAI path must be untouched — its ids are what replay needs."""
    events = await _run(_client_with_stream([
        _chunk([_delta(0, call_id="call_abc", name="t", args=_TOOL_ARGS)]),
        _chunk(finish_reason="tool_calls"),
    ]))
    done = [e for e in events if isinstance(e, ToolUseCompleteEvent)]
    assert [e.id for e in done] == ["call_abc"]


@pytest.mark.asyncio
async def test_missing_ids_are_minted_and_unique_per_call():
    """The bug: an endpoint that sends no ids used to yield "" for every call."""
    events = await _run(_client_with_stream([
        _chunk([
            _delta(0, call_id=None, name="t", args=_TOOL_ARGS),
            _delta(1, call_id=None, name="t", args=_TOOL_ARGS),
        ]),
        _chunk(finish_reason="tool_calls"),
    ]))
    done = [e for e in events if isinstance(e, ToolUseCompleteEvent)]
    ids = [e.id for e in done]
    assert len(ids) == 2
    assert all(i for i in ids), "a minted id must never be empty"
    assert len(set(ids)) == 2, f"parallel calls must not share an id: {ids}"


@pytest.mark.asyncio
async def test_minted_id_is_stable_from_start_to_complete():
    """planner_v3 keys action_id_index on the START event and looks it up again
    on COMPLETE. If those differ the completed call never finds its action."""
    events = await _run(_client_with_stream([
        _chunk([_delta(0, call_id=None, name="t")]),
        _chunk([_delta(0, call_id=None, args=_TOOL_ARGS)]),
        _chunk(finish_reason="tool_calls"),
    ]))
    start = next(e for e in events if isinstance(e, ToolUseStartEvent))
    done = next(e for e in events if isinstance(e, ToolUseCompleteEvent))
    assert start.id == done.id


@pytest.mark.asyncio
async def test_late_real_id_does_not_replace_a_minted_one():
    """Adopting a late id would break the start/complete identity above. A real
    id arriving on the FIRST chunk is still used verbatim (test one)."""
    events = await _run(_client_with_stream([
        _chunk([_delta(0, call_id=None, name="t")]),
        _chunk([_delta(0, call_id="call_late", args=_TOOL_ARGS)]),
        _chunk(finish_reason="tool_calls"),
    ]))
    start = next(e for e in events if isinstance(e, ToolUseStartEvent))
    done = next(e for e in events if isinstance(e, ToolUseCompleteEvent))
    assert start.id == done.id
    assert done.id != "call_late"


@pytest.mark.asyncio
async def test_minted_ids_do_not_repeat_across_requests():
    """Index 0 recurs every turn. A bare counter would put a second "call_0" in
    a transcript that already replays one — which is what broke Gemini."""
    chunks = [
        _chunk([_delta(0, call_id=None, name="t", args=_TOOL_ARGS)]),
        _chunk(finish_reason="tool_calls"),
    ]
    first = await _run(_client_with_stream(chunks))
    second = await _run(_client_with_stream(chunks))
    id1 = next(e.id for e in first if isinstance(e, ToolUseCompleteEvent))
    id2 = next(e.id for e in second if isinstance(e, ToolUseCompleteEvent))
    assert id1 != id2
