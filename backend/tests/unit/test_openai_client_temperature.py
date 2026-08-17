"""The OpenAI-compatible client must not send a sampling temperature unless
one was explicitly configured.

This client serves every OpenAI-compatible endpoint (custom providers,
LiteLLM, vLLM, Ollama), where model ids are gateway aliases. A growing set of
models behind those aliases (Claude Sonnet 5+, gpt-5, o-series) rejects any
temperature but their default with a 400 — observed in production as
``litellm.UnsupportedParamsError: ... does not support temperature=0.3.
Only temperature=1 is supported`` — and no name-based detection can recognize
an alias. Omitting the parameter is the only universally safe default.
"""
import pytest

from app.ai.llm.clients.openai_client import OpenAi
from app.ai.llm.llm import _parse_temperature


def _client(**kwargs) -> OpenAi:
    return OpenAi(api_key="test-key", base_url="http://localhost:9999/v1", **kwargs)


class TestBuildChatParams:
    def test_no_temperature_by_default(self):
        params = _client()._build_chat_params(model_id="gpt-4o", prompt="hi")
        assert "temperature" not in params

    def test_no_temperature_for_alias_models(self):
        # The customer-reported case: a LiteLLM alias fronting Claude Sonnet 5.
        params = _client()._build_chat_params(
            model_id="anthropic_claude_sonnet_latest", prompt="hi"
        )
        assert "temperature" not in params

    def test_configured_temperature_is_sent(self):
        params = _client(temperature=0.7)._build_chat_params(model_id="gpt-4o", prompt="hi")
        assert params["temperature"] == 0.7

    def test_configured_zero_temperature_is_sent(self):
        # 0 is a valid, deliberate choice — must not be dropped as falsy.
        params = _client(temperature=0.0)._build_chat_params(model_id="gpt-4o", prompt="hi")
        assert params["temperature"] == 0.0


class TestParseTemperature:
    @pytest.mark.parametrize("raw,expected", [
        (None, None),
        (0.7, 0.7),
        (0, 0.0),
        (1, 1.0),
        ("0.3", 0.3),          # additional_config values may arrive as strings
        ("", None),
        ("hot", None),
        (True, None),           # bool is not a temperature
        (-0.1, None),           # outside the OpenAI-accepted [0, 2] range
        (2.5, None),
        ({}, None),
    ])
    def test_parse(self, raw, expected):
        assert _parse_temperature(raw) == expected
