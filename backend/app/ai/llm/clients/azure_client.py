from openai import AzureOpenAI, AsyncAzureOpenAI
from typing import AsyncGenerator, Any, Optional

from app.ai.llm.clients.base import LLMClient
from app.ai.llm.types import LLMResponse, LLMUsage, ImageInput


class AzureClient(LLMClient):
    def __init__(self, api_key: str, endpoint_url: str, api_version: str | None = None):
        super().__init__()
        # endpoint_url should be the Azure OpenAI resource endpoint, e.g. https://<resource>.openai.azure.com
        effective_api_version = api_version or "2024-10-21"
        self.client = AzureOpenAI(
            api_key=api_key,
            azure_endpoint=endpoint_url,
            api_version=effective_api_version,
        )
        self.async_client = AsyncAzureOpenAI(
            api_key=api_key,
            azure_endpoint=endpoint_url,
            api_version=effective_api_version,
        )

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

    def inference(self, model_id: str, prompt: str, images: Optional[list[ImageInput]] = None) -> LLMResponse:
        # For Azure, model_id is the deployment (deployment name)
        temperature = 0.3
        if "gpt-5" in model_id:
            temperature = 1.0

        chat_completion = self.client.chat.completions.create(
            messages=[
                {
                    "role": "user",
                    "content": self._build_content(prompt, images),
                }
            ],
            model=model_id,
            temperature=temperature,
        )
        usage = self._extract_usage(getattr(chat_completion, "usage", None))
        self._set_last_usage(usage)
        content = chat_completion.choices[0].message.content or ""
        return LLMResponse(text=content, usage=usage)

    async def inference_stream(
        self, model_id: str, prompt: str, images: Optional[list[ImageInput]] = None
    ) -> AsyncGenerator[str, None]:
        # For Azure, model_id is the deployment (deployment name)
        temperature = 0.3
        if "gpt-5" in model_id:
            temperature = 1.0

        stream = await self.async_client.chat.completions.create(
            messages=[
                {
                    "role": "user",
                    "content": self._build_content(prompt, images),
                }
            ],
            model=model_id,
            temperature=temperature,
            stream=True
        )

        prompt_tokens = 0
        completion_tokens = 0
        async for chunk in stream:
            if not chunk.choices:
                # heartbeat/control packets; may still carry usage
                usage = self._extract_usage(getattr(chunk, "usage", None))
                if usage.prompt_tokens or usage.completion_tokens:
                    prompt_tokens = usage.prompt_tokens or prompt_tokens
                    completion_tokens = usage.completion_tokens or completion_tokens
                continue
            
            delta = chunk.choices[0].delta
            if delta and delta.content:
                yield delta.content

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

    def test_connection(self):
        return True

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