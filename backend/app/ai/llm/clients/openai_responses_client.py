import asyncio
import json

from app.ai.llm.toolcall_args import parse_tool_call_arguments
import os
from typing import AsyncGenerator, AsyncIterator, Any, Optional

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
    ReasoningCompleteEvent,
    ReasoningDeltaEvent,
    ReasoningStartEvent,
    TextDeltaEvent,
    ToolSpec,
    ToolUseCompleteEvent,
    ToolUseInputDeltaEvent,
    ToolUseStartEvent,
    UsageEvent,
    WebSearchCompleteEvent,
    WebSearchResultEvent,
    WebSearchStartEvent,
)


class OpenAIResponsesClient(LLMClient):
    """
    OpenAI Responses API client.

    Used for the main 'openai' provider. Supports native reasoning content
    streaming (reasoning_effort) and full conversation history via input[].
    Custom/compatible endpoints continue to use OpenAiClient (Chat Completions).

    `base_url` lets this same client target an OpenAI-compatible Responses
    endpoint — notably Azure OpenAI's v1 surface
    (``https://<resource>.openai.azure.com/openai/v1/``), which serves the
    Responses API (and the native ``web_search`` tool) using the plain OpenAI
    client rather than the AzureOpenAI/Chat-Completions client.

    `enable_web_search` turns on the provider-executed ``{"type": "web_search"}``
    server tool. Results stream back inline (web_search_call items + url_citation
    annotations) and never pass through our ToolRunner.
    """

    def __init__(
        self,
        api_key: str,
        base_url: Optional[str] = None,
        enable_web_search: bool = False,
        temperature: Optional[float] = None,
    ):
        super().__init__()
        client_kwargs: dict[str, Any] = {"api_key": api_key}
        if base_url:
            client_kwargs["base_url"] = base_url
        self.client = OpenAI(**client_kwargs)
        self.async_client = AsyncOpenAI(**client_kwargs)
        self.enable_web_search = enable_web_search
        # Admin-configured override; None keeps each path's historical default
        # (the legacy Chat Completions helpers send 0.3/1.0, the Responses path
        # sends nothing).
        self.temperature = temperature

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

        Image generation is a separate endpoint from the Responses API, so this
        mirrors the OpenAi client's implementation exactly.
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

    @staticmethod
    def _build_chat_content(prompt: str, images: Optional[list[ImageInput]] = None):
        if not images:
            return prompt.strip()
        content: list[dict] = [{"type": "text", "text": prompt.strip()}]
        for img in images:
            url = img.data if img.source_type == "url" else f"data:{img.media_type};base64,{img.data}"
            content.append({"type": "image_url", "image_url": {"url": url}})
        return content

    def inference(self, model_id: str, prompt: str, images: Optional[list[ImageInput]] = None) -> LLMResponse:
        temperature = self.temperature if self.temperature is not None else (1.0 if "gpt-5" in model_id else 0.3)
        chat_completion = self.client.chat.completions.create(
            model=model_id,
            messages=[{"role": "user", "content": self._build_chat_content(prompt, images)}],
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
        temperature = self.temperature if self.temperature is not None else (1.0 if "gpt-5" in model_id else 0.3)
        stream = await self.async_client.chat.completions.create(
            model=model_id,
            messages=[{"role": "user", "content": self._build_chat_content(prompt, images)}],
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

            image_blocks = [b for b in blocks if b.get("type") == "image"]

            # function_call_output items come first so each result follows the
            # call that produced it.
            for tr in tool_results:
                content = tr.get("content", "")
                if not isinstance(content, str):
                    content = json.dumps(content, default=str)
                out.append({
                    "type": "function_call_output",
                    "call_id": tr["tool_use_id"],
                    "output": content,
                })

            if tool_uses:
                if text_blocks:
                    text = "\n".join(b.get("text", "") for b in text_blocks if b.get("text"))
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
                continue

            # Text and images become their own message item — including when
            # tool_results were emitted above (that text is the per-turn head;
            # dropping it silently loses steering), and including images, which
            # the block path ignored entirely so a screenshot-bearing tool
            # result never reached a vision model.
            content_parts: list[dict] = []
            for b in text_blocks:
                if b.get("text"):
                    content_parts.append({"type": "input_text", "text": b["text"]})
            for b in image_blocks:
                src = b.get("source") or {}
                if src.get("type") == "url":
                    url = src.get("url", "")
                else:
                    url = f"data:{src.get('media_type', 'image/png')};base64,{src.get('data', '')}"
                content_parts.append({"type": "input_image", "image_url": url, "detail": "auto"})

            if not content_parts:
                continue
            item_role = "user" if tool_results else role
            if len(content_parts) == 1 and content_parts[0]["type"] == "input_text":
                out.append({"type": "message", "role": item_role, "content": content_parts[0]["text"]})
            else:
                out.append({"type": "message", "role": item_role, "content": content_parts})
        return out

    @staticmethod
    def _attach_images(items: list[dict], images: list[ImageInput]) -> None:
        """Fold standalone image inputs into the input items, in place.

        Images can only ride on user message items — not on function_call_output
        items. Merge into a trailing user message when there is one; otherwise
        (mid tool loop, where the tail is tool results) append a new user
        message so the images reach the model instead of being dropped.
        """
        parts: list[dict] = []
        for img in images:
            url = img.data if img.source_type == "url" else f"data:{img.media_type or 'image/png'};base64,{img.data}"
            parts.append({"type": "input_image", "image_url": url, "detail": "auto"})
        if not parts:
            return
        last = items[-1] if items else None
        if last is not None and last.get("type") == "message" and last.get("role") == "user":
            content = last.get("content")
            if isinstance(content, str):
                content = [{"type": "input_text", "text": content}] if content else []
            last["content"] = content + parts
        else:
            items.append({"type": "message", "role": "user", "content": parts})

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
        web_search: Optional[bool] = None,
        web_search_domains: Optional[list] = None,
    ) -> AsyncIterator[LLMStreamEvent]:
        input_items = self._translate_messages(messages)
        if images:
            self._attach_images(input_items, images)
        # Effective web-search gate: caller must request it (per-call, e.g. the
        # org+provider gate the planner computes) AND the client must have been
        # constructed web-search-capable. Default (None) = off, so non-planner
        # call sites never trigger it.
        use_web_search = bool(web_search) and self.enable_web_search

        request_kwargs: dict[str, Any] = {
            "model": model_id,
            "input": input_items,
            "stream": True,
        }
        # The Responses path historically sends no temperature (reasoning models
        # reject it); only an explicit admin-configured value is forwarded, and
        # not to reasoning models, which 400 on any sampling parameter.
        if self.temperature is not None and not model_id.startswith(("o1", "o3", "o4", "gpt-5")):
            request_kwargs["temperature"] = self.temperature
        if system:
            request_kwargs["instructions"] = system
        request_tools: list[dict] = self._translate_tools(tools) if tools else []
        if use_web_search:
            # Provider-executed server tool. Runs inside the Responses API and
            # streams web_search_call items + url_citation annotations inline.
            # search_context_size="high" makes it search more thoroughly and
            # open more results — measurably better recall on harder pages.
            web_tool: dict[str, Any] = {"type": "web_search", "search_context_size": "high"}
            if web_search_domains:
                # Hard-scope to the user's domains. This makes the tool open/read
                # those pages (open_page action) instead of snippet search.
                web_tool["filters"] = {"allowed_domains": list(web_search_domains)[:20]}
            request_tools.append(web_tool)
        if request_tools:
            request_kwargs["tools"] = request_tools
            # Only constrain parallelism for our function tools; the server-side
            # web_search tool is unaffected.
            # BOW_FORCE_PARALLEL_TOOLS relaxes single-tool mode (mirrors
            # anthropic_client) so concurrent multi-tool dispatch can run.
            if os.environ.get("BOW_FORCE_PARALLEL_TOOLS", "").lower() in ("1", "true", "yes"):
                disable_parallel_tools = False
            if tools and disable_parallel_tools:
                request_kwargs["parallel_tool_calls"] = False
        is_reasoning_model = (
            model_id.startswith(("o1", "o3", "o4", "gpt-5"))
            or model_id in {"o1", "o3"}
        )
        if thinking and is_reasoning_model:
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
                    # item.id  → key used by subsequent delta/done events (item_id)
                    # item.call_id → the model-assigned call ID we expose externally
                    item_id = getattr(item, "id", "") or ""
                    call_id = getattr(item, "call_id", "") or ""
                    name = getattr(item, "name", "") or ""
                    open_calls[item_id] = {"call_id": call_id, "name": name, "args_buffer": ""}
                    yield ToolUseStartEvent(id=call_id, name=name)
                elif itype == "reasoning":
                    reasoning_active = True
                    yield ReasoningStartEvent()
                elif itype == "web_search_call":
                    # Provider-executed search begins. The query is not populated
                    # yet here — it arrives on the completed item (see done).
                    item_id = getattr(item, "id", "") or ""
                    action = getattr(item, "action", None)
                    query = getattr(action, "query", None) if action else None
                    yield WebSearchStartEvent(id=item_id, query=query)

            elif etype == "response.output_text.annotation.added":
                annotation = getattr(event, "annotation", None)
                # Annotation is a dict-like (url_citation) on Azure/OpenAI.
                if isinstance(annotation, dict):
                    a_type = annotation.get("type")
                    url = annotation.get("url")
                    title = annotation.get("title")
                else:
                    a_type = getattr(annotation, "type", None)
                    url = getattr(annotation, "url", None)
                    title = getattr(annotation, "title", None)
                if a_type == "url_citation" and url:
                    yield WebSearchResultEvent(url=url, title=title)

            elif etype == "response.output_item.done":
                item = getattr(event, "item", None)
                if item is None:
                    continue
                itype = getattr(item, "type", None)
                if itype == "reasoning" and reasoning_active:
                    reasoning_active = False
                    yield ReasoningCompleteEvent(text="")
                elif itype == "web_search_call":
                    # Completed call — populated now. The action is one of
                    # search (query/queries), open_page (url), or find (url).
                    item_id = getattr(item, "id", "") or ""
                    action = getattr(item, "action", None)
                    a_url = getattr(action, "url", None) if action else None
                    query = (getattr(action, "query", None) if action else None) or a_url
                    queries = (getattr(action, "queries", None) if action else None)
                    if not queries and a_url:
                        queries = [a_url]
                    status = getattr(item, "status", None)
                    yield WebSearchCompleteEvent(id=item_id, query=query, queries=queries, status=status)

            elif etype == "response.output_text.delta":
                text = getattr(event, "delta", "") or ""
                if text:
                    yield TextDeltaEvent(text=text)

            elif etype in ("response.reasoning_summary_text.delta", "response.reasoning_text.delta"):
                text = getattr(event, "delta", "") or ""
                if text:
                    yield ReasoningDeltaEvent(text=text)

            elif etype == "response.reasoning_summary_text.done":
                # Reasoning summaries arrive as multiple "summary parts," each a
                # self-contained markdown section starting with a bold header.
                # Insert a paragraph break so consecutive parts don't render as
                # one inline blob (`…task.**Next section**`).
                yield ReasoningDeltaEvent(text="\n\n")

            elif etype == "response.function_call_arguments.delta":
                item_id = getattr(event, "item_id", "") or ""
                delta = getattr(event, "delta", "") or ""
                if delta and item_id in open_calls:
                    open_calls[item_id]["args_buffer"] += delta
                    yield ToolUseInputDeltaEvent(id=open_calls[item_id]["call_id"], partial_json=delta)

            elif etype == "response.function_call_arguments.done":
                item_id = getattr(event, "item_id", "") or ""
                if item_id in open_calls:
                    pending = open_calls.pop(item_id)
                    raw = getattr(event, "arguments", "") or pending["args_buffer"]
                    parsed = parse_tool_call_arguments(raw, pending["name"])
                    stop_reason = "tool_use"
                    yield ToolUseCompleteEvent(id=pending["call_id"], name=pending["name"], input=parsed)

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
