
from openai import AsyncOpenAI, OpenAI
from typing import AsyncGenerator
from app.ai.llm.clients.base import LLMClient
from langsmith.wrappers import wrap_openai
from langsmith import traceable
from functools import wraps

from app.settings.config import settings

def conditional_trace(func):

    if settings.ENVIRONMENT != "production":
        return traceable(func)
    return func


class Bow(LLMClient):
    def __init__(self, api_key: str, base_url: str = "https://api.openai.com/v1"):
        self.client = OpenAI(api_key=api_key, base_url=base_url)
        
        if settings.ENVIRONMENT != "production":
            self.async_client = wrap_openai(AsyncOpenAI(api_key=api_key, base_url=base_url))
        else:
            self.async_client = AsyncOpenAI(api_key=api_key, base_url=base_url)

    def inference(self, model_id: str, prompt: str) -> str:
        chat_completion = self.client.chat.completions.create(
            messages=[
                {
                    "role": "user",
                    "content": prompt.strip(),
                }
            ],
            model=model_id,
            temperature=0.7
        )
        return chat_completion.choices[0].message.content
    
    @traceable
    async def inference_stream(self, model_id: str, prompt: str) -> AsyncGenerator[str, None]:
        stream = await self.async_client.chat.completions.create(
            messages=[
                {
                    "role": "user",
                    "content": prompt.strip(),
                }
            ],
            model=model_id,
            temperature=0.7,
            stream=True
        )
        
        async for chunk in stream:
            if not chunk.choices:
                continue  # skip heartbeat/control packets
            
            content = chunk.choices[0].delta.content
            if content is not None:
                yield content

    def test_connection(self):
        return True