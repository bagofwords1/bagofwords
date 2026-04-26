import json
from openai import AzureOpenAI, AsyncAzureOpenAI
from typing import AsyncGenerator, AsyncIterator, Any, Optional

from app.ai.llm.clients.base import LLMClient
from app.ai.llm.types import (
    ImageInput,
    LLMResponse,
    LLMStreamEvent,
    LLMUsage,
    Message,
    MessageStopEvent,
    TextDeltaEvent,
    ToolSpec,
    ToolUseCompleteEvent,
    ToolUseInputDeltaEvent,
    ToolUseStartEvent,
    UsageEvent,
)


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

    @staticmethod
    def _translate_messages(messages: list[Message]) -> list[dict]:
        out: list[dict] = []
        for msg in messages:
            if isinstance(msg.content, str):
                out.append({"role": msg.role, "content": msg.content})
                continue
            blocks = msg.content
            tool_calls = [b for b in blocks if b.get("type") == "tool_use"]
            tool_results = [b for b in blocks if b.get("type") == "tool_result"]
            text_blocks = [b for b in blocks if b.get("type") == "text"]

            if tool_results:
                for tr in tool_results:
                    content = tr.get("content", "")
                    if not isinstance(content, str):
                        content = json.dumps(content, default=str)
                    out.append({
                        "role": "tool",
                        "tool_call_id": tr["tool_use_id"],
                        "content": content,
                    })
            elif tool_calls:
                text_content = text_blocks[0].get("text", "") if text_blocks else None
                oai_tool_calls = []
                for tc in tool_calls:
                    args = tc.get("input", {})
                    oai_tool_calls.append({
                        "id": tc["id"],
                        "type": "function",
                        "function": {
                            "name": tc["name"],
                            "arguments": json.dumps(args) if not isinstance(args, str) else args,
                        },
                    })
                entry: dict = {"role": "assistant", "tool_calls": oai_tool_calls}
                if text_content:
                    entry["content"] = text_content
                out.append(entry)
            else:
                text = " ".join(b.get("text", "") for b in text_blocks)
                out.append({"role": msg.role, "content": text})
        return out

    @staticmethod
    def _translate_tools(tools: list[ToolSpec]) -> list[dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.input_schema,
                },
            }
            for t in tools
        ]

    async def inference_stream_v2(
        self,
        model_id: str,
        messages: list[Message],
        system: Optional[str] = None,
        tools: Optional[list[ToolSpec]] = None,
        images: Optional[list[ImageInput]] = None,
    ) -> AsyncIterator[LLMStreamEvent]:
        oai_messages: list[dict] = []
        if system:
            oai_messages.append({"role": "system", "content": system})
        oai_messages.extend(self._translate_messages(messages))

        temperature = 1.0 if "gpt-5" in model_id else 0.3
        request_kwargs: dict[str, Any] = {
            "model": model_id,
            "messages": oai_messages,
            "temperature": temperature,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if tools:
            request_kwargs["tools"] = self._translate_tools(tools)
            request_kwargs["tool_choice"] = "auto"

        open_calls: dict[int, dict] = {}
        prompt_tokens = 0
        completion_tokens = 0
        cache_read_tokens = 0
        stop_reason: str | None = None

        stream = await self.async_client.chat.completions.create(**request_kwargs)
        async for chunk in stream:
            usage = self._extract_usage(getattr(chunk, "usage", None))
            if usage.prompt_tokens:
                prompt_tokens = usage.prompt_tokens
            if usage.completion_tokens:
                completion_tokens = usage.completion_tokens
            if usage.cache_read_tokens:
                cache_read_tokens = usage.cache_read_tokens

            if not chunk.choices:
                continue

            choice = chunk.choices[0]
            delta = choice.delta

            if choice.finish_reason:
                stop_reason = choice.finish_reason

            if delta.content:
                yield TextDeltaEvent(text=delta.content)

            if delta.tool_calls:
                for tc_delta in delta.tool_calls:
                    idx = tc_delta.index
                    if idx not in open_calls:
                        open_calls[idx] = {
                            "id": tc_delta.id or "",
                            "name": getattr(tc_delta.function, "name", "") or "",
                            "args_buffer": "",
                        }
                        yield ToolUseStartEvent(
                            id=open_calls[idx]["id"],
                            name=open_calls[idx]["name"],
                        )
                    else:
                        if tc_delta.id:
                            open_calls[idx]["id"] = tc_delta.id
                        if getattr(tc_delta.function, "name", None):
                            open_calls[idx]["name"] = tc_delta.function.name

                    fragment = getattr(tc_delta.function, "arguments", "") or ""
                    if fragment:
                        open_calls[idx]["args_buffer"] += fragment
                        yield ToolUseInputDeltaEvent(
                            id=open_calls[idx]["id"],
                            partial_json=fragment,
                        )

        for pending in open_calls.values():
            raw = pending["args_buffer"]
            try:
                parsed = json.loads(raw) if raw.strip() else {}
            except Exception:
                parsed = {"_unparsable": True, "_raw": raw}
            yield ToolUseCompleteEvent(
                id=pending["id"],
                name=pending["name"],
                input=parsed,
            )

        _stop_map = {"stop": "end_turn", "tool_calls": "tool_use", "length": "max_tokens"}
        yield MessageStopEvent(stop_reason=_stop_map.get(stop_reason or "", "other"))

        yield UsageEvent(
            input_tokens=prompt_tokens,
            output_tokens=completion_tokens,
            cache_read_tokens=cache_read_tokens,
        )
        self._set_last_usage(LLMUsage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cache_read_tokens=cache_read_tokens,
        ))

    def test_connection(self):
        return True

    @staticmethod
    def _extract_usage(raw: Any) -> LLMUsage:
        if raw is None:
            return LLMUsage()
        # Azure OpenAI runs on the same backend as OpenAI; prompt caching is
        # automatic on prefixes >= 1024 tokens, exposed via
        # prompt_tokens_details.cached_tokens. 50% input discount on hit.
        if isinstance(raw, dict):
            prompt = raw.get("prompt_tokens") or 0
            completion = raw.get("completion_tokens") or 0
            details = raw.get("prompt_tokens_details") or {}
            cache_read = (details.get("cached_tokens") if isinstance(details, dict) else 0) or 0
            return LLMUsage(
                prompt_tokens=int(prompt or 0),
                completion_tokens=int(completion or 0),
                cache_read_tokens=int(cache_read or 0),
            )
        prompt = getattr(raw, "prompt_tokens", 0) or getattr(raw, "prompt_tokens_cost", 0) or 0
        completion = getattr(raw, "completion_tokens", 0) or getattr(raw, "completion_tokens_cost", 0) or 0
        details = getattr(raw, "prompt_tokens_details", None)
        cache_read = getattr(details, "cached_tokens", 0) if details is not None else 0
        return LLMUsage(
            prompt_tokens=int(prompt or 0),
            completion_tokens=int(completion or 0),
            cache_read_tokens=int(cache_read or 0),
        )