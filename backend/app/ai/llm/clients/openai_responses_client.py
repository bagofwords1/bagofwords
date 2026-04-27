import json
from typing import AsyncGenerator, AsyncIterator, Any, Optional

from openai import AsyncOpenAI, OpenAI

from app.ai.llm.clients.base import LLMClient
from app.ai.llm.types import (
    ImageInput,
    LLMResponse,
    LLMStreamEvent,
    LLMUsage,
    Message,
    MessageStopEvent,
    ReasoningCompleteEvent,
    ReasoningDeltaEvent,
    ReasoningStartEvent,
    TextDeltaEvent,
    ToolSpec,
    ToolUseCompleteEvent,
    ToolUseInputDeltaEvent,
    ToolUseStartEvent,
    UsageEvent,
)


class OpenAIResponsesClient(LLMClient):
    """
    OpenAI Responses API client.

    Used for the main 'openai' provider. Supports native reasoning content
    streaming (reasoning_effort) and full conversation history via input[].
    Custom/compatible endpoints continue to use OpenAiClient (Chat Completions).
    """

    def __init__(self, api_key: str):
        super().__init__()
        self.client = OpenAI(api_key=api_key)
        self.async_client = AsyncOpenAI(api_key=api_key)

    def inference(self, model_id: str, prompt: str, images: Optional[list[ImageInput]] = None) -> LLMResponse:
        temperature = 1.0 if "gpt-5" in model_id else 0.3
        chat_completion = self.client.chat.completions.create(
            model=model_id,
            messages=[{"role": "user", "content": prompt.strip()}],
            temperature=temperature,
        )
        content = chat_completion.choices[0].message.content or ""
        usage_raw = getattr(chat_completion, "usage", None)
        prompt_tokens = getattr(usage_raw, "prompt_tokens", 0) or 0
        completion_tokens = getattr(usage_raw, "completion_tokens", 0) or 0
        usage = LLMUsage(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens)
        self._set_last_usage(usage)
        return LLMResponse(text=content, usage=usage)

    async def inference_stream(
        self, model_id: str, prompt: str, images: Optional[list[ImageInput]] = None
    ) -> AsyncGenerator[str, None]:
        temperature = 1.0 if "gpt-5" in model_id else 0.3
        stream = await self.async_client.chat.completions.create(
            model=model_id,
            messages=[{"role": "user", "content": prompt.strip()}],
            temperature=temperature,
            stream=True,
            stream_options={"include_usage": True},
        )
        prompt_tokens = 0
        completion_tokens = 0
        async for chunk in stream:
            if not chunk.choices:
                usage_raw = getattr(chunk, "usage", None)
                if usage_raw:
                    prompt_tokens = getattr(usage_raw, "prompt_tokens", 0) or prompt_tokens
                    completion_tokens = getattr(usage_raw, "completion_tokens", 0) or completion_tokens
                continue
            content = chunk.choices[0].delta.content
            if content:
                yield content
        self._set_last_usage(LLMUsage(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens))

    @staticmethod
    def _translate_messages(messages: list[Message]) -> list[dict]:
        """Translate provider-agnostic Messages to Responses API input items."""
        out: list[dict] = []
        for msg in messages:
            role = msg.role  # "user" or "assistant"

            if isinstance(msg.content, str):
                out.append({"type": "message", "role": role, "content": msg.content})
                continue

            blocks = msg.content
            tool_uses = [b for b in blocks if b.get("type") == "tool_use"]
            tool_results = [b for b in blocks if b.get("type") == "tool_result"]
            text_blocks = [b for b in blocks if b.get("type") == "text"]

            if tool_results:
                # Each tool result becomes a function_call_output item
                for tr in tool_results:
                    content = tr.get("content", "")
                    if not isinstance(content, str):
                        content = json.dumps(content, default=str)
                    out.append({
                        "type": "function_call_output",
                        "call_id": tr["tool_use_id"],
                        "output": content,
                    })
            elif tool_uses:
                # Text prefix first (if any), then each tool call as its own item
                if text_blocks:
                    text = " ".join(b.get("text", "") for b in text_blocks)
                    if text.strip():
                        out.append({"type": "message", "role": "assistant", "content": text})
                for tc in tool_uses:
                    args = tc.get("input", {})
                    out.append({
                        "type": "function_call",
                        "call_id": tc["id"],
                        "name": tc["name"],
                        "arguments": json.dumps(args) if not isinstance(args, str) else args,
                    })
            else:
                text = " ".join(b.get("text", "") for b in text_blocks)
                out.append({"type": "message", "role": role, "content": text})
        return out

    @staticmethod
    def _translate_tools(tools: list[ToolSpec]) -> list[dict]:
        return [
            {
                "type": "function",
                "name": t.name,
                "description": t.description,
                "parameters": t.input_schema,
            }
            for t in tools
        ]

    @staticmethod
    def _extract_usage(response_usage: Any) -> tuple[int, int, int]:
        if response_usage is None:
            return 0, 0, 0
        prompt = getattr(response_usage, "input_tokens", 0) or 0
        completion = getattr(response_usage, "output_tokens", 0) or 0
        details = getattr(response_usage, "input_tokens_details", None)
        cache_read = getattr(details, "cached_tokens", 0) if details else 0
        return int(prompt), int(completion), int(cache_read or 0)

    async def inference_stream_v2(
        self,
        model_id: str,
        messages: list[Message],
        system: Optional[str] = None,
        tools: Optional[list[ToolSpec]] = None,
        images: Optional[list[ImageInput]] = None,
        thinking: Optional[dict] = None,
        disable_parallel_tools: bool = True,
    ) -> AsyncIterator[LLMStreamEvent]:
        input_items = self._translate_messages(messages)

        request_kwargs: dict[str, Any] = {
            "model": model_id,
            "input": input_items,
            "stream": True,
        }
        if system:
            request_kwargs["instructions"] = system
        if tools:
            request_kwargs["tools"] = self._translate_tools(tools)
            if disable_parallel_tools:
                request_kwargs["parallel_tool_calls"] = False
        if thinking:
            effort = thinking.get("type")
            budget = thinking.get("budget_tokens")
            if effort == "adaptive" or not budget:
                reasoning_effort = "medium"
            elif budget >= 10000:
                reasoning_effort = "high"
            elif budget >= 3000:
                reasoning_effort = "medium"
            else:
                reasoning_effort = "low"
            request_kwargs["reasoning"] = {"effort": reasoning_effort, "summary": "auto"}

        # Track open tool calls: call_id → {name, args_buffer}
        open_calls: dict[str, dict] = {}
        reasoning_active = False
        prompt_tokens = 0
        completion_tokens = 0
        cache_read_tokens = 0
        stop_reason = "end_turn"

        stream = await self.async_client.responses.create(**request_kwargs)
        async for event in stream:
            etype = getattr(event, "type", None)

            if etype == "response.output_item.added":
                item = getattr(event, "item", None)
                if item is None:
                    continue
                itype = getattr(item, "type", None)
                if itype == "function_call":
                    call_id = getattr(item, "call_id", "") or ""
                    name = getattr(item, "name", "") or ""
                    open_calls[call_id] = {"name": name, "args_buffer": ""}
                    yield ToolUseStartEvent(id=call_id, name=name)
                elif itype == "reasoning":
                    reasoning_active = True
                    yield ReasoningStartEvent()

            elif etype == "response.output_item.done":
                item = getattr(event, "item", None)
                if item is None:
                    continue
                itype = getattr(item, "type", None)
                if itype == "reasoning" and reasoning_active:
                    reasoning_active = False
                    yield ReasoningCompleteEvent(text="")

            elif etype == "response.output_text.delta":
                text = getattr(event, "delta", "") or ""
                if text:
                    yield TextDeltaEvent(text=text)

            elif etype in ("response.reasoning_summary_text.delta", "response.reasoning_text.delta"):
                text = getattr(event, "delta", "") or ""
                if text:
                    yield ReasoningDeltaEvent(text=text)

            elif etype == "response.function_call_arguments.delta":
                call_id = getattr(event, "item_id", "") or ""
                delta = getattr(event, "delta", "") or ""
                if delta and call_id in open_calls:
                    open_calls[call_id]["args_buffer"] += delta
                    yield ToolUseInputDeltaEvent(id=call_id, partial_json=delta)

            elif etype == "response.function_call_arguments.done":
                call_id = getattr(event, "item_id", "") or ""
                if call_id in open_calls:
                    pending = open_calls.pop(call_id)
                    raw = getattr(event, "arguments", "") or pending["args_buffer"]
                    try:
                        parsed = json.loads(raw) if raw.strip() else {}
                    except Exception:
                        parsed = {"_unparsable": True, "_raw": raw}
                    stop_reason = "tool_use"
                    yield ToolUseCompleteEvent(id=call_id, name=pending["name"], input=parsed)

            elif etype == "response.completed":
                response = getattr(event, "response", None)
                usage = getattr(response, "usage", None) if response else None
                prompt_tokens, completion_tokens, cache_read_tokens = self._extract_usage(usage)
                status = getattr(response, "status", None) if response else None
                if status == "incomplete":
                    stop_reason = "max_tokens"

        yield MessageStopEvent(stop_reason=stop_reason)
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
