import json
from typing import Any, AsyncGenerator, AsyncIterator, Optional

from anthropic import Anthropic as AnthropicAPI, AsyncAnthropic

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


_STOP_REASON_MAP = {
    "end_turn": "end_turn",
    "tool_use": "tool_use",
    "max_tokens": "max_tokens",
    "stop_sequence": "stop_sequence",
}


class Anthropic(LLMClient):
    def __init__(self, api_key: str, base_url: str = None):
        super().__init__()
        self.client = AnthropicAPI(api_key=api_key)
        self.async_client = AsyncAnthropic(api_key=api_key)
        self.max_tokens = 32768
        self.temperature = 0.3

    @staticmethod
    def _build_content(prompt: str, images: Optional[list[ImageInput]] = None) -> str | list[dict[str, Any]]:
        """Build message content, either as string or multimodal content array."""
        if not images:
            return prompt.strip()

        content: list[dict[str, Any]] = []
        # Anthropic recommends images before text for better performance
        for img in images:
            if img.source_type == "url":
                content.append({
                    "type": "image",
                    "source": {
                        "type": "url",
                        "url": img.data
                    }
                })
            else:
                content.append({
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": img.media_type,
                        "data": img.data
                    }
                })
        content.append({"type": "text", "text": prompt.strip()})
        return content

    def inference(self, model_id: str, prompt: str, images: Optional[list[ImageInput]] = None) -> LLMResponse:
        message = self.client.messages.create(
            model=model_id,
            messages=[
                {
                    "role": "user",
                    "content": self._build_content(prompt, images),
                }
            ],
            max_tokens=self.max_tokens,
            temperature=self.temperature,
        )
        usage = self._extract_usage(getattr(message, "usage", None))
        self._set_last_usage(usage)
        text = message.content[0].text if message.content and message.content[0].text else ""
        return LLMResponse(text=text, usage=usage)

    async def inference_stream(
        self, model_id: str, prompt: str, images: Optional[list[ImageInput]] = None
    ) -> AsyncGenerator[str, None]:
        stream = await self.async_client.messages.create(
            model=model_id,
            messages=[
                {
                    "role": "user",
                    "content": self._build_content(prompt, images),
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

    # ------------------------------------------------------------------
    # Native tool_use streaming (used by planner_v3)
    # ------------------------------------------------------------------

    @staticmethod
    def _translate_messages(messages: list[Message]) -> list[dict]:
        """Translate provider-agnostic Message list to Anthropic messages format.

        Plain string content becomes [{"type": "text", "text": ...}].
        List content blocks pass through after type-specific translation.
        """
        out: list[dict] = []
        for msg in messages:
            if isinstance(msg.content, str):
                out.append({"role": msg.role, "content": msg.content})
                continue

            blocks: list[dict] = []
            for blk in msg.content:
                btype = blk.get("type")
                if btype == "text":
                    blocks.append({"type": "text", "text": blk.get("text", "")})
                elif btype == "tool_use":
                    blocks.append({
                        "type": "tool_use",
                        "id": blk["id"],
                        "name": blk["name"],
                        "input": blk.get("input", {}),
                    })
                elif btype == "tool_result":
                    content = blk.get("content", "")
                    if isinstance(content, str):
                        rendered = content
                    else:
                        rendered = json.dumps(content, default=str)
                    entry: dict = {
                        "type": "tool_result",
                        "tool_use_id": blk["tool_use_id"],
                        "content": rendered,
                    }
                    if blk.get("is_error"):
                        entry["is_error"] = True
                    blocks.append(entry)
                elif btype == "image":
                    src = blk.get("source") or {}
                    blocks.append({"type": "image", "source": src})
                else:
                    # Unknown block type — pass through as text fallback
                    blocks.append({"type": "text", "text": json.dumps(blk, default=str)})
            out.append({"role": msg.role, "content": blocks})
        return out

    @staticmethod
    def _translate_tools(tools: list[ToolSpec]) -> list[dict]:
        return [
            {
                "name": t.name,
                "description": t.description,
                "input_schema": t.input_schema,
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
        # If images supplied, attach them to the last user message as image blocks.
        # (Most callers will embed images directly in messages; this is a back-compat path.)
        msgs = self._translate_messages(messages)
        if images:
            image_blocks = []
            for img in images:
                if img.source_type == "url":
                    image_blocks.append({"type": "image", "source": {"type": "url", "url": img.data}})
                else:
                    image_blocks.append({
                        "type": "image",
                        "source": {"type": "base64", "media_type": img.media_type, "data": img.data},
                    })
            if msgs and msgs[-1]["role"] == "user":
                last = msgs[-1]
                if isinstance(last["content"], str):
                    last["content"] = [{"type": "text", "text": last["content"]}]
                last["content"] = image_blocks + last["content"]
            else:
                msgs.append({"role": "user", "content": image_blocks})

        request_kwargs: dict[str, Any] = {
            "model": model_id,
            "messages": msgs,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "stream": True,
        }
        if system:
            request_kwargs["system"] = system
        if tools:
            request_kwargs["tools"] = self._translate_tools(tools)

        # Per-tool-call accumulators keyed by content_block index
        # value: {"id": str, "name": str, "input_buffer": str}
        open_tool_blocks: dict[int, dict] = {}

        prompt_tokens = 0
        completion_tokens = 0

        stream = await self.async_client.messages.create(**request_kwargs)

        async for chunk in stream:
            ctype = getattr(chunk, "type", None)

            if ctype == "message_start":
                usage = self._extract_usage(getattr(getattr(chunk, "message", None), "usage", None))
                if usage.prompt_tokens or usage.completion_tokens:
                    prompt_tokens = usage.prompt_tokens or prompt_tokens
                    completion_tokens = usage.completion_tokens or completion_tokens
                continue

            if ctype == "content_block_start":
                idx = getattr(chunk, "index", None)
                blk = getattr(chunk, "content_block", None)
                if blk is None:
                    continue
                btype = getattr(blk, "type", None)
                if btype == "tool_use":
                    open_tool_blocks[idx] = {
                        "id": getattr(blk, "id", "") or "",
                        "name": getattr(blk, "name", "") or "",
                        "input_buffer": "",
                    }
                    yield ToolUseStartEvent(
                        id=open_tool_blocks[idx]["id"],
                        name=open_tool_blocks[idx]["name"],
                    )
                # text blocks: nothing to emit at start; deltas will fire
                continue

            if ctype == "content_block_delta":
                idx = getattr(chunk, "index", None)
                delta = getattr(chunk, "delta", None)
                if delta is None:
                    continue
                dtype = getattr(delta, "type", None)
                if dtype == "text_delta":
                    text = getattr(delta, "text", "") or ""
                    if text:
                        yield TextDeltaEvent(text=text)
                elif dtype == "input_json_delta":
                    fragment = getattr(delta, "partial_json", "") or ""
                    if idx in open_tool_blocks and fragment:
                        open_tool_blocks[idx]["input_buffer"] += fragment
                        yield ToolUseInputDeltaEvent(
                            id=open_tool_blocks[idx]["id"],
                            partial_json=fragment,
                        )
                continue

            if ctype == "content_block_stop":
                idx = getattr(chunk, "index", None)
                if idx in open_tool_blocks:
                    pending = open_tool_blocks.pop(idx)
                    raw = pending["input_buffer"]
                    try:
                        parsed = json.loads(raw) if raw.strip() else {}
                    except Exception:
                        parsed = {"_unparsable": True, "_raw": raw}
                    yield ToolUseCompleteEvent(
                        id=pending["id"],
                        name=pending["name"],
                        input=parsed,
                    )
                continue

            if ctype == "message_delta":
                # Stop reason and final usage delta arrive here
                delta = getattr(chunk, "delta", None)
                stop_reason = getattr(delta, "stop_reason", None) if delta else None
                usage = self._extract_usage(getattr(chunk, "usage", None))
                if usage.prompt_tokens or usage.completion_tokens:
                    prompt_tokens = usage.prompt_tokens or prompt_tokens
                    completion_tokens = usage.completion_tokens or completion_tokens
                if stop_reason:
                    yield MessageStopEvent(
                        stop_reason=_STOP_REASON_MAP.get(stop_reason, "other")
                    )
                continue

            if ctype == "message_stop":
                # Some SDKs emit this terminator without a stop_reason; ignore.
                continue

        # Emit final usage event after the stream ends
        yield UsageEvent(input_tokens=prompt_tokens, output_tokens=completion_tokens)
        self._set_last_usage(
            LLMUsage(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens)
        )