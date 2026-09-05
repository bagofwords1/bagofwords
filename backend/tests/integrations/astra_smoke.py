"""Run from backend: PYTHONPATH=. python tests/integrations/astra_smoke.py.

Uses OPENAI_API_KEY from the environment, or a non-echoing terminal prompt.
Only synthetic prompts are sent. Never prints credentials or raw API errors.
"""
import asyncio
import getpass
import os

from app.ai.llm.clients.openai_responses_client import OpenAIResponsesClient
from app.ai.llm.types import Message, TextDeltaEvent, ToolSpec, ToolUseCompleteEvent


async def main():
    key = os.environ.get('OPENAI_API_KEY') or getpass.getpass('OpenAI API key: ')
    client = OpenAIResponsesClient(api_key=key, temperature=0.7)
    client.client = client.client.with_options(timeout=60, max_retries=0)
    client.async_client = client.async_client.with_options(timeout=60, max_retries=0)
    model = 'gpt-6-astra'
    try:
        result = await asyncio.to_thread(client.inference, model, 'Reply with OK only.')
        assert result.text.strip()
        print('PASS plain inference', flush=True)
        text = ''.join([chunk async for chunk in client.inference_stream(model, 'Reply with OK only.')])
        assert text.strip()
        print('PASS streaming inference (connection-test path)', flush=True)
        messages = [Message(role='user', content='Call lookup_code to retrieve the code, then report it. Do not guess.')]
        tools = [ToolSpec(name='lookup_code', description='Return the verification code.', input_schema={'type':'object','properties':{},'required':[], 'additionalProperties':False})]
        events = [event async for event in client.inference_stream_v2(model, messages, tools=tools, thinking={'type':'adaptive'})]
        calls = [event for event in events if isinstance(event, ToolUseCompleteEvent)]
        assert calls, 'No tool call received'
        call = calls[0]
        messages.extend([
            Message(role='assistant', content=[{'type':'tool_use','id':call.id,'name':call.name,'input':call.input}]),
            Message(role='user', content=[{'type':'tool_result','tool_use_id':call.id,'content':'ASTRA-7391'}]),
        ])
        events = [event async for event in client.inference_stream_v2(model, messages, tools=tools, thinking={'type':'adaptive'})]
        answer = ''.join(event.text for event in events if isinstance(event, TextDeltaEvent))
        assert 'ASTRA-7391' in answer
        print('PASS Responses reasoning + tool-result round trip', flush=True)
    except Exception as exc:
        print(f'FAIL {type(exc).__name__}; status={getattr(exc, "status_code", None)}; code={getattr(exc, "code", None)}', flush=True)
        raise SystemExit(1) from None
    finally:
        await client.async_client.close()
        client.client.close()


if __name__ == '__main__':
    asyncio.run(main())
