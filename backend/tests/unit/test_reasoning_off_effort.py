"""Reasoning "off" on models that always think.

Sonnet 5 / Opus 4.7+ / Fable 5 cannot turn thinking off, and with no effort in
the request the provider runs them at its default (high). "Off" must ask for
the least effort instead, while any explicit effort — a trigger phrase, the
per-completion setting, the model default — still decides.

The provider SDK is the mocked boundary; the request it receives is the
contract.
"""
import asyncio

import pytest

from app.ai.llm.clients.anthropic_client import Anthropic
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


ALWAYS_THINKING = ["claude-sonnet-5", "claude-opus-4-8", "claude-fable-5-1"]


@pytest.mark.parametrize("model_id", ALWAYS_THINKING)
def test_off_asks_always_thinking_models_for_low_effort(model_id):
    request = _request_for(model_id)
    assert request["extra_body"]["thinking"]["type"] == "adaptive"
    assert _effort(request) == "low"


@pytest.mark.parametrize("model_id", ALWAYS_THINKING)
@pytest.mark.parametrize("prompt", ["think hard about churn", "please be thorough here"])
def test_trigger_phrase_still_raises_effort(model_id, prompt):
    assert _effort(_request_for(model_id, prompt=prompt)) == "high"


@pytest.mark.parametrize("model_id", ALWAYS_THINKING)
@pytest.mark.parametrize("source", ["per_completion", "model_default"])
def test_an_explicit_effort_is_never_lowered(model_id, source):
    assert _effort(_request_for(model_id, **{source: "medium"})) == "medium"


@pytest.mark.parametrize("model_id", ["claude-haiku-4-5-20251001", "claude-sonnet-4-6"])
def test_models_that_can_stop_thinking_still_send_none(model_id):
    request = _request_for(model_id)
    assert "thinking" not in (request.get("extra_body") or {})
    assert _effort(request) is None
