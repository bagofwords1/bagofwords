import base64
import json
import uuid
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


class Google(LLMClient):
    # Newer Gemini models (gemini-flash-latest / 3.6-flash, gemini-flash-lite-latest,
    # the pro line) cannot have thinking turned off: a thinking_budget of 0 is
    # rejected with a hard 400 INVALID_ARGUMENT, so every request fails. 128 is the
    # smallest budget accepted across the models we support, so use it as the floor
    # instead of 0 — the cost is negligible and the request actually goes through.
    MIN_THINKING_BUDGET = 128

    def __init__(self, api_key: str | None = None, temperature: float | None = None,
                 default_headers: dict | None = None):
        super().__init__()
        client_kwargs: dict = {"api_key": api_key}
        if default_headers:
            client_kwargs["http_options"] = types.HttpOptions(headers=default_headers)
        self.client = genai.Client(**client_kwargs)
        # Admin-configured override, or the historical default.
        self.temperature = 0.3 if temperature is None else temperature

    @classmethod
    def _thinking_budget(cls, requested: int | None = None) -> int:
        if requested is None:
            return cls.MIN_THINKING_BUDGET
        return max(int(requested), cls.MIN_THINKING_BUDGET)

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
        thinking_budget = self._thinking_budget()

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
        thinking_budget = self._thinking_budget()

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

    # thought_signature is opaque binary. ToolUseCompleteEvent.signature is a
    # str (it gets stored on a tool_use block and JSON-serialized alongside the
    # rest of the history), so carry it base64-encoded and reverse that on replay.
    @staticmethod
    def _encode_signature(sig: bytes | str | None) -> Optional[str]:
        if not sig:
            return None
        if isinstance(sig, bytes):
            return base64.b64encode(sig).decode("ascii")
        return sig

    @staticmethod
    def _decode_signature(sig: bytes | str | None) -> Optional[bytes]:
        if not sig:
            return None
        if isinstance(sig, bytes):
            return sig
        try:
            return base64.b64decode(sig, validate=True)
        except Exception:
            return sig.encode("utf-8")

    @staticmethod
    def _translate_messages(messages: list[Message]) -> list[types.Content]:
        # tool_use_id → name, resolved in a single forward pass. A global
        # pre-pass would be wrong: ids are minted per request (see
        # ``tool_call_counter``), so "call_0" recurs every turn and a
        # last-one-wins map labels each turn's function_response with a later
        # turn's tool name. Walking in order always resolves an id against the
        # most recent tool_use that precedes the result.
        id_to_name: dict[str, str] = {}

        out: list[types.Content] = []
        for msg in messages:
            if isinstance(msg.content, list):
                for b in msg.content:
                    if b.get("type") == "tool_use":
                        id_to_name[b["id"]] = b["name"]
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
                    part = types.Part.from_function_call(
                        name=tc["name"],
                        args=tc.get("input", {}),
                    )
                    # Gemini 3 rejects a replayed function call whose
                    # thought_signature is missing ("Function call is missing a
                    # thought_signature in functionCall parts"), so hand back
                    # whatever the model gave us for this call.
                    sig = Google._decode_signature(tc.get("signature"))
                    if sig:
                        part.thought_signature = sig
                    parts.append(part)
                out.append(types.Content(role="model", parts=parts))
            else:
                text = " ".join(b.get("text", "") for b in text_blocks)
                out.append(types.Content(role=role, parts=[types.Part.from_text(text=text)]))
        return out

    # Fields not accepted by Google's FunctionDeclaration schema validator
    _GOOGLE_SCHEMA_STRIP = frozenset({
        "$defs", "$schema", "choices", "examples", "default", "title",
        "additionalProperties",
    })

    # Keywords whose values map *names* (not keywords) to sub-schemas. Their keys
    # are author-chosen — a tool parameter may legitimately be called "title" or
    # "default" — so they must never be matched against _GOOGLE_SCHEMA_STRIP.
    _GOOGLE_SCHEMA_NAME_MAPS = frozenset({
        "properties", "$defs", "definitions", "patternProperties",
    })

    @staticmethod
    def _resolve_schema_refs(schema: dict) -> dict:
        """Inline $ref references and strip / convert fields Google's SDK doesn't accept."""
        defs = schema.get("$defs", {})

        def _resolve_child(key: str, value: any) -> any:
            # Under "properties"/"$defs"/..., the keys are names, not keywords:
            # recurse into the values but leave the names untouched.
            if key in Google._GOOGLE_SCHEMA_NAME_MAPS and isinstance(value, dict):
                return {name: _resolve(sub) for name, sub in value.items()}
            return _resolve(value)

        def _resolve(node: any) -> any:
            if isinstance(node, dict):
                if "$ref" in node:
                    ref = node["$ref"]
                    if ref.startswith("#/$defs/"):
                        def_name = ref[len("#/$defs/"):]
                        resolved = defs.get(def_name, {})
                        return _resolve(resolved)
                    return {"type": "string"}  # unresolvable ref → fallback
                # Convert JSON Schema "const" → "enum" (Google supports enum, not const)
                if "const" in node:
                    result = {
                        k: _resolve_child(k, v)
                        for k, v in node.items()
                        if k not in Google._GOOGLE_SCHEMA_STRIP and k != "const"
                    }
                    result["enum"] = [node["const"]]
                    return result
                result = {
                    k: _resolve_child(k, v)
                    for k, v in node.items()
                    if k not in Google._GOOGLE_SCHEMA_STRIP
                }
                # Drop required entries that reference undefined properties
                if "required" in result and "properties" in result:
                    defined = set(result["properties"].keys())
                    result["required"] = [r for r in result["required"] if r in defined]
                    if not result["required"]:
                        del result["required"]
                return result
            if isinstance(node, list):
                return [_resolve(i) for i in node]
            return node

        return _resolve(schema)

    @staticmethod
    def _translate_tools(tools: list[ToolSpec]) -> list[types.Tool]:
        declarations = [
            types.FunctionDeclaration(
                name=t.name,
                description=t.description,
                parameters=Google._resolve_schema_refs(t.input_schema),
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
        thinking: Optional[dict] = None,
        disable_parallel_tools: bool = True,
    ) -> AsyncIterator[LLMStreamEvent]:
        if thinking:
            budget = self._thinking_budget(thinking.get("budget_tokens") or 1024)
            thinking_config = types.ThinkingConfig(thinking_budget=budget, include_thoughts=True)
        else:
            thinking_config = types.ThinkingConfig(
                thinking_budget=self._thinking_budget(), include_thoughts=False
            )
        config_kwargs: dict = {
            "thinking_config": thinking_config,
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

        # Gemini doesn't hand out tool-call ids, so we mint them. Scope them to
        # this request: a bare counter restarts at 0 every turn, and the
        # recurring "call_0" made ids ambiguous once several turns were replayed
        # back as history.
        call_prefix = f"call_{uuid.uuid4().hex[:8]}"
        tool_call_counter = 0
        reasoning_started = False
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
                # Gemini reports STOP alongside function calls; once we've seen a
                # call the turn is a tool turn, so don't let STOP downgrade it.
                if finish_name in ("STOP",) and stop_reason != "tool_use":
                    stop_reason = "end_turn"
                elif finish_name in ("MAX_TOKENS",):
                    stop_reason = "max_tokens"

            for part in (candidate.content.parts if candidate.content else []):
                is_thought = getattr(part, "thought", False)
                if part.text and is_thought:
                    if not reasoning_started:
                        reasoning_started = True
                        yield ReasoningStartEvent()
                    yield ReasoningDeltaEvent(text=part.text)
                elif part.text and not is_thought:
                    if reasoning_started:
                        reasoning_started = False
                        yield ReasoningCompleteEvent(text="")
                    yield TextDeltaEvent(text=part.text)
                fc = getattr(part, "function_call", None)
                if fc:
                    stop_reason = "tool_use"
                    call_id = f"{call_prefix}_{tool_call_counter}"
                    tool_call_counter += 1
                    yield ToolUseStartEvent(id=call_id, name=fc.name)
                    args_json = json.dumps(dict(fc.args))
                    yield ToolUseInputDeltaEvent(id=call_id, partial_json=args_json)
                    # Gemini 3 requires the thought_signature back on any replay
                    # of this call; it rides along on the same part.
                    yield ToolUseCompleteEvent(
                        id=call_id,
                        name=fc.name,
                        input=dict(fc.args),
                        signature=self._encode_signature(getattr(part, "thought_signature", None)),
                    )

        if reasoning_started:
            yield ReasoningCompleteEvent(text="")

        yield MessageStopEvent(stop_reason=stop_reason)
        yield UsageEvent(input_tokens=prompt_tokens, output_tokens=completion_tokens)
        self._set_last_usage(LLMUsage(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens))

