import asyncio
import json

from app.ai.llm.toolcall_args import parse_tool_call_arguments
import os
import uuid
from typing import AsyncGenerator, AsyncIterator, Any, Optional

import httpx
from openai import AsyncOpenAI, OpenAI

from app.ai.llm.clients.base import LLMClient
from app.ai.llm.types import (
    ImageInput,
    ImageOutput,
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


class OpenAi(LLMClient):
    # Class-level default so instances created without __init__ (test doubles
    # built via __new__) still resolve the attribute.
    temperature: Optional[float] = None

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.openai.com/v1",
        verify_ssl: bool = True,
        temperature: Optional[float] = None,
        default_headers: Optional[dict[str, str]] = None,
    ):
        super().__init__()
        # No temperature is sent unless one was explicitly configured. This
        # client serves every OpenAI-COMPATIBLE endpoint (custom providers,
        # LiteLLM, vLLM, Ollama), where model ids are gateway aliases — a
        # growing set of models behind them (Claude Sonnet 5+, gpt-5, o-series)
        # rejects any temperature but their default with a 400, and no
        # name-based detection can recognize an alias.
        self.temperature = temperature
        kwargs: dict[str, Any] = {"api_key": api_key, "base_url": base_url}
        if default_headers:
            kwargs["default_headers"] = default_headers
        if not verify_ssl:
            kwargs["http_client"] = httpx.Client(verify=verify_ssl)
        self.client = OpenAI(**kwargs)

        async_kwargs: dict[str, Any] = {"api_key": api_key, "base_url": base_url}
        if default_headers:
            async_kwargs["default_headers"] = default_headers
        if not verify_ssl:
            async_kwargs["http_client"] = httpx.AsyncClient(verify=verify_ssl)
        self.async_client = AsyncOpenAI(**async_kwargs)

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

    def _build_chat_params(
        self,
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
        params: dict[str, Any] = {
            "messages": [
                {
                    "role": "user",
                    "content": OpenAi._build_content(prompt, images),
                }
            ],
            "model": model_id,
        }
        if self.temperature is not None:
            params["temperature"] = self.temperature

        if stream:
            params["stream"] = True
            # Ask the API to emit a final usage chunk so we record provider-reported
            # token counts instead of falling back to the char/4 estimate (which
            # undercounts dense/structured content by ~25-30%). The usage chunk
            # arrives after all content has streamed, so it adds no latency.
            params["stream_options"] = {"include_usage": True}

        # Enable medium reasoning effort for reasoning-capable models.
        # Adjust this predicate as you add/change reasoning models.
        if model_id.startswith(("o1", "o3")) or model_id in {"o1", "o3"}:
            params["reasoning_effort"] = "medium"

        return params

    async def generate_image(
        self,
        model_id: str,
        prompt: str,
        *,
        size: Optional[str] = None,
        quality: Optional[str] = None,
        images: Optional[list[ImageInput]] = None,
    ) -> ImageOutput:
        """Generate an image via the OpenAI Images API (e.g. gpt-image-1).

        Uses the sync SDK off-thread (mirrors how the OpenAI-compatible client
        runs elsewhere). gpt-image-1 always returns base64 (no url option), so we
        read ``b64_json`` directly. Reference ``images`` are not wired into the
        edit endpoint yet — text-to-image only for now.
        """
        params: dict[str, Any] = {"model": model_id, "prompt": prompt, "n": 1}
        if size:
            params["size"] = size
        if quality:
            params["quality"] = quality

        def _call():
            return self.client.images.generate(**params)

        response = await asyncio.to_thread(_call)

        item = response.data[0]
        b64 = getattr(item, "b64_json", None)
        if not b64:
            raise RuntimeError("Image generation returned no base64 payload")

        usage = LLMUsage()
        raw_usage = getattr(response, "usage", None)
        if raw_usage is not None:
            usage = LLMUsage(
                prompt_tokens=int(getattr(raw_usage, "input_tokens", 0) or 0),
                completion_tokens=int(getattr(raw_usage, "output_tokens", 0) or 0),
            )
        self._set_last_usage(usage)

        return ImageOutput(
            data=b64,
            media_type="image/png",
            revised_prompt=getattr(item, "revised_prompt", None),
            usage=usage,
        )

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
        # OpenAI surfaces cache hits via prompt_tokens_details.cached_tokens.
        # Caching is automatic on prefixes >= 1024 tokens; cached tokens
        # are billed at 50% of normal input. There's no cache_creation
        # concept on OpenAI — the cache is fully managed.
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

    # ------------------------------------------------------------------
    # Native tool_use streaming (used by planner_v3)
    # ------------------------------------------------------------------

    @staticmethod
    def _translate_messages(messages: list[Message]) -> list[dict]:
        """Translate provider-agnostic Message list to OpenAI messages format.

        Every block type must survive. The three ways this silently lost content
        before: a turn carrying tool_result AND text emitted only the tool
        messages (the transcript sends results and the per-turn head together);
        only ``text_blocks[0]`` reached an assistant turn; and ``image`` blocks
        were unhandled in the block path, collapsing to an empty string.
        """
        out: list[dict] = []
        for msg in messages:
            if isinstance(msg.content, str):
                out.append({"role": msg.role, "content": msg.content})
                continue
            blocks = msg.content
            tool_calls = [b for b in blocks if b.get("type") == "tool_use"]
            tool_results = [b for b in blocks if b.get("type") == "tool_result"]
            text_blocks = [b for b in blocks if b.get("type") == "text"]
            image_blocks = [b for b in blocks if b.get("type") == "image"]

            # Tool results first: Chat Completions requires each `tool` message
            # to follow the assistant turn that requested it, before any new
            # user content.
            for tr in tool_results:
                content = tr.get("content", "")
                if not isinstance(content, str):
                    content = json.dumps(content, default=str)
                out.append({
                    "role": "tool",
                    "tool_call_id": tr["tool_use_id"],
                    "content": content,
                })

            if tool_calls:
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
                text_content = "\n".join(
                    b.get("text", "") for b in text_blocks if b.get("text")
                )
                if text_content:
                    entry["content"] = text_content
                out.append(entry)
                continue

            # Remaining text / images become their own message. Emitted even
            # when tool_results were present above — that text is the per-turn
            # head and dropping it loses steering.
            content_parts: list[dict] = []
            for b in text_blocks:
                if b.get("text"):
                    content_parts.append({"type": "text", "text": b["text"]})
            for b in image_blocks:
                src = b.get("source") or {}
                if src.get("type") == "url":
                    url = src.get("url", "")
                else:
                    url = f"data:{src.get('media_type', 'image/png')};base64,{src.get('data', '')}"
                content_parts.append({"type": "image_url", "image_url": {"url": url}})

            if not content_parts:
                continue
            role = "user" if tool_results else msg.role
            if len(content_parts) == 1 and content_parts[0]["type"] == "text":
                out.append({"role": role, "content": content_parts[0]["text"]})
            else:
                out.append({"role": role, "content": content_parts})
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

    @staticmethod
    def _attach_images(oai_messages: list[dict], images: list[ImageInput]) -> None:
        """Fold standalone image inputs into the conversation, in place.

        Chat Completions can only carry images on user messages — `tool`
        messages cannot. Merge into a trailing user message when there is one;
        otherwise (mid tool loop, where the tail is `tool` results) append a
        new user turn so the images reach the model instead of being dropped.
        """
        parts: list[dict] = []
        for img in images:
            if img.source_type == "url":
                url = img.data
            else:
                url = f"data:{img.media_type or 'image/png'};base64,{img.data}"
            parts.append({"type": "image_url", "image_url": {"url": url}})
        if not parts:
            return
        last = oai_messages[-1] if oai_messages else None
        if last is not None and last.get("role") == "user":
            content = last.get("content")
            if isinstance(content, str):
                content = [{"type": "text", "text": content}] if content else []
            last["content"] = content + parts
        else:
            oai_messages.append({"role": "user", "content": parts})

    async def inference_stream_v2(
        self,
        model_id: str,
        messages: list[Message],
        system: Optional[str] = None,
        tools: Optional[list[ToolSpec]] = None,
        images: Optional[list[ImageInput]] = None,
        thinking: Optional[dict] = None,  # accepted for parity; reasoning needs Responses-API migration
        disable_parallel_tools: bool = True,
    ) -> AsyncIterator[LLMStreamEvent]:
        oai_messages: list[dict] = []
        if system:
            oai_messages.append({"role": "system", "content": system})
        oai_messages.extend(self._translate_messages(messages))
        if images:
            self._attach_images(oai_messages, images)

        request_kwargs: dict[str, Any] = {
            "model": model_id,
            "messages": oai_messages,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if self.temperature is not None:
            request_kwargs["temperature"] = self.temperature
        if tools:
            request_kwargs["tools"] = self._translate_tools(tools)
            request_kwargs["tool_choice"] = "auto"
            # Restrict the response to one tool_call at a time. This is the
            # signature default only — planner_v3 passes
            # disable_parallel_tools=not parallel_tools_enabled, which is False
            # under the shipped ai_tool_concurrency=4, so the normal agent path
            # DOES get parallel calls. It applies to callers that don't go
            # through the planner. (The original rationale — "so the agent loop
            # never has to silently drop extras" — no longer holds either: the
            # loop dispatches batches and reports the tail beyond
            # BOW_AGENT_MAX_ACTIONS_PER_DECISION back as not_executed rather
            # than dropping it.) Setting also passes through
            # LiteLLM unchanged (LiteLLM honors parallel_tool_calls=False
            # for OpenAI/Anthropic/Azure backends).
            # BOW_FORCE_PARALLEL_TOOLS relaxes this (mirrors anthropic_client)
            # so the concurrent multi-tool dispatch can be exercised end-to-end.
            if os.environ.get("BOW_FORCE_PARALLEL_TOOLS", "").lower() in ("1", "true", "yes"):
                disable_parallel_tools = False
            if disable_parallel_tools:
                request_kwargs["parallel_tool_calls"] = False
        if model_id.startswith(("o1", "o3")) or model_id in {"o1", "o3"}:
            request_kwargs["reasoning_effort"] = "medium"

        # tool_calls accumulator keyed by index: {id, minted, name, args_buffer}
        open_calls: dict[int, dict] = {}
        # Fallback id namespace for endpoints that return no tool-call ids.
        # Per-request, so the same index in a later turn never reuses an id an
        # earlier turn already put in the transcript.
        _call_prefix = f"call_{uuid.uuid4().hex[:8]}"
        prompt_tokens = 0
        completion_tokens = 0
        cache_read_tokens = 0
        stop_reason: str | None = None

        stream = await self.async_client.chat.completions.create(**request_kwargs)
        async for chunk in stream:
            # Usage arrives on the final chunk (stream_options include_usage)
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

            # Capture stop reason
            if choice.finish_reason:
                stop_reason = choice.finish_reason

            # Text delta
            if delta.content:
                yield TextDeltaEvent(text=delta.content)

            # Tool call deltas
            if delta.tool_calls:
                for tc_delta in delta.tool_calls:
                    idx = tc_delta.index
                    if idx not in open_calls:
                        # First chunk for this tool call — emit start event.
                        # OpenAI itself always sends an id here, but this client
                        # also serves every OpenAI-COMPATIBLE endpoint (custom
                        # providers, LiteLLM, vLLM, Ollama) and some of those
                        # issue no tool-call ids at all. That used to leave the
                        # id as "", which is survivable for a single call and
                        # not for several: parallel tool calls are on by default
                        # (ai_tool_concurrency=4), so a batch would produce N
                        # transcript parts all keyed on "" and the tool_use ->
                        # tool_result pairing on replay becomes ambiguous.
                        # Mint one instead, scoped to this request so a bare
                        # counter can't collide across replayed turns — same
                        # approach google_client uses, Gemini issuing no ids
                        # either.
                        open_calls[idx] = {
                            "id": tc_delta.id or f"{_call_prefix}_{idx}",
                            "minted": not tc_delta.id,
                            "name": getattr(tc_delta.function, "name", "") or "",
                            "args_buffer": "",
                        }
                        yield ToolUseStartEvent(
                            id=open_calls[idx]["id"],
                            name=open_calls[idx]["name"],
                        )
                    else:
                        # Update id/name if they arrive late (some models stream
                        # them). A MINTED id is kept even if a real one shows up
                        # later: the start and delta events already went out
                        # under it, and planner_v3 keys action_id_index on that
                        # value — swapping mid-stream makes the complete event
                        # miss its own action.
                        if tc_delta.id and not open_calls[idx]["minted"]:
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

        # Emit complete events for all accumulated tool calls
        for pending in open_calls.values():
            raw = pending["args_buffer"]
            parsed = parse_tool_call_arguments(raw, pending["name"])
            yield ToolUseCompleteEvent(
                id=pending["id"],
                name=pending["name"],
                input=parsed,
            )

        # Map OpenAI finish_reason to our vocabulary
        _stop_map = {"stop": "end_turn", "tool_calls": "tool_use", "length": "max_tokens"}
        yield MessageStopEvent(
            stop_reason=_stop_map.get(stop_reason or "", "other"),
            raw_stop_reason=stop_reason,
        )

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
