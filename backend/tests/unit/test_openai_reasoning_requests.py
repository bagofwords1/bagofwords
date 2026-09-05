"""Reasoning models must receive supported parameters on every inference path."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.ai.llm.clients.openai_responses_client import OpenAIResponsesClient
from app.ai.llm.types import Message


async def empty_stream():
    if False:
        yield


@pytest.mark.asyncio
@pytest.mark.parametrize('model_id', ['gpt-6-astra', 'gpt-6-astra-2026-09-05'])
@pytest.mark.parametrize('temperature', [None, 0, 0.7])
@pytest.mark.parametrize('path', ['sync', 'stream', 'tools'])
async def test_reasoning_requests_omit_sampling_and_honor_thinking(model_id, temperature, path):
    client = OpenAIResponsesClient(api_key='test-key', temperature=temperature)
    response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='ok'))])
    sync_create = Mock(return_value=response)
    async_create = AsyncMock(side_effect=lambda **kw: empty_stream())
    client.client.chat.completions.create = sync_create
    client.async_client.chat.completions.create = async_create
    client.async_client.responses.create = async_create
    if path == 'sync':
        assert client.inference(model_id, 'hello').text
        params = sync_create.call_args.kwargs
    elif path == 'stream':
        async for _ in client.inference_stream(model_id, 'hello'):
            pass
        params = async_create.call_args.kwargs
    else:
        async for _ in client.inference_stream_v2(
            model_id, [Message(role='user', content='hello')],
            thinking={'type': 'adaptive'},
        ):
            pass
        params = async_create.call_args.kwargs
        assert params['reasoning']['effort'] in {'low', 'medium', 'high', 'xhigh', 'max'}
    assert 'temperature' not in params
    await client.async_client.close()
    client.client.close()


@pytest.mark.parametrize('base_url', [None, 'https://gateway.example/v1'])
def test_astra_openai_provider_uses_responses_with_configured_endpoint(base_url):
    from app.ai.llm.llm import LLM

    # Only the provider configuration is needed; no database operations occur.
    provider = SimpleNamespace(
        provider_type='openai', additional_config={'base_url': base_url},
        decrypt_credentials=lambda: ('test-key', ''),
    )
    model = SimpleNamespace(model_id='gpt-6-astra', provider=provider, config={})
    llm = LLM(model)
    assert isinstance(llm.client, OpenAIResponsesClient)
    assert str(llm.client.client.base_url).rstrip('/') == (base_url or 'https://api.openai.com/v1')
