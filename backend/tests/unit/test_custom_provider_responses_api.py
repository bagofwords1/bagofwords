import types

import pytest

from app.ai.llm.clients.openai_client import OpenAi
from app.ai.llm.clients.openai_responses_client import OpenAIResponsesClient
from app.ai.llm.llm import LLM


def _custom_model(**additional_config) -> types.SimpleNamespace:
    provider = types.SimpleNamespace(
        id="p1",
        provider_type="custom",
        additional_config={"base_url": "https://nim.example.com/v1", **additional_config},
        decrypt_credentials=lambda: ("", None),
    )
    return types.SimpleNamespace(model_id="openai/gpt-oss-120b", provider=provider, organization_id="o1", config={})


@pytest.mark.parametrize("config, expected", [
    ({"use_responses_api": True}, OpenAIResponsesClient),
    ({"use_responses_api": False}, OpenAi),
    ({}, OpenAi),
])
def test_custom_provider_client_follows_responses_api_opt_in(config, expected):
    """Custom providers stay on Chat Completions unless the admin opts in."""
    assert type(LLM(_custom_model(**config)).client) is expected
