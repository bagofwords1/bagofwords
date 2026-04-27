import base64
import json
from typing import AsyncGenerator, AsyncIterator, Optional

from google import genai
from google.genai import types

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


class Google(LLMClient):
    def __init__(self, api_key: str | None = None):
        super().__init__()
        self.client = genai.Client(api_key=api_key)
        self.temperature = 0.3

    @staticmethod
    def _build_contents(prompt: str, images: Optional[list[ImageInput]] = None) -> str | list:
        """Build contents, either as string or list with Parts for multimodal."""
        if not images:
            return prompt.strip()

        contents = []
        for img in images:
            if img.source_type == "url":
                # For URLs, use Part.from_uri (works with gs:// or https://)
                contents.append(types.Part.from_uri(file_uri=img.data, mime_type=img.media_type))
            else:
                # For base64, decode and use Part.from_bytes
                image_bytes = base64.b64decode(img.data)
                contents.append(types.Part.from_bytes(data=image_bytes, mime_type=img.media_type))
        contents.append(types.Part.from_text(text=prompt.strip()))
        return contents

    def inference(self, model_id: str, prompt: str, images: Optional[list[ImageInput]] = None) -> LLMResponse:
        thinking_budget = 128 if "pro" in model_id else 0

        response = self.client.models.generate_content(
            model=model_id,
            contents=self._build_contents(prompt, images),
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

    async def inference_stream(
        self, model_id: str, prompt: str, images: Optional[list[ImageInput]] = None
    ) -> AsyncGenerator[str, None]:
        thinking_budget = 128 if "pro" in model_id else 0

        prompt_tokens = 0
        completion_tokens = 0
        for chunk in self.client.models.generate_content_stream(
            model=model_id,
            contents=self._build_contents(prompt, images),
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

    @staticmethod
    def _translate_messages(messages: list[Message]) -> list[types.Content]:
        # First pass: build tool_use_id → name map for function_response translation
        id_to_name: dict[str, str] = {}
        for msg in messages:
            if isinstance(msg.content, list):
                for b in msg.content:
                    if b.get("type") == "tool_use":
                        id_to_name[b["id"]] = b["name"]

        out: list[types.Content] = []
        for msg in messages:
            role = "model" if msg.role == "assistant" else "user"
            if isinstance(msg.content, str):
                out.append(types.Content(role=role, parts=[types.Part.from_text(text=msg.content)]))
                continue

            blocks = msg.content
            text_blocks = [b for b in blocks if b.get("type") == "text"]
            tool_uses = [b for b in blocks if b.get("type") == "tool_use"]
            tool_results = [b for b in blocks if b.get("type") == "tool_result"]

            if tool_results:
                parts = []
                for tr in tool_results:
                    tool_id = tr["tool_use_id"]
                    name = id_to_name.get(tool_id, tool_id)
                    content = tr.get("content", "")
                    if not isinstance(content, str):
                        content = json.dumps(content, default=str)
                    parts.append(types.Part.from_function_response(
                        name=name,
                        response={"output": content},
                    ))
                out.append(types.Content(role="user", parts=parts))
            elif tool_uses:
                parts = []
                for tc in tool_uses:
                    parts.append(types.Part.from_function_call(
                        name=tc["name"],
                        args=tc.get("input", {}),
                    ))
                out.append(types.Content(role="model", parts=parts))
            else:
                text = " ".join(b.get("text", "") for b in text_blocks)
                out.append(types.Content(role=role, parts=[types.Part.from_text(text=text)]))
        return out

    @staticmethod
    def _translate_tools(tools: list[ToolSpec]) -> list[types.Tool]:
        declarations = [
            types.FunctionDeclaration(
                name=t.name,
                description=t.description,
                parameters=t.input_schema,
            )
            for t in tools
        ]
        return [types.Tool(function_declarations=declarations)]

    async def inference_stream_v2(
        self,
        model_id: str,
        messages: list[Message],
        system: Optional[str] = None,
        tools: Optional[list[ToolSpec]] = None,
        images: Optional[list[ImageInput]] = None,
        thinking: Optional[dict] = None,  # accepted for parity; not yet wired (uses fixed thinking_budget below)
        disable_parallel_tools: bool = True,  # accepted for parity; Gemini doesn't parallel by default
    ) -> AsyncIterator[LLMStreamEvent]:
        thinking_budget = 128 if "pro" in model_id else 0
        config_kwargs: dict = {
            "thinking_config": types.ThinkingConfig(thinking_budget=thinking_budget),
            "temperature": self.temperature,
        }
        if system:
            config_kwargs["system_instruction"] = system
        if tools:
            config_kwargs["tools"] = self._translate_tools(tools)

        contents = self._translate_messages(messages)
        prompt_tokens = 0
        completion_tokens = 0
        stop_reason = "end_turn"

        # google-genai sync generator — run in executor to avoid blocking
        import asyncio
        loop = asyncio.get_running_loop()

        def _collect():
            chunks = []
            for chunk in self.client.models.generate_content_stream(
                model=model_id,
                contents=contents,
                config=types.GenerateContentConfig(**config_kwargs),
            ):
                chunks.append(chunk)
            return chunks

        chunks = await loop.run_in_executor(None, _collect)

        tool_call_counter = 0
        for chunk in chunks:
            usage_meta = getattr(chunk, "usage_metadata", None)
            if usage_meta:
                prompt_tokens = getattr(usage_meta, "prompt_token_count", prompt_tokens) or prompt_tokens
                completion_tokens = getattr(usage_meta, "candidates_token_count", completion_tokens) or completion_tokens

            candidate = chunk.candidates[0] if chunk.candidates else None
            if not candidate:
                continue

            finish = getattr(candidate, "finish_reason", None)
            if finish:
                finish_name = getattr(finish, "name", str(finish))
                if finish_name in ("STOP",):
                    stop_reason = "end_turn"
                elif finish_name in ("MAX_TOKENS",):
                    stop_reason = "max_tokens"

            for part in (candidate.content.parts if candidate.content else []):
                if part.text:
                    yield TextDeltaEvent(text=part.text)
                fc = getattr(part, "function_call", None)
                if fc:
                    stop_reason = "tool_use"
                    call_id = f"call_{tool_call_counter}"
                    tool_call_counter += 1
                    yield ToolUseStartEvent(id=call_id, name=fc.name)
                    args_json = json.dumps(dict(fc.args))
                    yield ToolUseInputDeltaEvent(id=call_id, partial_json=args_json)
                    yield ToolUseCompleteEvent(id=call_id, name=fc.name, input=dict(fc.args))

        yield MessageStopEvent(stop_reason=stop_reason)
        yield UsageEvent(input_tokens=prompt_tokens, output_tokens=completion_tokens)
        self._set_last_usage(LLMUsage(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens))

