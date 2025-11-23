from typing import AsyncGenerator

from google import genai
from google.genai import types

from app.ai.llm.clients.base import LLMClient
from app.ai.llm.types import LLMResponse, LLMUsage


class Google(LLMClient):
    def __init__(self, api_key: str | None = None):
        super().__init__()
        self.client = genai.Client(api_key=api_key)
        self.temperature = 0.3

    def inference(self, model_id: str, prompt: str) -> LLMResponse:
        thinking_budget = 128 if "pro" in model_id else 0

        response = self.client.models.generate_content(
            model=model_id,
            contents=prompt.strip(),
            config=types.GenerateContentConfig(
                thinking_config=types.ThinkingConfig(thinking_budget=thinking_budget),
                temperature=self.temperature,
            ),
        )
        usage_meta = getattr(response, "usage_metadata", None)
        usage = LLMUsage(
            prompt_tokens=getattr(usage_meta, "prompt_token_count", 0) if usage_meta else 0,
            completion_tokens=getattr(usage_meta, "candidates_token_count", 0) if usage_meta else 0,
        )
        self._set_last_usage(usage)
        text = getattr(response, "text", "") or ""
        return LLMResponse(text=text, usage=usage)

    async def inference_stream(self, model_id: str, prompt: str) -> AsyncGenerator[str, None]:
        thinking_budget = 128 if "pro" in model_id else 0

        prompt_tokens = 0
        completion_tokens = 0
        for chunk in self.client.models.generate_content_stream(
            model=model_id,
            contents=[prompt.strip()],
            config=types.GenerateContentConfig(
                thinking_config=types.ThinkingConfig(thinking_budget=thinking_budget),
                temperature=self.temperature,
            ),
        ):
            text = getattr(chunk, "text", None)
            if text:
                yield text
            usage_meta = getattr(chunk, "usage_metadata", None)
            if usage_meta:
                prompt_tokens = getattr(usage_meta, "prompt_token_count", prompt_tokens) or prompt_tokens
                completion_tokens = getattr(usage_meta, "candidates_token_count", completion_tokens) or completion_tokens

        self._set_last_usage(
            LLMUsage(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
            )
        )

