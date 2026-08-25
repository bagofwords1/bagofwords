import asyncio
import random
import re
import time
from typing import AsyncGenerator, Optional, Callable

from .clients.openai_client import OpenAi
from .clients.openai_responses_client import OpenAIResponsesClient
from .clients.google_client import Google
from .clients.anthropic_client import Anthropic
from .clients.azure_client import AzureClient
from .clients.bedrock_client import BedrockClient
from .types import (
    ImageInput,
    ImageOutput,
    LLMResponse,
    LLMStreamEvent,
    LLMUsage,
    Message,
    ToolSpec,
    UsageEvent,
)
from app.ai.utils.token_counter import count_tokens, estimate_tokens_fast
from app.ai.llm import trace as llm_trace
from app.ai.llm.pii.loader import load_redactor_for_org
from app.ai.llm.pii.redactor import PiiRedactor, PiiPromptBlockedError
from app.models.llm_model import LLMModel
from app.ai.llm.usage_attribution import get_usage_attribution
from app.services.llm_usage_recorder import LLMUsageRecorderService
from app.services.usage_policy_service import UsageLimitContext, usage_policy_service
from app.settings.logging_config import get_logger
from app.core.otel import get_tracer
from opentelemetry.trace import StatusCode
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

logger = get_logger(__name__)
tracer = get_tracer(__name__)

# Shared reference to the app's main asyncio loop. Populated lazily the
# first time _schedule_usage_record runs from an async context, so that
# later calls from worker threads (e.g. asyncio.to_thread(llm.inference))
# can still schedule usage recording via run_coroutine_threadsafe.
_MAIN_LOOP: Optional[asyncio.AbstractEventLoop] = None

# Strong references to in-flight usage-record tasks. asyncio only keeps a weak
# reference to tasks created via loop.create_task(), so a fire-and-forget task
# can be garbage-collected mid-execution before its DB commit lands — silently
# dropping the usage record. Holding a reference here until the task completes
# closes that gap. This adds no latency: the tasks still run in the background,
# off the response path. The set is bounded by in-flight count and self-drains.
_PENDING_RECORD_TASKS: set = set()

# Bounded retry for transient provider failures. Deliberately small: the
# agent-level planner retries and the EE fallback chain sit above this, so the
# façade only smooths over blips — it must not mask a real outage from them.
_MAX_SYNC_RETRIES = 2
_MAX_STREAM_RETRIES = 1
# 'quota' is deliberately absent: an exhausted allowance or empty credit
# balance is not transient, so retrying it only adds backoff latency to a call
# that cannot succeed. It goes straight up to the fallback chain instead.
_RETRYABLE_CODES = ("rate_limit", "network", "provider_error")


def _is_transient_llm_error(exc: BaseException, *, provider: str, model: Optional[str]) -> bool:
    try:
        from .errors import classify
        return classify(exc, provider=provider, model=model).code in _RETRYABLE_CODES
    except Exception:
        return False


def _parse_temperature(raw) -> Optional[float]:
    """Sampling temperature from provider additional_config, or None.

    None means "don't send the parameter" — the only universally safe default
    for OpenAI-compatible endpoints. Values outside [0, 2] (the OpenAI API's
    accepted range) are treated as unset rather than forwarded to fail.
    """
    if raw is None or isinstance(raw, bool):
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if not (0.0 <= value <= 2.0):
        return None
    return value


# Public Azure AI Foundry resources are always on this host suffix.
_AZURE_FOUNDRY_HOST_SUFFIX = ".services.ai.azure.com"


def _is_anthropic_model_id(model_id: Optional[str]) -> bool:
    """Whether a deployment name denotes an Anthropic model.

    This is the primary routing signal for the Azure provider, ahead of the
    endpoint. Measured against a live Foundry resource: an Anthropic deployment
    answers ONLY on ``/anthropic/v1/messages``, and it answers there on every
    hostname the resource carries (``services.ai``, ``cognitiveservices``,
    ``openai.azure.com`` alike), while both OpenAI-shaped routes reject it with
    ``404 api_not_supported``. So the model family decides the surface and the
    hostname does not enter into it.

    Deployment names are admin-chosen, so this is a heuristic. Foundry defaults
    the name to the catalog id (``claude-haiku-4-5``), and a renamed deployment
    still matches as long as the name carries the family. One named to hide it
    entirely lands on the OpenAI surface and fails as ``model_not_found`` —
    immediate and legible, not a stall.
    """
    name = (model_id or "").strip().lower()
    return "claude" in name or "anthropic" in name


def _azure_foundry_anthropic_base_url(endpoint_url: str) -> str:
    """Derive the Foundry Anthropic base_url from a provider endpoint.

    Foundry serves Anthropic deployments at ``<resource>/anthropic/v1/messages``
    — the native Messages API, not the OpenAI-compatible surface, which answers
    them with ``404 api_not_supported``.

    Returns the ``/anthropic`` root and NOT ``/anthropic/v1``: the Anthropic SDK
    appends its own ``/v1/messages`` to whatever base_url it is given, so
    passing the versioned path produces ``/anthropic/v1/v1/messages``.
    """
    base = LLM._azure_v1_base_url(endpoint_url)
    # _azure_v1_base_url already normalized away portal paths and settled the
    # scheme/host; swap its /openai/v1/ tail for the Anthropic root.
    return base[: -len("openai/v1/")] + "anthropic"


def _is_azure_foundry_endpoint(endpoint_url: str) -> bool:
    """Whether an Azure endpoint is a Foundry resource, by hostname.

    Only decides which OpenAI-shaped route to use — the deployment-scoped one
    the Azure SDK builds, or ``/openai/v1``. Foundry serves both, so this is not
    a correctness fork there; it matters for a classic Azure OpenAI resource,
    which may only serve the deployment route. Defaulting anything unrecognized
    to that route therefore keeps existing providers on their proven path.

    Anthropic deployments never reach this function — they are routed by model
    family, which holds on every hostname (see _is_anthropic_model_id).
    """
    base = (endpoint_url or "").strip().rstrip("/").lower()
    host = base.split("://", 1)[-1].split("/", 1)[0].split(":", 1)[0]
    return host.endswith(_AZURE_FOUNDRY_HOST_SUFFIX) or "/openai/v1" in base


def _retry_delay(attempt: int) -> float:
    """Jittered exponential backoff: 0.5s, 1s, 2s… capped at 4s."""
    return min(0.5 * (2 ** attempt), 4.0) + random.uniform(0, 0.25)


class LLM:
    def __init__(
        self,
        model: LLMModel,
        usage_session_maker: Optional[Callable[[], "AsyncSession"]] = None,
        usage_context: Optional[UsageLimitContext] = None,
        pii_redactor: Optional[PiiRedactor] = None,
    ):
        self.model = model
        self.model_id = model.model_id
        self.provider = model.provider.provider_type
        # PII protection (enterprise). Resolved lazily and cached per org from
        # the model's organization, so redaction is on-by-construction at every
        # LLM call site rather than threaded through ~29 constructors. An
        # explicit ``pii_redactor`` (tests / overrides) short-circuits loading.
        self._organization_id = getattr(model, "organization_id", None)
        self._pii_redactor_override = pii_redactor
        self._pii_redactor: Optional[PiiRedactor] = pii_redactor
        self._pii_loaded = pii_redactor is not None
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = _MAIN_LOOP
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
        self._usage_limit_context = usage_context
        additional_config = self.model.provider.additional_config or {}
        enable_web_search = bool(additional_config.get("enable_web_search", False))
        # Optional explicit sampling temperature. Model-level config (set via
        # POST /llm/models/{id}/set_temperature, stored on LLMModel.config) wins
        # over the provider-level additional_config escape hatch. When neither is
        # set, each client keeps its default posture — the OpenAI-compatible
        # client sends no temperature at all (the endpoint's own default
        # applies, and models that reject non-default temperatures — Claude
        # Sonnet 5+, gpt-5 behind LiteLLM aliases — stop 400ing).
        model_config = getattr(self.model, "config", None) or {}
        configured_temperature = _parse_temperature(model_config.get("temperature"))
        if configured_temperature is None:
            configured_temperature = _parse_temperature(additional_config.get("temperature"))
        if self.provider == "openai":
            base_url = additional_config.get("base_url")
            if base_url:
                # Custom base URL on openai provider → use Chat Completions (compatible endpoint)
                self.client = OpenAi(api_key=self.api_key, base_url=base_url, temperature=configured_temperature)
            else:
                # Default OpenAI → Responses API (supports reasoning content)
                self.client = OpenAIResponsesClient(
                    api_key=self.api_key,
                    enable_web_search=enable_web_search,
                    temperature=configured_temperature,
                )
        elif self.provider == "anthropic":
            self.client = Anthropic(api_key=self.api_key, temperature=configured_temperature)
        elif self.provider == "google":
            self.client = Google(api_key=self.api_key, temperature=configured_temperature)
        elif self.provider == "azure":
            endpoint_url = additional_config.get("endpoint_url")
            if not endpoint_url:
                raise ValueError("Azure provider requires endpoint_url in additional_config")
            if _is_anthropic_model_id(self.model_id):
                # Model family first, endpoint second. An Anthropic deployment
                # answers only on /anthropic/v1 (the native Messages API), and
                # answers there on every hostname an Azure AI Foundry resource
                # carries — so no endpoint inspection can improve on this, and
                # any that tried would get it wrong for the resource's
                # openai.azure.com alias.
                self.client = Anthropic(
                    api_key=self.api_key,
                    base_url=_azure_foundry_anthropic_base_url(endpoint_url),
                    temperature=configured_temperature,
                )
            elif bool(additional_config.get("use_responses_api", False)):
                # Opt-in, and required for native web search: the Responses API
                # is only served in some Azure regions, so Chat Completions
                # stays the default. Both surfaces expose it at the same
                # /openai/v1 base.
                self.client = OpenAIResponsesClient(
                    api_key=self.api_key,
                    base_url=self._azure_v1_base_url(endpoint_url),
                    enable_web_search=enable_web_search,
                    temperature=configured_temperature,
                )
            elif _is_azure_foundry_endpoint(endpoint_url):
                # Foundry: OpenAI-compatible Chat Completions under /openai/v1,
                # reached with the plain OpenAI client rather than the Azure
                # SDK's deployment-scoped one.
                self.client = OpenAi(
                    api_key=self.api_key,
                    base_url=self._azure_v1_base_url(endpoint_url),
                    temperature=configured_temperature,
                )
            else:
                self.client = AzureClient(
                    api_key=self.api_key,
                    endpoint_url=endpoint_url,
                    temperature=configured_temperature,
                )
        elif self.provider == "custom":
            base_url = self.model.provider.additional_config.get("base_url") if self.model.provider.additional_config else None
            if not base_url:
                raise ValueError("Custom provider requires base_url in additional_config")
            verify_ssl = self.model.provider.additional_config.get("verify_ssl", True) if self.model.provider.additional_config else True
            # Use empty string for api_key if not provided (some local servers don't need auth)
            api_key = self.api_key or ""
            self.client = OpenAi(api_key=api_key, base_url=base_url, verify_ssl=verify_ssl, temperature=configured_temperature)
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

    @staticmethod
    def _azure_v1_base_url(endpoint_url: str) -> str:
        """Derive the ``/openai/v1`` base_url from a provider endpoint.

        The AzureClient (Chat Completions) takes the resource root
        (``https://<resource>.openai.azure.com``). The v1 surface — Responses on
        Azure OpenAI, and everything on Azure AI Foundry
        (``https://<resource>.services.ai.azure.com``) — lives under
        ``/openai/v1/`` on that same host. Accept either form so admins can paste
        whichever they have, and normalize to the v1 base the OpenAI client wants.
        """
        base = (endpoint_url or "").strip().rstrip("/")
        if "/openai/v1" in base:
            # Already a v1 base (possibly without trailing slash).
            return base.split("/openai/v1")[0] + "/openai/v1/"
        if base.endswith("/openai"):
            return base + "/v1/"
        # Strip the non-inference paths the Azure portal hands out, so pasting
        # one does not silently produce a URL that exists nowhere. The Foundry
        # portal shows the project endpoint
        # (``.../api/projects/<name>``) far more prominently than the resource
        # root, and ``/models`` is the older Azure AI Inference route; appending
        # /openai/v1 to either yields a path APIM answers with a 401 whose
        # message blames the key, which is near-impossible to debug from the UI.
        base = re.sub(r"/api/projects/[^/]+$", "", base)
        base = re.sub(r"/models$", "", base)
        return base + "/openai/v1/"

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

    async def _aget_pii_redactor(self) -> Optional[PiiRedactor]:
        """Async resolution of this org's PII redactor (cached)."""
        # Tolerate objects built via object.__new__(LLM) that bypass __init__
        # (some tests do this) — no pii state means no redaction.
        if not hasattr(self, "_pii_loaded"):
            return None
        if self._pii_loaded:
            return self._pii_redactor
        try:
            self._pii_redactor = await load_redactor_for_org(
                self._organization_id, self._usage_session_maker
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("PII redactor resolution failed: %s", exc)
            self._pii_redactor = None
        self._pii_loaded = True
        return self._pii_redactor

    def _get_pii_redactor_sync(self) -> Optional[PiiRedactor]:
        """Sync resolution for the ``inference`` path (always dispatched via
        ``asyncio.to_thread``). Bridges to the app's event loop — running on the
        main thread — to run the async settings load, the same mechanism usage
        recording uses. Never blocks the loop thread on itself."""
        # Tolerate objects built via object.__new__(LLM) that bypass __init__.
        if not hasattr(self, "_pii_loaded"):
            return None
        if self._pii_loaded:
            return self._pii_redactor

        # If a loop is running on THIS thread we were (unexpectedly) called on
        # the event-loop thread rather than via to_thread. Blocking on that loop
        # would deadlock, so leave the redactor unloaded and let a later threaded
        # call resolve it. The async inference paths always load via await, so
        # streaming (the tool-use path) stays covered regardless.
        try:
            asyncio.get_running_loop()
            logger.debug("PII redactor: sync inference on loop thread; deferring load")
            return None
        except RuntimeError:
            pass

        loop = self._loop if (self._loop is not None and self._loop.is_running()) else _MAIN_LOOP
        if loop is None or not loop.is_running():
            return None  # no loop reachable yet; retry on a later call
        try:
            fut = asyncio.run_coroutine_threadsafe(
                load_redactor_for_org(self._organization_id, self._usage_session_maker),
                loop,
            )
            self._pii_redactor = fut.result(timeout=10)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("PII redactor resolution (sync) failed: %s", exc)
            self._pii_redactor = None
        self._pii_loaded = True
        return self._pii_redactor

    def _apply_pii(self, prompt: str, redactor: Optional[PiiRedactor], span) -> str:
        """Apply redaction to a prompt. Raises PiiPromptBlockedError in block
        mode. Records a non-sensitive summary on the span."""
        if redactor is None or not redactor.active:
            return prompt
        redacted, result = redactor.apply(prompt)
        if result.redacted:
            try:
                span.set_attribute("llm.pii_redacted", True)
                span.set_attribute("llm.pii_rules", ",".join(m["id"] for m in result.matches))
                span.set_attribute("llm.pii_match_count", sum(m["count"] for m in result.matches))
            except Exception:
                pass
            logger.info(
                "PII protection applied (mode=%s, rules=%s)",
                redactor.mode,
                ",".join(f"{m['id']}:{m['count']}" for m in result.matches),
            )
        return redacted

    def _apply_pii_v2(self, system, messages, redactor: Optional[PiiRedactor], span):
        """Redact the system prompt and every text-bearing block across the
        conversation for the native tool-use path. Returns (system, messages).
        Raises PiiPromptBlockedError in block mode if anything matched."""
        if redactor is None or not redactor.active:
            return system, messages

        all_matches: list = []

        def _redact_str(value: str) -> str:
            if not isinstance(value, str) or not value:
                return value
            result = redactor.scan(value)
            if result.redacted:
                all_matches.extend(result.matches)
            return result.text

        new_system = _redact_str(system) if isinstance(system, str) else system

        new_messages = []
        for message in messages or []:
            content = getattr(message, "content", None)
            if isinstance(content, str):
                new_content = _redact_str(content)
            elif isinstance(content, list):
                new_content = []
                for block in content:
                    if isinstance(block, dict):
                        nb = dict(block)
                        # Redact the two string fields that carry model-visible
                        # text: 'text' (text blocks) and 'content' (tool_result).
                        for key in ("text", "content"):
                            if isinstance(nb.get(key), str):
                                nb[key] = _redact_str(nb[key])
                        new_content.append(nb)
                    else:
                        new_content.append(block)
            else:
                new_messages.append(message)
                continue
            new_messages.append(Message(role=message.role, content=new_content))

        if all_matches:
            # Aggregate counts per rule for the summary/telemetry.
            agg: dict = {}
            for m in all_matches:
                entry = agg.setdefault(
                    m["id"], {"id": m["id"], "name": m["name"], "count": 0, "action": m.get("action", "replace")}
                )
                entry["count"] += m["count"]
            aggregated = list(agg.values())
            blocked = [a["name"] for a in aggregated if a.get("action") == "block"]
            try:
                span.set_attribute("llm.pii_redacted", True)
                span.set_attribute("llm.pii_rules", ",".join(a["id"] for a in aggregated))
                span.set_attribute("llm.pii_match_count", sum(a["count"] for a in aggregated))
            except Exception:
                pass
            logger.info(
                "PII protection applied (rules=%s)",
                ",".join(f"{a['id']}:{a['count']}:{a['action']}" for a in aggregated),
            )
            # Block wins: if any block-action rule matched anywhere, refuse.
            if blocked:
                raise PiiPromptBlockedError(blocked)

        return new_system, new_messages

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
            prompt = self._apply_pii(prompt, self._get_pii_redactor_sync(), span)
            logger.debug("Model: %s, prompt: %s", self.model_id, prompt)
            prompt_tokens_estimate = self._count_tokens(prompt)
            span.set_attribute("llm.prompt_tokens_estimate", prompt_tokens_estimate)
            self._check_usage_limit_sync(prompt_tokens_estimate, should_record=should_record)
            response = None
            for _attempt in range(_MAX_SYNC_RETRIES + 1):
                try:
                    response = self.client.inference(model_id=self.model_id, prompt=prompt, images=images)
                    break
                except Exception as e:
                    if _attempt >= _MAX_SYNC_RETRIES or not _is_transient_llm_error(
                        e, provider=self.provider, model=self.model_id
                    ):
                        span.set_status(StatusCode.ERROR, str(e))
                        span.record_exception(e)
                        raise RuntimeError(f"LLM inference failed (provider={self.provider}, model={self.model_id}): {e}") from e
                    delay = _retry_delay(_attempt)
                    logger.warning(
                        "LLM inference transient error (provider=%s, model=%s); retry %d/%d in %.1fs: %s",
                        self.provider, self.model_id, _attempt + 1, _MAX_SYNC_RETRIES, delay, e,
                    )
                    span.add_event("llm.retry", {"attempt": _attempt + 1})
                    # Sync path runs via asyncio.to_thread — blocking sleep is fine.
                    time.sleep(delay)
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
            self._record_usage_limit_sync(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                cache_read_tokens=usage.cache_read_tokens,
                cache_creation_tokens=usage.cache_creation_tokens,
                scope=usage_scope,
                scope_ref_id=usage_scope_ref_id,
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
            prompt = self._apply_pii(prompt, await self._aget_pii_redactor(), span)
            logger.debug("Model: %s, prompt: %s", self.model_id, prompt)
            started_payload = False
            prefix = ""
            prompt_tokens = prompt_tokens_estimate if prompt_tokens_estimate is not None else self._estimate_tokens_fast(prompt)
            span.set_attribute("llm.prompt_tokens_estimate", prompt_tokens)
            await self._check_usage_limit_async(prompt_tokens, should_record=should_record)
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
            await self._record_usage_limit_async(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                scope=usage_scope,
                scope_ref_id=usage_scope_ref_id,
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
        web_search: Optional[bool] = None,
        web_search_domains: Optional[list] = None,
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
            system, messages = self._apply_pii_v2(
                system, messages, await self._aget_pii_redactor(), span
            )

            prompt_tokens = (
                prompt_tokens_estimate
                if prompt_tokens_estimate is not None
                else 0
            )
            if not prompt_tokens:
                prompt_parts = [system or ""]
                for message in messages or []:
                    content = getattr(message, "content", "")
                    prompt_parts.append(content if isinstance(content, str) else str(content))
                prompt_tokens = self._estimate_tokens_fast("\n".join(prompt_parts))
            await self._check_usage_limit_async(prompt_tokens, should_record=should_record)
            completion_tokens = 0
            cache_read_tokens = 0
            cache_creation_tokens = 0
            stream_start = time.monotonic()
            ttft_recorded = False

            # `web_search` (native, provider-executed) is only honored by the
            # OpenAI Responses client. Forward it just to that client so the
            # other clients' signatures stay untouched.
            client_kwargs: dict = {}
            if web_search is not None and isinstance(self.client, OpenAIResponsesClient):
                client_kwargs["web_search"] = web_search
                if web_search_domains:
                    client_kwargs["web_search_domains"] = web_search_domains

            # Bounded retry, but only while the stream hasn't produced anything:
            # once an event reached the consumer we can't transparently restart
            # (partial content may already be on the SSE wire) — mid-stream
            # failures surface to the agent-level retry/fallback instead.
            _stream_attempt = 0
            # Per-call I/O trace (off unless BOW_LLM_TRACE_FILE is set). Built
            # here so it records the post-PII request actually sent, and
            # finalized after the stream so usage/TTFT land on the same row.
            _trace_record = (
                llm_trace.build_record(
                    provider=self.provider,
                    model_id=target_model_id,
                    client=type(self.client).__name__,
                    system=system,
                    messages=messages,
                    tools=tools,
                    usage_scope=usage_scope,
                    thinking=bool(thinking),
                    image_count=len(images or []),
                )
                if llm_trace.enabled()
                else None
            )
            _trace_events: dict = {}
            _trace_stop_reason = None
            while True:
                _received_any = False
                try:
                    async for evt in self.client.inference_stream_v2(
                        model_id=target_model_id,
                        messages=messages,
                        system=system,
                        tools=tools,
                        images=images,
                        thinking=thinking,
                        disable_parallel_tools=disable_parallel_tools,
                        **client_kwargs,
                    ):
                        _received_any = True
                        if _trace_record is not None:
                            _et = getattr(evt, "type", None)
                            _trace_events[_et] = _trace_events.get(_et, 0) + 1
                            if _et == "message_stop":
                                _trace_stop_reason = getattr(evt, "stop_reason", None)
                        if not ttft_recorded and getattr(evt, "type", None) in (
                            "text_delta",
                            "tool_use_start",
                        ):
                            ttft_ms = (time.monotonic() - stream_start) * 1000
                            span.set_attribute("llm.ttft_ms", ttft_ms)
                            span.add_event("ttft", {"ttft_ms": ttft_ms})
                            ttft_recorded = True
                            if _trace_record is not None:
                                _trace_record["ttft_ms"] = round(ttft_ms, 1)

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
                    break
                except Exception as e:
                    if (
                        not _received_any
                        and _stream_attempt < _MAX_STREAM_RETRIES
                        and _is_transient_llm_error(e, provider=self.provider, model=target_model_id)
                    ):
                        delay = _retry_delay(_stream_attempt)
                        _stream_attempt += 1
                        logger.warning(
                            "LLM v2 stream transient error (provider=%s, model=%s); retry %d/%d in %.1fs: %s",
                            self.provider, target_model_id, _stream_attempt, _MAX_STREAM_RETRIES, delay, e,
                        )
                        span.add_event("llm.retry", {"attempt": _stream_attempt})
                        await asyncio.sleep(delay)
                        continue
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

            if _trace_record is not None:
                _trace_record.update(
                    duration_ms=round((time.monotonic() - stream_start) * 1000, 1),
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    cache_read_tokens=cache_read_tokens,
                    cache_creation_tokens=cache_creation_tokens,
                    uncached_input_tokens=max(
                        int(prompt_tokens or 0) - int(cache_read_tokens or 0), 0
                    ),
                    stop_reason=_trace_stop_reason,
                    events=_trace_events,
                    retries=_stream_attempt,
                )
                llm_trace.write(_trace_record)

            self._schedule_usage_record(
                scope=usage_scope,
                scope_ref_id=usage_scope_ref_id,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                cache_read_tokens=cache_read_tokens,
                cache_creation_tokens=cache_creation_tokens,
                should_record=should_record,
            )
            await self._record_usage_limit_async(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                cache_read_tokens=cache_read_tokens,
                cache_creation_tokens=cache_creation_tokens,
                scope=usage_scope,
                scope_ref_id=usage_scope_ref_id,
                should_record=should_record,
            )

    def _validate_image_generation_support(self) -> None:
        """Raise if the selected model is not an image-generation model."""
        if not getattr(self.model, "supports_image_generation", False):
            raise ValueError(
                f"Model '{self.model_id}' does not support image generation. "
                "Select an image-generation model (e.g. gpt-image-1)."
            )

    async def generate_image(
        self,
        prompt: str,
        *,
        size: Optional[str] = None,
        quality: Optional[str] = None,
        images: Optional[list[ImageInput]] = None,
        usage_scope: Optional[str] = None,
        usage_scope_ref_id: Optional[str] = None,
        should_record: bool = True,
    ) -> ImageOutput:
        """Generate an image and return it as an :class:`ImageOutput`.

        Gated by the model's ``supports_image_generation`` capability. Token usage
        (image models report image-token counts) is recorded through the same
        pipeline as text calls so cost/monitoring stay populated.
        """
        with tracer.start_as_current_span("llm.generate_image") as span:
            span.set_attribute("llm.model_id", self.model_id)
            span.set_attribute("llm.provider", self.provider)
            self._validate_image_generation_support()
            try:
                result = await self.client.generate_image(
                    model_id=self.model_id,
                    prompt=prompt,
                    size=size,
                    quality=quality,
                    images=images,
                )
            except Exception as e:
                span.set_status(StatusCode.ERROR, str(e))
                span.record_exception(e)
                raise RuntimeError(
                    f"Image generation failed (provider={self.provider}, model={self.model_id}): {e}"
                ) from e

            usage = result.usage or LLMUsage()
            prompt_tokens = usage.prompt_tokens
            completion_tokens = usage.completion_tokens
            span.set_attribute("llm.prompt_tokens", prompt_tokens)
            span.set_attribute("llm.completion_tokens", completion_tokens)

            self._schedule_usage_record(
                scope=usage_scope,
                scope_ref_id=usage_scope_ref_id,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                should_record=should_record,
            )
            await self._record_usage_limit_async(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                scope=usage_scope,
                scope_ref_id=usage_scope_ref_id,
                should_record=should_record,
            )
            return result

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

    def _quota_total_tokens(
        self,
        *,
        prompt_tokens: int,
        completion_tokens: int,
        cache_read_tokens: int = 0,
        cache_creation_tokens: int = 0,
    ) -> int:
        total = (prompt_tokens or 0) + (completion_tokens or 0)
        if self.provider == "anthropic":
            total += (cache_read_tokens or 0) + (cache_creation_tokens or 0)
        return max(int(total), 0)

    def _quota_cost_micro_usd(
        self,
        *,
        prompt_tokens: int,
        completion_tokens: int,
        cache_read_tokens: int = 0,
        cache_creation_tokens: int = 0,
    ) -> int:
        """Dollar cost of this call, in micro-USD, for the spend quota.

        Reuses the same per-model rate math as the LLM usage recorder so the
        spend counter and the Cost console stay consistent. Returns 0 when the
        model has no configured rates.
        """
        try:
            input_cost = LLMUsageRecorderService._calc_input_cost(
                self.model,
                prompt_tokens or 0,
                cache_read_tokens or 0,
                cache_creation_tokens or 0,
                self.provider,
            )
            output_cost = LLMUsageRecorderService._calc_output_cost(
                self.model, completion_tokens or 0
            )
        except Exception:
            return 0
        return max(int(round((input_cost + output_cost) * 1_000_000)), 0)

    def _check_usage_limit_sync(self, requested_tokens: int, *, should_record: bool) -> None:
        """Pre-LLM-call quota check. Routed through the context cache so
        steady-state checks are in-memory (no DB roundtrip per call).
        """
        context = self._usage_limit_context
        if not should_record or context is None or context.session_maker is None:
            return
        context.run_blocking(context.check_tokens(requested_tokens))

    async def _check_usage_limit_async(self, requested_tokens: int, *, should_record: bool) -> None:
        """Async variant of the cache-aware quota check."""
        context = self._usage_limit_context
        if not should_record or context is None or context.session_maker is None:
            return
        await context.check_tokens(requested_tokens)

    def _record_usage_limit_sync(
        self,
        *,
        prompt_tokens: int,
        completion_tokens: int,
        cache_read_tokens: int = 0,
        cache_creation_tokens: int = 0,
        scope: Optional[str],
        scope_ref_id: Optional[str],
        should_record: bool,
    ) -> None:
        """Record tokens against the per-agent quota accumulator.

        Used to issue a synchronous DB write (FOR UPDATE + UPDATE) that
        blocked every LLM call on a row lock. Now it just bumps an
        in-memory counter on the shared `UsageLimitContext`; the agent
        flushes once at end of run via `context.flush()`.
        """
        context = self._usage_limit_context
        if not should_record or context is None or context.session_maker is None:
            return
        total_tokens = self._quota_total_tokens(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cache_read_tokens=cache_read_tokens,
            cache_creation_tokens=cache_creation_tokens,
        )
        if total_tokens <= 0:
            return
        metadata = {
            "provider": self.provider,
            "model_id": self.model_id,
            "scope": scope or context.source,
            "scope_ref_id": scope_ref_id or context.source_ref_id,
            "prompt_tokens": prompt_tokens or 0,
            "completion_tokens": completion_tokens or 0,
            "cache_read_tokens": cache_read_tokens or 0,
            "cache_creation_tokens": cache_creation_tokens or 0,
        }
        context.add_tokens(total_tokens, metadata)
        cost_micro = self._quota_cost_micro_usd(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cache_read_tokens=cache_read_tokens,
            cache_creation_tokens=cache_creation_tokens,
        )
        if cost_micro > 0:
            context.add_cost(cost_micro, metadata)

    async def _record_usage_limit_async(
        self,
        *,
        prompt_tokens: int,
        completion_tokens: int,
        cache_read_tokens: int = 0,
        cache_creation_tokens: int = 0,
        scope: Optional[str],
        scope_ref_id: Optional[str],
        should_record: bool,
    ) -> None:
        """Async variant — same in-memory accumulation, never blocks on DB."""
        context = self._usage_limit_context
        if not should_record or context is None or context.session_maker is None:
            return
        total_tokens = self._quota_total_tokens(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cache_read_tokens=cache_read_tokens,
            cache_creation_tokens=cache_creation_tokens,
        )
        if total_tokens <= 0:
            return
        metadata = {
            "provider": self.provider,
            "model_id": self.model_id,
            "scope": scope or context.source,
            "scope_ref_id": scope_ref_id or context.source_ref_id,
            "prompt_tokens": prompt_tokens or 0,
            "completion_tokens": completion_tokens or 0,
            "cache_read_tokens": cache_read_tokens or 0,
            "cache_creation_tokens": cache_creation_tokens or 0,
        }
        context.add_tokens(total_tokens, metadata)
        cost_micro = self._quota_cost_micro_usd(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cache_read_tokens=cache_read_tokens,
            cache_creation_tokens=cache_creation_tokens,
        )
        if cost_micro > 0:
            context.add_cost(cost_micro, metadata)

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
        if not should_record or ((prompt_tokens or 0) == 0 and (completion_tokens or 0) == 0):
            return
        # Safety net: a call that reaches here with tokens but no scope is a
        # call site that forgot to label itself. Rather than silently dropping
        # the record (the old behavior, which made tokens vanish from
        # /monitoring), record it under "unscoped" and warn so it's visible.
        if not scope:
            logger.warning(
                "LLM usage recorded without a usage_scope (provider=%s model=%s); "
                "labeling as 'unscoped'. Add usage_scope at the call site.",
                self.provider, self.model_id,
            )
            scope = "unscoped"
        session_maker = self._usage_session_maker
        if session_maker is None:
            return

        # Snapshot attribution NOW, synchronously, while we're still in the LLM
        # call's own context. The actual DB write runs on a background task /
        # worker thread where the contextvar may not have propagated.
        attribution = get_usage_attribution()

        async def _record_usage():
            # SQLite: the agent's writer transaction can hold the lock while a
            # tool/code execution is in flight, so the first attempt may lose.
            # This is a background task — a few spaced retries recover nearly
            # every record instead of silently dropping it (these rows back
            # /monitoring and cost accounting; the drop was measurable — see
            # docs/feedback-loops/agent-latency-deep-dive.md).
            delays = (0.0, 0.5, 2.0, 5.0)
            for attempt, delay in enumerate(delays, start=1):
                if delay:
                    await asyncio.sleep(delay)
                try:
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
                            organization_id=attribution.get("organization_id"),
                            user_id=attribution.get("user_id"),
                            report_id=attribution.get("report_id"),
                            data_source_id=attribution.get("data_source_id"),
                            routed=bool(attribution.get("routed")),
                            baseline_model_id=attribution.get("baseline_model_id"),
                        )
                        await session.commit()
                    return
                except Exception as exc:
                    if self._is_sqlite_lock_error(exc):
                        if attempt < len(delays):
                            continue
                        logger.warning(
                            "Dropping LLM usage record after %d lock retries: scope=%s",
                            attempt, scope,
                        )
                        return
                    logger.warning("Skipping LLM usage record after unexpected failure: %s", exc)
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
                asyncio.run_coroutine_threadsafe(_record_usage(), loop)
            except Exception as exc:
                logger.warning("Unable to schedule LLM usage recording (threadsafe): %s", exc)
            return
        try:
            task = loop.create_task(_record_usage())
            # Retain a strong reference until the task finishes so it can't be
            # GC'd mid-flight (see _PENDING_RECORD_TASKS note above).
            _PENDING_RECORD_TASKS.add(task)
            task.add_done_callback(_PENDING_RECORD_TASKS.discard)
        except Exception as exc:
            logger.warning("Unable to schedule LLM usage recording: %s", exc)

    @staticmethod
    def _is_sqlite_lock_error(exc: Exception) -> bool:
        if not isinstance(exc, OperationalError):
            return False
        message = str(exc).lower()
        return "database is locked" in message or "database table is locked" in message
