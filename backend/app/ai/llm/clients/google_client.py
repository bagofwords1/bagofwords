from typing import AsyncGenerator
from app.ai.llm.clients.base import LLMClient
from google import genai
from google.genai import types

class Google(LLMClient):
    def __init__(self, api_key: str | None = None):
        self.client = genai.Client(api_key=api_key) 
        self.temperature = 0.3


    def inference(self, model_id: str, prompt: str) -> str:
        if 'pro' in model_id:
            self.thinking_budget = 128
        else:
            self.thinking_budget = 0

        response = self.client.models.generate_content(
            model=model_id,
            contents=prompt.strip(),
            config=types.GenerateContentConfig(
                thinking_config=types.ThinkingConfig(thinking_budget=self.thinking_budget),
                temperature=self.temperature
            ),
        )
        # The SDK returns a response object with .text aggregated
        return getattr(response, "text", "") or ""

    async def inference_stream(self, model_id: str, prompt: str) -> AsyncGenerator[str, None]:
        if 'pro' in model_id:
            self.thinking_budget = 128
        else:
            self.thinking_budget = 0

        for chunk in self.client.models.generate_content_stream(
            model=model_id,
            contents=[prompt.strip()],
            config=types.GenerateContentConfig(
                thinking_config=types.ThinkingConfig(thinking_budget=self.thinking_budget),
                temperature=self.temperature
            ),
        ):
            text = getattr(chunk, "text", None)
            if text:
                yield text

