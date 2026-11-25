"""How a streamed Responses API turn ends is reported faithfully.

Azure OpenAI answers a prompt its content filter blocks (Prompt Shields'
jailbreak detector) with HTTP 200: a canned "I'm sorry, but I cannot assist
with that request." text delta, then ``response.incomplete`` carrying
``incomplete_details.reason == "content_filter"`` — observed live against an
Azure deployment. The stream's stop must say so, or the planner
finalizes the canned text as a successful answer.

Only the SDK call (``responses.create``) is faked: the event sequence below is
the one Azure sent, field for field where it matters.
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.ai.llm.llm import LLM
from app.ai.llm.types import Message, MessageStopEvent, TextDeltaEvent


def _model():
    return SimpleNamespace(
        model_id="gpt-6-luna", is_small_default=False, config={}, organization_id=None, supports_vision=True,
        provider=SimpleNamespace(provider_type="openai", additional_config={},
                                 decrypt_credentials=lambda: ("test-key", None)),
    )


def _usage():
    return SimpleNamespace(input_tokens=900, output_tokens=12, input_tokens_details=None, output_tokens_details=None)


def _stream(text: str, terminal: str, status: str, incomplete_details, content_filters=None):
    async def events():
        yield SimpleNamespace(type="response.output_text.delta", delta=text)
        yield SimpleNamespace(type=terminal, response=SimpleNamespace(
            status=status, usage=_usage(), incomplete_details=incomplete_details,
            content_filters=content_filters))
    return events


# As Azure sent it for a prompt Prompt Shields blocked.
_AZURE_CONTENT_FILTERS = [{
    "blocked": True, "source_type": "prompt",
    "content_filter_results": {
        "hate": {"filtered": False, "severity": "safe"},
        "sexual": {"filtered": False, "severity": "safe"},
        "jailbreak": {"detected": True, "filtered": True},
    },
}]


def _run(events):
    llm = LLM(_model())

    async def create(**kwargs):
        return events()

    async def collect():
        return [e async for e in llm.inference_stream_v2(
            messages=[Message(role="user", content="hey")], should_record=False)]

    with patch.object(llm.client.async_client.responses, "create", create):
        out = asyncio.run(collect())
    stops = [e for e in out if isinstance(e, MessageStopEvent)]
    assert len(stops) == 1
    return out, stops[0]


@pytest.mark.parametrize("details", [{"reason": "content_filter"}, SimpleNamespace(reason="content_filter")],
                         ids=["dict", "object"])
def test_a_content_filtered_stream_stops_with_content_filter(details):
    events = _stream("I'm sorry, but I cannot assist with that request.",
                     "response.incomplete", "incomplete", details, _AZURE_CONTENT_FILTERS)
    _, stop = _run(events)
    assert stop.raw_stop_reason == "content_filter"
    assert stop.stop_reason != "end_turn", "a filtered stream must not look like a finished answer"
    assert stop.content_filter_categories == ["jailbreak"]  # only what fired, not the "safe" ones


@pytest.mark.parametrize("terminal", ["response.incomplete", "response.completed"])
def test_a_token_limited_stream_stops_with_max_tokens(terminal):
    events = _stream("partial answer", terminal, "incomplete", {"reason": "max_output_tokens"})
    _, stop = _run(events)
    assert stop.stop_reason == "max_tokens"
    assert stop.raw_stop_reason == "max_output_tokens"


def test_a_completed_stream_still_ends_the_turn_with_its_text():
    events = _stream("All done.", "response.completed", "completed", None)
    out, stop = _run(events)
    assert stop.stop_reason == "end_turn"
    assert stop.content_filter_categories == []
    assert "".join(e.text for e in out if isinstance(e, TextDeltaEvent)) == "All done."
