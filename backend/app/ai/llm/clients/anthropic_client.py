from app.ai.llm.clients.base import LLMClient
from anthropic import Anthropic as AnthropicAPI, AsyncAnthropic
from typing import AsyncGenerator

class Anthropic(LLMClient):
    def __init__(self, api_key: str, base_url: str = None):
        self.client = AnthropicAPI(api_key=api_key)
        self.async_client = AsyncAnthropic(api_key=api_key)
        self.max_tokens = 1024
        self.temperature = 0.3

    def inference(self, model_id: str, prompt: str) -> str:
        message = self.client.messages.create(
            model=model_id,
            messages=[
                {
                    "role": "user",
                    "content": prompt.strip(),
                }
            ],
            max_tokens=self.max_tokens,
            temperature=self.temperature
        )
        if message.content[0].text:
            return message.content[0].text
        else:
            return None
    
    async def inference_stream(self, model_id: str, prompt: str) -> AsyncGenerator[str, None]:
        stream = await self.async_client.messages.create(
            model=model_id,
            messages=[
                {
                    "role": "user",
                    "content": prompt.strip(),
                }
            ],
            max_tokens=self.max_tokens,
            temperature=self.temperature,
            stream=True
        )
        
        async for chunk in stream:
            if chunk.type == 'content_block_delta' and chunk.delta.text:
                yield chunk.delta.text


    async def test_connection(self):
        return True