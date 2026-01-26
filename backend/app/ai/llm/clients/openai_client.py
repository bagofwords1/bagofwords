from typing import AsyncGenerator, Any, Optional

from openai import AsyncOpenAI, OpenAI

from app.ai.llm.clients.base import LLMClient
from app.ai.llm.types import LLMResponse, LLMUsage, ImageInput


class OpenAi(LLMClient):
    def __init__(self, api_key: str, base_url: str = "https://api.openai.com/v1"):
        super().__init__()
        self.client = OpenAI(api_key=api_key, base_url=base_url)
        self.async_client = AsyncOpenAI(api_key=api_key, base_url=base_url)

    @staticmethod
    def _build_content(prompt: str, images: Optional[list[ImageInput]] = None) -> str | list[dict[str, Any]]:
        """Build message content, either as string or multimodal content array."""
        if not images:
            return prompt.strip()

        content: list[dict[str, Any]] = [{"type": "text", "text": prompt.strip()}]
        for img in images:
            if img.source_type == "url":
                image_url = img.data
            else:
                # base64 data URL format
                image_url = f"data:{img.media_type};base64,{img.data}"
            content.append({
                "type": "image_url",
                "image_url": {"url": image_url}
            })
        return content

    @staticmethod
    def _build_chat_params(
        model_id: str,
        prompt: str,
        *,
        images: Optional[list[ImageInput]] = None,
        stream: bool = False
    ) -> dict[str, Any]:
        """
        Build parameters for OpenAI chat completions, including optional reasoning settings.

        We only pass `reasoning_effort` for models that support OpenAI's reasoning API
        to avoid API errors for non-reasoning models.
        """
        temperature = 1 if model_id == "gpt-5" else 0.3

        params: dict[str, Any] = {
            "messages": [
                {
                    "role": "user",
                    "content": OpenAi._build_content(prompt, images),
                }
            ],
            "model": model_id,
            "temperature": temperature,
        }

        if stream:
            params["stream"] = True

        # Enable medium reasoning effort for reasoning-capable models.
        # Adjust this predicate as you add/change reasoning models.
        if model_id.startswith(("o1", "o3")) or model_id in {"o1", "o3"}:
            params["reasoning_effort"] = "medium"

        return params

    def inference(self, model_id: str, prompt: str, images: Optional[list[ImageInput]] = None) -> LLMResponse:
        chat_completion = self.client.chat.completions.create(
            **self._build_chat_params(model_id=model_id, prompt=prompt, images=images)
        )
        usage = self._extract_usage(getattr(chat_completion, "usage", None))
        self._set_last_usage(usage)
        content = chat_completion.choices[0].message.content or ""
        return LLMResponse(text=content, usage=usage)

    async def inference_stream(
        self, model_id: str, prompt: str, images: Optional[list[ImageInput]] = None
    ) -> AsyncGenerator[str, None]:
        stream = await self.async_client.chat.completions.create(
            **self._build_chat_params(model_id=model_id, prompt=prompt, images=images, stream=True)
        )

        prompt_tokens = 0
        completion_tokens = 0
        async for chunk in stream:
            if not chunk.choices:
                usage = self._extract_usage(getattr(chunk, "usage", None))
                if usage.prompt_tokens or usage.completion_tokens:
                    prompt_tokens = usage.prompt_tokens or prompt_tokens
                    completion_tokens = usage.completion_tokens or completion_tokens
                continue

            content = chunk.choices[0].delta.content
            if content is not None:
                yield content

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
            prompt = raw.get("prompt_tokens") or 0
            completion = raw.get("completion_tokens") or 0
            return LLMUsage(prompt_tokens=int(prompt or 0), completion_tokens=int(completion or 0))
        prompt = getattr(raw, "prompt_tokens", 0) or getattr(raw, "prompt_tokens_cost", 0) or 0
        completion = getattr(raw, "completion_tokens", 0) or getattr(raw, "completion_tokens_cost", 0) or 0
        return LLMUsage(prompt_tokens=int(prompt or 0), completion_tokens=int(completion or 0))
