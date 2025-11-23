from typing import AsyncGenerator, Any

from anthropic import Anthropic as AnthropicAPI, AsyncAnthropic

from app.ai.llm.clients.base import LLMClient
from app.ai.llm.types import LLMResponse, LLMUsage


class Anthropic(LLMClient):
    def __init__(self, api_key: str, base_url: str = None):
        super().__init__()
        self.client = AnthropicAPI(api_key=api_key)
        self.async_client = AsyncAnthropic(api_key=api_key)
        self.max_tokens = 1024
        self.temperature = 0.3

    def inference(self, model_id: str, prompt: str) -> LLMResponse:
        message = self.client.messages.create(
            model=model_id,
            messages=[
                {
                    "role": "user",
                    "content": prompt.strip(),
                }
            ],
            max_tokens=self.max_tokens,
            temperature=self.temperature,
        )
        usage = self._extract_usage(getattr(message, "usage", None))
        self._set_last_usage(usage)
        text = message.content[0].text if message.content and message.content[0].text else ""
        return LLMResponse(text=text, usage=usage)

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
            stream=True,
        )

        prompt_tokens = 0
        completion_tokens = 0
        async for chunk in stream:
            if chunk.type == "content_block_delta" and chunk.delta.text:
                yield chunk.delta.text
            usage = self._extract_usage(getattr(chunk, "usage", None))
            if usage.prompt_tokens or usage.completion_tokens:
                prompt_tokens = usage.prompt_tokens or prompt_tokens
                completion_tokens = usage.completion_tokens or completion_tokens

        self._set_last_usage(
            LLMUsage(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
            )
        )

    @staticmethod
    def _extract_usage(raw: Any) -> LLMUsage:
        if raw is None:
            return LLMUsage()
        if isinstance(raw, dict):
            return LLMUsage(
                prompt_tokens=int(raw.get("input_tokens", 0) or 0),
                completion_tokens=int(raw.get("output_tokens", 0) or 0),
            )
        prompt = getattr(raw, "input_tokens", 0)
        completion = getattr(raw, "output_tokens", 0)
        return LLMUsage(prompt_tokens=int(prompt or 0), completion_tokens=int(completion or 0))

    async def test_connection(self):
        return True