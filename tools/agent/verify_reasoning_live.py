"""Bounded synthetic API checks. Run from backend with the appropriate key in
OPENAI_API_KEY or ANTHROPIC_API_KEY; never prints credentials or response text.
Usage: .venv/bin/python ../tools/agent/verify_reasoning_live.py openai|anthropic
"""
import asyncio, json, logging, os, sys, time
sys.path.insert(0, os.getcwd())
from app.ai.llm.clients.anthropic_client import Anthropic
from app.ai.llm.clients.openai_client import OpenAi
from app.ai.llm.clients.openai_responses_client import OpenAIResponsesClient
from app.ai.llm.reasoning import _effort_to_thinking_config
from app.ai.llm.types import Message, ReasoningDeltaEvent, TextDeltaEvent, UsageEvent, MessageStopEvent
logging.disable(logging.CRITICAL)
provider = sys.argv[1]
if provider not in {'openai', 'anthropic'}:
    raise SystemExit('Choose openai or anthropic')
key = os.environ['ANTHROPIC_API_KEY' if provider == 'anthropic' else 'OPENAI_API_KEY']
async def main():
    failed = False
    model = 'claude-sonnet-5' if provider == 'anthropic' else 'gpt-5-mini'
    kinds = ['anthropic'] if provider == 'anthropic' else ['responses', 'chat']
    for kind in kinds:
        client = Anthropic(api_key=key) if kind == 'anthropic' else OpenAIResponsesClient(api_key=key) if kind == 'responses' else OpenAi(api_key=key)
        if kind == 'anthropic': client.max_tokens = 3072
        target = client.async_client.messages if kind == 'anthropic' else client.async_client.responses if kind == 'responses' else client.async_client.chat.completions
        original = target.create
        sent = {}
        async def capped(_create=original, **kwargs):
            if kind == 'responses': kwargs['max_output_tokens'] = 3072
            if kind == 'chat': kwargs['max_completion_tokens'] = 3072
            sent.update({k: kwargs[k] for k in ['reasoning', 'reasoning_effort', 'extra_body'] if k in kwargs})
            return await _create(**kwargs)
        target.create = capped
        for effort in ['low', 'high']:
            started = time.monotonic(); reason=''; answer=''; usage={}; stop=None; sent.clear()
            try:
                async with asyncio.timeout(90):
                    async for e in client.inference_stream_v2(model, [Message(role='user', content='Find the smallest positive integer n such that n mod 7 = 3, n mod 11 = 5, and n mod 13 = 7. Verify all three remainders. Give the integer and verification in at most 80 words.')], thinking=_effort_to_thinking_config(effort, model)):
                        if isinstance(e, ReasoningDeltaEvent): reason += e.text
                        if isinstance(e, TextDeltaEvent): answer += e.text
                        if isinstance(e, UsageEvent): usage = vars(e)
                        if isinstance(e, MessageStopEvent): stop = e.stop_reason
                expected = next(n for n in range(1,1002) if n%7==3 and n%11==5 and n%13==7)
                failed |= str(expected) not in answer or stop == 'max_tokens'
                print(json.dumps(dict(client=kind, model=model, effort=effort, elapsed_s=round(time.monotonic()-started,2), reasoning_chars=len(reason), answer_chars=len(answer), correct=str(expected) in answer, stop=stop, usage=usage, settings=sent)), flush=True)
            except Exception as exc:
                failed = True
                print(json.dumps(dict(client=kind, effort=effort, error=type(exc).__name__, status=getattr(exc,'status_code',None))),flush=True)
        await client.async_client.close();client.client.close()
    return failed

if __name__ == "__main__":
    raise SystemExit(1 if asyncio.run(main()) else 0)
