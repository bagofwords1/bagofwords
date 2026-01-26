import asyncio
import re
from typing import AsyncGenerator, Optional, Callable

from .clients.openai_client import OpenAi
from .clients.google_client import Google
from .clients.anthropic_client import Anthropic
from .clients.azure_client import AzureClient
from .types import LLMResponse, LLMUsage, ImageInput
from app.ai.utils.token_counter import count_tokens
from app.models.llm_model import LLMModel
from app.services.llm_usage_recorder import LLMUsageRecorderService
from app.settings.logging_config import get_logger
from sqlalchemy.ext.asyncio import AsyncSession

logger = get_logger(__name__)


class LLM:
    def __init__(
        self,
        model: LLMModel,
        usage_session_maker: Optional[Callable[[], "AsyncSession"]] = None,
    ):
        self.model = model
        self.model_id = model.model_id
        self.provider = model.provider.provider_type
        self.api_key = self.model.provider.decrypt_credentials()[0]
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
            # Use empty string for api_key if not provided (some local servers don't need auth)
            api_key = self.api_key or ""
            self.client = OpenAi(api_key=api_key, base_url=base_url)
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
        self._validate_vision_support(images)
        logger.debug("Model: %s, prompt: %s", self.model_id, prompt)
        prompt_tokens_estimate = self._count_tokens(prompt)
        try:
            response = self.client.inference(model_id=self.model_id, prompt=prompt, images=images)
        except Exception as e:
            raise RuntimeError(f"LLM inference failed (provider={self.provider}, model={self.model_id}): {e}") from e
        logger.debug("Response: %s", response)

        text, usage = self._coerce_response(response)
        if not usage.prompt_tokens and not usage.completion_tokens and hasattr(self.client, "pop_last_usage"):
            usage = self.client.pop_last_usage()
        sanitized = self._sanitize_response_text(text)
        completion_tokens = usage.completion_tokens or self._count_tokens(sanitized)
        prompt_tokens = usage.prompt_tokens or prompt_tokens_estimate

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
    ) -> AsyncGenerator[str, None]:
        self._validate_vision_support(images)
        logger.debug("Model: %s, prompt: %s", self.model_id, prompt)
        started_payload = False
        prefix = ""
        prompt_tokens = self._count_tokens(prompt)
        completion_tokens = 0
        streamed_chunks: list[str] = []
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
                            completion_tokens += self._count_tokens(emission)
                            streamed_chunks.append(emission)
                            yield emission
                        else:
                            continue
                    else:
                        started_payload = True
                        emission = prefix[m.start():]
                        prefix = ""
                        completion_tokens += self._count_tokens(emission)
                        streamed_chunks.append(emission)
                        yield emission
                else:
                    if "```" in chunk:
                        chunk = chunk.replace("```", "")
                    completion_tokens += self._count_tokens(chunk)
                    streamed_chunks.append(chunk)
                    yield chunk
        except Exception as e:
            raise RuntimeError(f"LLM streaming failed (provider={self.provider}, model={self.model_id}): {e}") from e
        usage = LLMUsage()
        if hasattr(self.client, "pop_last_usage"):
            usage = self.client.pop_last_usage()
        if usage.prompt_tokens or usage.completion_tokens:
            prompt_tokens = usage.prompt_tokens or prompt_tokens
            completion_tokens = usage.completion_tokens or completion_tokens
        else:
            completion_tokens = self._count_tokens("".join(streamed_chunks)) or completion_tokens
        self._schedule_usage_record(
            scope=usage_scope,
            scope_ref_id=usage_scope_ref_id,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            should_record=should_record,
        )

    async def test_connection(self, prompt: str = "Hello, how are you?"):
        try:
            test_inference = self.inference(prompt, should_record=False)

            if not isinstance(test_inference, str) or not test_inference.strip():
                return {
                    "success": False,
                    "message": "No response from the model, regular inference request failed",
                }

            test_stream = ""
            async for chunk in self.inference_stream(prompt, should_record=False):
                if not chunk:
                    continue
                test_stream += chunk
                if len(test_stream) > 100:
                    break

            if not test_stream:
                return {
                    "success": False,
                    "message": "No response from the model, streaming request failed",
                }

        except Exception as e:
            return {
                "success": False,
                "message": str(e),
            }

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

    def _schedule_usage_record(
        self,
        *,
        scope: Optional[str],
        scope_ref_id: Optional[str],
        prompt_tokens: int,
        completion_tokens: int,
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
                    )
                    await session.commit()
            coroutine = _record_usage()
        except Exception as exc:
            logger.debug("Failed to create LLM usage record coroutine: %s", exc)
            return

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.debug("Skipping LLM usage recording; no running loop for scope %s", scope)
            return
        try:
            loop.create_task(coroutine)
        except Exception as exc:
            logger.warning("Unable to schedule LLM usage recording: %s", exc)