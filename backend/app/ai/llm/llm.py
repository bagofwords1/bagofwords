import asyncio
import re
import time
from typing import AsyncGenerator, Optional, Callable

from .clients.openai_client import OpenAi
from .clients.google_client import Google
from .clients.anthropic_client import Anthropic
from .clients.azure_client import AzureClient
from .clients.bedrock_client import BedrockClient
from .types import (
    ImageInput,
    LLMResponse,
    LLMStreamEvent,
    LLMUsage,
    Message,
    ToolSpec,
    UsageEvent,
)
from app.ai.utils.token_counter import count_tokens, estimate_tokens_fast
from app.models.llm_model import LLMModel
from app.services.llm_usage_recorder import LLMUsageRecorderService
from app.settings.logging_config import get_logger
from app.core.otel import get_tracer
from opentelemetry.trace import StatusCode
from sqlalchemy.ext.asyncio import AsyncSession

logger = get_logger(__name__)
tracer = get_tracer(__name__)

# Shared reference to the app's main asyncio loop. Populated lazily the
# first time _schedule_usage_record runs from an async context, so that
# later calls from worker threads (e.g. asyncio.to_thread(llm.inference))
# can still schedule usage recording via run_coroutine_threadsafe.
_MAIN_LOOP: Optional[asyncio.AbstractEventLoop] = None


class LLM:
    def __init__(
        self,
        model: LLMModel,
        usage_session_maker: Optional[Callable[[], "AsyncSession"]] = None,
    ):
        self.model = model
        self.model_id = model.model_id
        self.provider = model.provider.provider_type
        try:
            self.api_key = self.model.provider.decrypt_credentials()[0]
        except Exception as exc:
            # For most providers, failing to decrypt credentials is a hard error.
            # The only exception is Bedrock when using non-API-key auth (e.g., IAM),
            # where an API key is not required.
            additional_config = getattr(self.model.provider, "additional_config", None) or {}
            auth_mode = additional_config.get("auth_mode", "iam") if isinstance(additional_config, dict) else "iam"
            if self.provider == "bedrock" and auth_mode != "api_key":
                logger.warning(
                    "Failed to decrypt credentials for Bedrock provider in '%s' auth mode; "
                    "continuing without api_key. Error: %s",
                    auth_mode,
                    exc,
                )
                self.api_key = None
            else:
                logger.error(
                    "Failed to decrypt credentials for provider '%s': %s",
                    self.provider,
                    exc,
                )
                raise
        self._usage_session_maker = usage_session_maker
        if self.provider == "openai":
            base_url = None
            if self.model.provider.additional_config:
                base_url = self.model.provider.additional_config.get("base_url")
            self.client = OpenAi(api_key=self.api_key, base_url=base_url or "https://api.openai.com/v1")
        elif self.provider == "anthropic":
            self.client = Anthropic(api_key=self.api_key)
        elif self.provider == "google":
            self.client = Google(api_key=self.api_key)
        elif self.provider == "azure":
            endpoint_url = self.model.provider.additional_config.get("endpoint_url") if self.model.provider.additional_config else None
            if not endpoint_url:
                raise ValueError("Azure provider requires endpoint_url in additional_config")
            self.client = AzureClient(api_key=self.api_key, endpoint_url=endpoint_url)
        elif self.provider == "custom":
            base_url = self.model.provider.additional_config.get("base_url") if self.model.provider.additional_config else None
            if not base_url:
                raise ValueError("Custom provider requires base_url in additional_config")
            verify_ssl = self.model.provider.additional_config.get("verify_ssl", True) if self.model.provider.additional_config else True
            # Use empty string for api_key if not provided (some local servers don't need auth)
            api_key = self.api_key or ""
            self.client = OpenAi(api_key=api_key, base_url=base_url, verify_ssl=verify_ssl)
        elif self.provider == "bedrock":
            additional_config = self.model.provider.additional_config or {}
            region = additional_config.get("region")
            if not region:
                raise ValueError("Bedrock provider requires region in additional_config")
            auth_mode = additional_config.get("auth_mode", "iam")
            if auth_mode == "api_key" and not self.api_key:
                raise ValueError("Bedrock provider with auth_mode 'api_key' requires provider credentials")

            bedrock_kwargs: dict = {"region": region, "auth_mode": auth_mode}
            if auth_mode == "api_key":
                bedrock_kwargs["api_key"] = self.api_key
            elif auth_mode == "access_keys":
                try:
                    access_key, secret_key = self.model.provider.decrypt_credentials()
                except Exception:
                    raise ValueError("Bedrock provider with auth_mode 'access_keys' requires stored AWS credentials")
                if not access_key or not secret_key:
                    raise ValueError("Bedrock provider with auth_mode 'access_keys' requires both access key and secret key")
                bedrock_kwargs["aws_access_key_id"] = access_key
                bedrock_kwargs["aws_secret_access_key"] = secret_key
            self.client = BedrockClient(**bedrock_kwargs)
        else:
            raise ValueError(f"Provider {self.provider} not supported")

    def _validate_vision_support(self, images: Optional[list[ImageInput]]) -> None:
        """Validate that the model supports vision if images are provided."""
        if not images:
            return
        supports_vision = getattr(self.model, "supports_vision", False)
        if not supports_vision:
            raise ValueError(
                f"Model '{self.model_id}' does not support images. "
                "Please select a vision-capable model or remove images from your request."
            )

    def inference(
        self,
        prompt: str,
        *,
        images: Optional[list[ImageInput]] = None,
        usage_scope: Optional[str] = None,
        usage_scope_ref_id: Optional[str] = None,
        should_record: bool = True,
    ) -> str:
        with tracer.start_as_current_span("llm.inference") as span:
            span.set_attribute("llm.model_id", self.model_id)
            span.set_attribute("llm.provider", self.provider)
            self._validate_vision_support(images)
            logger.debug("Model: %s, prompt: %s", self.model_id, prompt)
            prompt_tokens_estimate = self._count_tokens(prompt)
            span.set_attribute("llm.prompt_tokens_estimate", prompt_tokens_estimate)
            try:
                response = self.client.inference(model_id=self.model_id, prompt=prompt, images=images)
            except Exception as e:
                span.set_status(StatusCode.ERROR, str(e))
                span.record_exception(e)
                raise RuntimeError(f"LLM inference failed (provider={self.provider}, model={self.model_id}): {e}") from e
            logger.debug("Response: %s", response)

            text, usage = self._coerce_response(response)
            if not usage.prompt_tokens and not usage.completion_tokens and hasattr(self.client, "pop_last_usage"):
                usage = self.client.pop_last_usage()
            sanitized = self._sanitize_response_text(text)
            completion_tokens = usage.completion_tokens or self._count_tokens(sanitized)
            prompt_tokens = usage.prompt_tokens or prompt_tokens_estimate

            span.set_attribute("llm.prompt_tokens", prompt_tokens)
            span.set_attribute("llm.completion_tokens", completion_tokens)

            self._schedule_usage_record(
                scope=usage_scope,
                scope_ref_id=usage_scope_ref_id,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                should_record=should_record,
            )
            return sanitized

    async def inference_stream(
        self,
        prompt: str,
        *,
        images: Optional[list[ImageInput]] = None,
        usage_scope: Optional[str] = None,
        usage_scope_ref_id: Optional[str] = None,
        should_record: bool = True,
        prompt_tokens_estimate: Optional[int] = None,
    ) -> AsyncGenerator[str, None]:
        with tracer.start_as_current_span("llm.inference_stream") as span:
            span.set_attribute("llm.model_id", self.model_id)
            span.set_attribute("llm.provider", self.provider)
            self._validate_vision_support(images)
            logger.debug("Model: %s, prompt: %s", self.model_id, prompt)
            started_payload = False
            prefix = ""
            prompt_tokens = prompt_tokens_estimate if prompt_tokens_estimate is not None else self._estimate_tokens_fast(prompt)
            span.set_attribute("llm.prompt_tokens_estimate", prompt_tokens)
            completion_tokens = 0
            streamed_chunks: list[str] = []
            stream_start = time.monotonic()
            ttft_recorded = False
            try:
                async for chunk in self.client.inference_stream(model_id=self.model_id, prompt=prompt, images=images):
                    if chunk is None:
                        continue
                    if not isinstance(chunk, str):
                        try:
                            chunk = str(chunk)
                        except Exception:
                            continue

                    if "```" in chunk:
                        chunk = chunk.replace("```", "")

                    if not started_payload:
                        prefix += chunk
                        prefix = re.sub(r"^\s*```(?:[A-Za-z]+)?\s*", "", prefix)
                        prefix = re.sub(r"^\s*(?:json|JSON|python|PYTHON)\s*\r?\n", "", prefix)
                        if re.fullmatch(r"\s*(?:json|JSON|python|PYTHON)\s*", prefix or ""):
                            continue
                        prefix = re.sub(r"^\s+", "", prefix)

                        m = re.search(r"[\{\[]", prefix)
                        if not m:
                            if re.search(r"\S", prefix):
                                started_payload = True
                                emission = prefix
                                prefix = ""
                                if not ttft_recorded:
                                    ttft_ms = (time.monotonic() - stream_start) * 1000
                                    span.set_attribute("llm.ttft_ms", ttft_ms)
                                    span.add_event("ttft", {"ttft_ms": ttft_ms})
                                    ttft_recorded = True
                                completion_tokens += self._estimate_tokens_fast(emission)
                                streamed_chunks.append(emission)
                                yield emission
                            else:
                                continue
                        else:
                            started_payload = True
                            emission = prefix[m.start():]
                            prefix = ""
                            if not ttft_recorded:
                                ttft_ms = (time.monotonic() - stream_start) * 1000
                                span.set_attribute("llm.ttft_ms", ttft_ms)
                                span.add_event("ttft", {"ttft_ms": ttft_ms})
                                ttft_recorded = True
                            completion_tokens += self._estimate_tokens_fast(emission)
                            streamed_chunks.append(emission)
                            yield emission
                    else:
                        if "```" in chunk:
                            chunk = chunk.replace("```", "")
                        completion_tokens += self._estimate_tokens_fast(chunk)
                        streamed_chunks.append(chunk)
                        yield chunk
            except Exception as e:
                span.set_status(StatusCode.ERROR, str(e))
                span.record_exception(e)
                raise RuntimeError(f"LLM streaming failed (provider={self.provider}, model={self.model_id}): {e}") from e
            usage = LLMUsage()
            if hasattr(self.client, "pop_last_usage"):
                usage = self.client.pop_last_usage()
            if usage.prompt_tokens or usage.completion_tokens:
                prompt_tokens = usage.prompt_tokens or prompt_tokens
                completion_tokens = usage.completion_tokens or completion_tokens
            else:
                completion_tokens = self._estimate_tokens_fast("".join(streamed_chunks)) or completion_tokens

            span.set_attribute("llm.prompt_tokens", prompt_tokens)
            span.set_attribute("llm.completion_tokens", completion_tokens)
            span.set_attribute("llm.stream_chunks", len(streamed_chunks))

            self._schedule_usage_record(
                scope=usage_scope,
                scope_ref_id=usage_scope_ref_id,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                should_record=should_record,
            )

    async def inference_stream_v2(
        self,
        *,
        model_id: Optional[str] = None,
        messages: list[Message],
        system: Optional[str] = None,
        tools: Optional[list[ToolSpec]] = None,
        images: Optional[list[ImageInput]] = None,
        thinking: Optional[dict] = None,
        disable_parallel_tools: bool = True,
        usage_scope: Optional[str] = None,
        usage_scope_ref_id: Optional[str] = None,
        should_record: bool = True,
        prompt_tokens_estimate: Optional[int] = None,
    ):
        """Streaming inference with native tool_use support.

        Forwards :class:`LLMStreamEvent`s from the underlying client and records
        token usage after the stream ends (using either provider-reported usage
        or, as fallback, the prompt_tokens_estimate plus a UsageEvent count).
        """
        target_model_id = model_id or self.model_id
        with tracer.start_as_current_span("llm.inference_stream_v2") as span:
            span.set_attribute("llm.model_id", target_model_id)
            span.set_attribute("llm.provider", self.provider)
            self._validate_vision_support(images)

            prompt_tokens = (
                prompt_tokens_estimate
                if prompt_tokens_estimate is not None
                else 0
            )
            completion_tokens = 0
            cache_read_tokens = 0
            cache_creation_tokens = 0
            stream_start = time.monotonic()
            ttft_recorded = False

            try:
                async for evt in self.client.inference_stream_v2(
                    model_id=target_model_id,
                    messages=messages,
                    system=system,
                    tools=tools,
                    images=images,
                    thinking=thinking,
                    disable_parallel_tools=disable_parallel_tools,
                ):
                    if not ttft_recorded and getattr(evt, "type", None) in (
                        "text_delta",
                        "tool_use_start",
                    ):
                        ttft_ms = (time.monotonic() - stream_start) * 1000
                        span.set_attribute("llm.ttft_ms", ttft_ms)
                        span.add_event("ttft", {"ttft_ms": ttft_ms})
                        ttft_recorded = True

                    if isinstance(evt, UsageEvent):
                        if evt.input_tokens:
                            prompt_tokens = evt.input_tokens
                        if evt.output_tokens:
                            completion_tokens = evt.output_tokens
                        if evt.cache_read_tokens:
                            cache_read_tokens = evt.cache_read_tokens
                            span.set_attribute("llm.cache_read_tokens", cache_read_tokens)
                        if evt.cache_creation_tokens:
                            cache_creation_tokens = evt.cache_creation_tokens
                            span.set_attribute(
                                "llm.cache_creation_tokens", cache_creation_tokens
                            )

                    yield evt
            except Exception as e:
                span.set_status(StatusCode.ERROR, str(e))
                span.record_exception(e)
                raise RuntimeError(
                    f"LLM v2 streaming failed (provider={self.provider}, model={target_model_id}): {e}"
                ) from e

            # Pull final usage from client if it didn't emit a UsageEvent
            if hasattr(self.client, "pop_last_usage"):
                usage = self.client.pop_last_usage()
                if usage.prompt_tokens:
                    prompt_tokens = usage.prompt_tokens
                if usage.completion_tokens:
                    completion_tokens = usage.completion_tokens

            span.set_attribute("llm.prompt_tokens", prompt_tokens)
            span.set_attribute("llm.completion_tokens", completion_tokens)

            self._schedule_usage_record(
                scope=usage_scope,
                scope_ref_id=usage_scope_ref_id,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                cache_read_tokens=cache_read_tokens,
                cache_creation_tokens=cache_creation_tokens,
                should_record=should_record,
            )

    async def test_connection(self, prompt: str = "Hello, how are you?"):
        logger.info("Testing LLM connection: provider=%s, model=%s", self.provider, self.model_id)
        try:
            test_stream = ""
            async for chunk in self.inference_stream(prompt, should_record=False):
                if not chunk:
                    continue
                test_stream += chunk
                if len(test_stream) > 100:
                    break

            if not test_stream:
                logger.warning("LLM test connection returned empty response: provider=%s, model=%s", self.provider, self.model_id)
                return {
                    "success": False,
                    "message": "No response from the model, streaming request failed",
                }

        except Exception as e:
            logger.error("LLM test connection failed: provider=%s, model=%s, error=%s", self.provider, self.model_id, e, exc_info=True)
            return {
                "success": False,
                "message": str(e),
            }

        logger.info("LLM test connection successful: provider=%s, model=%s", self.provider, self.model_id)
        return {
            "success": True,
            "message": "Successfully connected to LLM",
        }

    def _coerce_response(self, response) -> tuple[str, LLMUsage]:
        if isinstance(response, LLMResponse):
            return response.text, response.usage or LLMUsage()
        if isinstance(response, tuple) and response:
            text = response[0]
            usage_raw = response[1] if len(response) > 1 else None
            usage = self._coerce_usage(usage_raw)
            return text, usage
        usage = self._coerce_usage(getattr(response, "usage", None))
        return response, usage

    @staticmethod
    def _coerce_usage(raw) -> LLMUsage:
        if isinstance(raw, LLMUsage):
            return raw
        if isinstance(raw, dict):
            return LLMUsage(
                prompt_tokens=int(raw.get("prompt_tokens", 0) or 0),
                completion_tokens=int(raw.get("completion_tokens", 0) or 0),
            )
        return LLMUsage()

    def _sanitize_response_text(self, response: str) -> str:
        try:
            if not isinstance(response, str):
                response = str(response)
            response = re.sub(r"^\s*```(?:[A-Za-z0-9_\-]+)?\s*\r?\n", "", response)
            response = re.sub(r"^\s*(?:json|python)\s*\r?\n", "", response, flags=re.IGNORECASE)
            response = re.sub(r"(?m)^\s*```\s*$", "", response)
            return response
        except Exception:
            raise RuntimeError("LLM inference returned a non-string response that could not be sanitized")

    def _count_tokens(self, text: str) -> int:
        if not text:
            return 0
        try:
            return count_tokens(text, getattr(self.model, "model_id", None))
        except Exception:
            return 0

    def _estimate_tokens_fast(self, text: str) -> int:
        try:
            return estimate_tokens_fast(text)
        except Exception:
            return 0

    def _schedule_usage_record(
        self,
        *,
        scope: Optional[str],
        scope_ref_id: Optional[str],
        prompt_tokens: int,
        completion_tokens: int,
        cache_read_tokens: int = 0,
        cache_creation_tokens: int = 0,
        should_record: bool,
    ):
        if not should_record or not scope or ((prompt_tokens or 0) == 0 and (completion_tokens or 0) == 0):
            return
        session_maker = self._usage_session_maker
        if session_maker is None:
            return
        try:
            async def _record_usage():
                async with session_maker() as session:
                    recorder = LLMUsageRecorderService(session)
                    await recorder.record(
                        scope=scope,
                        scope_ref_id=scope_ref_id,
                        llm_model=self.model,
                        prompt_tokens=prompt_tokens or 0,
                        completion_tokens=completion_tokens or 0,
                        cache_read_tokens=cache_read_tokens or 0,
                        cache_creation_tokens=cache_creation_tokens or 0,
                    )
                    await session.commit()
            coroutine = _record_usage()
        except Exception as exc:
            logger.debug("Failed to create LLM usage record coroutine: %s", exc)
            return

        global _MAIN_LOOP
        try:
            loop = asyncio.get_running_loop()
            _MAIN_LOOP = loop
        except RuntimeError:
            # Called from a worker thread (e.g. asyncio.to_thread(llm.inference)).
            # Fall back to the main loop captured from a previous async call.
            loop = _MAIN_LOOP if (_MAIN_LOOP is not None and _MAIN_LOOP.is_running()) else None
            if loop is None:
                logger.debug("Skipping LLM usage recording; no running loop for scope %s", scope)
                return
            try:
                asyncio.run_coroutine_threadsafe(coroutine, loop)
            except Exception as exc:
                logger.warning("Unable to schedule LLM usage recording (threadsafe): %s", exc)
            return
        try:
            loop.create_task(coroutine)
        except Exception as exc:
            logger.warning("Unable to schedule LLM usage recording: %s", exc)
