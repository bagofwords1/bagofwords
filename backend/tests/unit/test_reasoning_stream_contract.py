"""Provider-boundary contracts for effort and visible reasoning."""
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from app.ai.llm.clients.anthropic_client import Anthropic
from app.ai.llm.clients.openai_client import OpenAi
from app.ai.llm.clients.openai_responses_client import OpenAIResponsesClient
from app.ai.llm.clients.azure_client import AzureClient
from app.ai.llm.types import Message, ReasoningDeltaEvent, TextDeltaEvent


async def stream(events=()):
    for event in events:
        yield event


def make_client(kind):
    if kind == 'anthropic':
        return Anthropic(api_key='test-key')
    if kind == 'responses':
        return OpenAIResponsesClient(api_key='test-key')
    if kind == 'azure':
        return AzureClient(api_key='test-key', endpoint_url='https://example.openai.azure.com')
    return OpenAi(api_key='test-key', base_url='https://example.test/v1')


def capture(client, kind, events=()):
    create = AsyncMock(side_effect=lambda **kw: stream(events))
    if kind == 'anthropic':
        client.async_client.messages.create = create
    elif kind == 'responses':
        client.async_client.responses.create = create
    else:
        client.async_client.chat.completions.create = create
    return create


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['anthropic', 'responses', 'custom', 'azure'])
@pytest.mark.parametrize('effort', ['low', 'medium', 'high'])
async def test_selected_effort_reaches_provider(kind, effort):
    client = make_client(kind)
    create = capture(client, kind)
    model = 'claude-sonnet-5' if kind == 'anthropic' else 'gpt-5.2'
    async for _ in client.inference_stream_v2(model, [Message(role='user', content='hello')], thinking={'type': 'adaptive', 'effort': effort}):
        pass
    params = create.call_args.kwargs
    if kind == 'anthropic':
        assert params['extra_body']['output_config']['effort'] == effort
        assert 'effort' not in params['extra_body']['thinking']
    elif kind == 'responses':
        assert params['reasoning']['effort'] == effort
    else:
        assert params['reasoning_effort'] == effort
    await client.async_client.close()
    client.client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['anthropic', 'responses'])
async def test_summary_requested_without_effort_override(kind):
    client = make_client(kind)
    create = capture(client, kind)
    model = 'claude-sonnet-5' if kind == 'anthropic' else 'gpt-5.2'
    async for _ in client.inference_stream_v2(model, [Message(role='user', content='hello')]):
        pass
    params = create.call_args.kwargs
    if kind == 'anthropic':
        assert params['extra_body']['thinking']['display'] == 'summarized'
        assert 'output_config' not in params['extra_body']
    else:
        assert params['reasoning'] == {'summary': 'auto'}
    await client.async_client.close()
    client.client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['custom', 'azure'])
@pytest.mark.parametrize('field', ['reasoning_content', 'reasoning'])
async def test_reasoning_is_separate_from_answer(kind, field):
    client = make_client(kind)
    def chunk(**delta):
        return NS(usage=None, choices=[NS(finish_reason=None, delta=NS(content=None, tool_calls=None, **delta))])
    capture(client, kind, [chunk(**{field: 'Checking constraints.'}), NS(usage=None, choices=[NS(finish_reason='stop', delta=NS(content='42', tool_calls=None))])])
    events = [e async for e in client.inference_stream_v2('some-custom-model', [Message(role='user', content='hello')])]
    assert ''.join(e.text for e in events if isinstance(e, ReasoningDeltaEvent)) == 'Checking constraints.'
    assert ''.join(e.text for e in events if isinstance(e, TextDeltaEvent)) == '42'
    await client.async_client.close()
    client.client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['responses', 'custom', 'azure'])
@pytest.mark.parametrize('model', ['gpt-4.1', 'arbitrary-deployment-name'])
async def test_unknown_models_do_not_receive_unsupported_parameters(kind, model):
    client = make_client(kind)
    create = capture(client, kind)
    async for _ in client.inference_stream_v2(model, [Message(role='user', content='hello')], thinking={'type': 'enabled', 'budget_tokens': 15000}):
        pass
    assert 'reasoning_effort' not in create.call_args.kwargs
    assert 'reasoning' not in create.call_args.kwargs
    await client.async_client.close()
    client.client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['responses', 'custom', 'azure'])
async def test_configured_deployment_capability_preserves_request_model(kind):
    client = make_client(kind)
    client.reasoning_model_id = 'gpt-5.2'
    create = capture(client, kind)
    async for _ in client.inference_stream_v2('deployment-west', [Message(role='user', content='hello')], thinking={'type': 'enabled', 'budget_tokens': 15000}):
        pass
    params = create.call_args.kwargs
    assert params['model'] == 'deployment-west'
    assert (params['reasoning']['effort'] if kind == 'responses' else params['reasoning_effort']) == 'high'
    assert 'temperature' not in params
    await client.async_client.close()
    client.client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['responses', 'custom', 'azure'])
async def test_reported_reasoning_usage_survives_stream(kind):
    from app.ai.llm.types import UsageEvent
    client = make_client(kind)
    if kind == 'responses':
        events = [NS(type='response.completed', response=NS(status='completed', usage=NS(input_tokens=12, output_tokens=21, output_tokens_details=NS(reasoning_tokens=17))))]
    else:
        events = [NS(choices=[], usage=NS(prompt_tokens=12, completion_tokens=21, completion_tokens_details=NS(reasoning_tokens=17)))]
    capture(client, kind, events)
    results = [e async for e in client.inference_stream_v2('gpt-5.2', [Message(role='user', content='hello')])]
    usage = next(e for e in results if isinstance(e, UsageEvent))
    assert usage.reasoning_tokens == 17
    assert usage.output_tokens == 21  # reasoning already included; don't double-count
    await client.async_client.close()
    client.client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('explicit,prompt,default,expected', [
    (None, 'Please think carefully about this table', None, 'high'),
    ('low', 'Please think carefully about this table', 'medium', 'low'),
    (None, 'Create the table', 'medium', 'medium'),
    ('off', 'Think hard about this table', 'high', None),
])
@pytest.mark.parametrize('method', ['generate_code', 'generate_inspection_code', 'generate_transform_code'])
async def test_coder_inherits_effort_and_keeps_summary_out_of_code(explicit, prompt, default, expected, method):
    from app.ai.agents.coder.coder import Coder
    from app.ai.llm.types import ReasoningCompleteEvent
    provider = NS(provider_type='openai', additional_config={}, decrypt_credentials=lambda: ('test-key', ''))
    model = NS(model_id='gpt-5.2', provider=provider, config={'reasoning_effort': default})
    settings = NS(get_config=lambda _: NS(value=True))
    summaries = []
    async def collect(event):
        summaries.append(event)
    coder = Coder(model, settings, reasoning_effort=explicit, reasoning_callback=collect)
    code = 'def generate_df(ds_clients, excel_files):\n    return None'
    events = [
        NS(type='response.output_item.added', item=NS(type='reasoning')),
        NS(type='response.reasoning_summary_text.delta', delta='Checking the schema.'),
        NS(type='response.output_item.done', item=NS(type='reasoning')),
        NS(type='response.output_text.delta', delta=code),
    ]
    create = capture(coder.llm.client, 'responses', events)
    kwargs = dict(prompt=prompt, schemas='', ds_clients={}, excel_files=[], code_and_error_messages=[], memories='', previous_messages='', retries=0)
    if method == 'generate_code':
        from app.ai.schemas.codegen import CodeGenContext
        kwargs.update(data_model={}, interpreted_prompt=prompt, context=CodeGenContext(user_prompt=prompt, schemas_excerpt=''))
    result = await getattr(coder, method)(**kwargs)
    assert create.call_args.kwargs['reasoning'].get('effort') == expected
    assert 'Checking the schema.' not in result
    assert 'def generate_df' in result
    assert ''.join(e.text for e in summaries if isinstance(e, ReasoningDeltaEvent)) == 'Checking the schema.'
    await coder.llm.client.async_client.close()
    coder.llm.client.client.close()


def test_custom_provider_can_explicitly_select_responses():
    from app.ai.llm.llm import LLM
    provider = NS(provider_type='custom', additional_config={'base_url': 'https://gateway.example/v1', 'use_responses_api': True}, decrypt_credentials=lambda: ('test-key', ''))
    model = NS(model_id='deployment-east', provider=provider, config={'reasoning_model_id': 'gpt-5.2'})
    llm = LLM(model)
    assert isinstance(llm.client, OpenAIResponsesClient)
    assert str(llm.client.client.base_url).rstrip('/') == 'https://gateway.example/v1'
    assert llm.client.reasoning_model_id == 'gpt-5.2'


@pytest.mark.asyncio
@pytest.mark.parametrize('model', ['claude-sonnet-5', 'claude-opus-4-7', 'claude-opus-5'])
async def test_modern_anthropic_remaps_legacy_budget_after_model_switch(model):
    client = make_client('anthropic')
    create = capture(client, 'anthropic')
    async for _ in client.inference_stream_v2(model, [Message(role='user', content='hello')], thinking={'type': 'enabled', 'budget_tokens': 15000}):
        pass
    body = create.call_args.kwargs['extra_body']
    assert body['thinking'] == {'type': 'adaptive', 'display': 'summarized'}
    assert body['output_config']['effort'] == 'high'
    await client.async_client.close()
    client.client.close()


@pytest.mark.asyncio
async def test_older_anthropic_keeps_supported_manual_budget():
    client = make_client('anthropic')
    create = capture(client, 'anthropic')
    async for _ in client.inference_stream_v2('claude-sonnet-4-20250514', [Message(role='user', content='hello')], thinking={'type': 'enabled', 'budget_tokens': 5000, 'effort': 'medium'}):
        pass
    body = create.call_args.kwargs['extra_body']
    assert body['thinking']['budget_tokens'] == 5000
    assert 'effort' not in body['thinking']
    assert 'output_config' not in body
    await client.async_client.close()
    client.client.close()
