"""Unit tests for AzureClient.inference_stream_v2 chunk handling.

Azure OpenAI can emit streaming chunks whose `choices` array is non-empty but
whose `choice.delta` is `None` (e.g. content-filter / annotation-only packets).
The client must skip these instead of raising
"'NoneType' object has no attribute 'content'".
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.ai.llm.clients.azure_client import AzureClient
from app.ai.llm.types import Message, TextDeltaEvent


def _chunk(*, choices=None, usage=None):
    return SimpleNamespace(choices=choices or [], usage=usage)


def _choice(*, delta, finish_reason=None):
    return SimpleNamespace(delta=delta, finish_reason=finish_reason)


class _FakeStream:
    def __init__(self, chunks):
        self._chunks = chunks

    def __aiter__(self):
        return self._aiter()

    async def _aiter(self):
        for chunk in self._chunks:
            yield chunk


def _make_client() -> AzureClient:
    client = AzureClient.__new__(AzureClient)
    # LLMClient base sets up usage bookkeeping via __init__; call it directly
    # to avoid constructing the real AzureOpenAI SDK clients.
    from app.ai.llm.clients.base import LLMClient

    LLMClient.__init__(client)
    client.async_client = MagicMock()
    return client


@pytest.mark.asyncio
async def test_none_delta_chunk_is_skipped():
    """A chunk with a non-empty choices array but delta=None must not raise."""
    client = _make_client()

    chunks = [
        # Content-filter style packet: choice present, delta is None.
        _chunk(choices=[_choice(delta=None)]),
        _chunk(choices=[_choice(delta=SimpleNamespace(content="hello", tool_calls=None))]),
        _chunk(choices=[_choice(delta=None, finish_reason="content_filter")]),
        _chunk(choices=[_choice(delta=SimpleNamespace(content=None, tool_calls=None), finish_reason="stop")]),
    ]

    async def _create(**_kwargs):
        return _FakeStream(chunks)

    client.async_client.chat.completions.create = _create

    events = []
    async for evt in client.inference_stream_v2(
        model_id="gpt-5-mini",
        messages=[Message(role="user", content="hi")],
    ):
        events.append(evt)

    text_events = [e for e in events if isinstance(e, TextDeltaEvent)]
    assert [e.text for e in text_events] == ["hello"]
