"""Small-default policy is enforced on provider requests across APIs."""
import asyncio
from types import SimpleNamespace
from unittest.mock import patch
import pytest
from app.ai.llm.llm import LLM
from app.ai.llm.types import Message


def model(small, model_id='gpt-6-luna', provider='openai'):
    return SimpleNamespace(model_id=model_id, is_small_default=small, config={},
        organization_id=None, supports_vision=True,
        provider=SimpleNamespace(provider_type=provider, additional_config={},
                                 decrypt_credentials=lambda: ('test-key', None)))


@pytest.mark.parametrize('small', [True, False])
@pytest.mark.parametrize('api', ['sync', 'stream', 'v2'])
def test_small_default_uses_minimum_on_every_api(small, api):
    llm = LLM(model(small))
    requests = []
    def capture(**kwargs):
        requests.append(kwargs)
        raise ValueError('provider boundary reached')
    async def acapture(**kwargs):
        return capture(**kwargs)
    async def run():
        if api == 'v2':
            async for _ in llm.inference_stream_v2(
                messages=[Message(role='user', content='think hard')],
                thinking={'type': 'enabled', 'effort': 'high', 'budget_tokens': 15000},
                should_record=False,
            ): pass
        else:
            async for _ in llm.inference_stream('hello', should_record=False): pass
    with patch.object(llm.client.client.chat.completions, 'create', capture), \
         patch.object(llm.client.async_client.responses, 'create', acapture), \
         patch.object(llm.client.async_client.chat.completions, 'create', acapture):
        with pytest.raises((ValueError, RuntimeError)):
            if api == 'sync': llm.inference('hello', should_record=False)
            else: asyncio.run(run())
    assert requests
    request = requests[0]
    effort = request.get('reasoning_effort') or request.get('reasoning', {}).get('effort')
    assert effort == ('none' if small else 'high' if api == 'v2' else None)

@pytest.mark.parametrize('model_id,provider,expected', [
    ('gpt-6-luna','openai', 'none'),
    ('o3','openai','low'),
    ('claude-sonnet-4-6','anthropic','disabled'),
    ('claude-fable-5-1','anthropic','low'),
])
@pytest.mark.parametrize('small,default', [(True,None),(False,'off')])
def test_sync_small_or_judge_policy_reaches_provider(model_id, provider, expected, small, default):
    llm = LLM(model(small,model_id,provider), reasoning_effort=default)
    requests=[]
    def capture(**kwargs):
        requests.append(kwargs)
        raise ValueError('provider boundary reached')
    endpoint = llm.client.client.messages if provider=='anthropic' else llm.client.client.chat.completions
    with patch.object(endpoint,'create',capture), pytest.raises(RuntimeError):
        llm.inference('Judge this result', should_record=False)
    request=requests[0]
    extra=request.get('extra_body',{})
    effort=request.get('reasoning_effort') or extra.get('output_config',{}).get('effort') or extra.get('thinking',{}).get('type')
    assert effort==expected


def test_legacy_small_stream_returns_provider_text():
    llm=LLM(model(True))
    async def events():
        yield SimpleNamespace(type='response.output_text.delta',delta='answer')
        yield SimpleNamespace(type='response.completed',response=SimpleNamespace(
            usage=SimpleNamespace(input_tokens=11,output_tokens=2,input_tokens_details=None,output_tokens_details=None)))
    async def create(**kwargs): return events()
    async def run():
        return ''.join([c async for c in llm.inference_stream('question',should_record=False)])
    with patch.object(llm.client.async_client.responses,'create',create):
        assert asyncio.run(run())=='answer'

@pytest.mark.parametrize('provider', ['azure','google','bedrock'])
def test_sync_adapters_translate_explicit_minimum(provider):
    from app.ai.llm.clients.azure_client import AzureClient
    from app.ai.llm.clients.google_client import Google
    from app.ai.llm.clients.bedrock_client import BedrockClient
    requests=[]
    def capture(**kwargs):
        requests.append(kwargs)
        raise ValueError('provider boundary reached')
    cls={'azure':AzureClient,'google':Google,'bedrock':BedrockClient}[provider]
    client=cls.__new__(cls)
    client.temperature=0.3
    client.reasoning_mode='like' if provider=='azure' else 'auto'
    client.reasoning_model_id='gpt-6-luna'
    client.client=SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=capture)),
        models=SimpleNamespace(generate_content=capture),converse=capture,
    )
    model_id={'azure':'opaque-deployment','google':'gemini-2.5-flash','bedrock':'anthropic.claude-sonnet-4-6'}[provider]
    with pytest.raises(ValueError,match='provider boundary reached'):
        client.inference(model_id,'question',thinking={'type':'disabled'})
    request=requests[0]
    if provider=='azure':
        assert request['reasoning_effort']=='none'
        assert 'temperature' not in request
    elif provider=='google':
        assert request['config'].thinking_config.thinking_budget==Google.MIN_THINKING_BUDGET
    else:
        assert request['additionalModelRequestFields']['thinking']=={'type':'disabled'}


@pytest.mark.parametrize('effort', ['off','medium','high'])
def test_main_model_inherits_explicit_constructor_policy(effort):
    llm=LLM(model(False),reasoning_effort=effort)
    requests=[]
    async def capture(**kwargs):
        requests.append(kwargs)
        raise ValueError('provider boundary reached')
    async def run():
        async for _ in llm.inference_stream_v2(messages=[Message(role='user',content='hello')],should_record=False): pass
    with patch.object(llm.client.async_client.responses,'create',capture),pytest.raises(RuntimeError):
        asyncio.run(run())
    assert requests[0]['reasoning']['effort']==('none' if effort=='off' else effort)
