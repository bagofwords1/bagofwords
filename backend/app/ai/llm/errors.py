"""Centralized LLM error classification.

Turns provider-raised exceptions into a structured shape suitable for SSE
emission and DB storage. Always preserves the raw provider message so the
user can see what actually went wrong, even when our classifier doesn't
recognize the error code. The friendly ``summary`` is for known cases
(auth, rate_limit, etc.) and is purely additive — never replaces the
provider's actual error text.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
import re
from typing import Optional


# Stable codes the frontend keys on for localized titles and toast variants.
# 'unknown' is intentionally common and well-supported — when we don't
# recognize an error, the user still sees the provider's actual message,
# just labeled as 'unknown' so the frontend doesn't wrongly route it
# (e.g. won't link to "Settings → LLM Providers" if it isn't actually
# an auth issue).
ERROR_CODES = (
    "auth",
    "rate_limit",
    "quota",
    "context_length",
    "model_not_found",
    "provider_error",
    "network",
    "unknown",
)

# ---- model not found -----------------------------------------------------
#
# A deployment/model/route that does not exist is permanent: the identical
# request will fail identically forever. Separating it from provider_error
# matters because provider_error is retryable — an unrouted model otherwise
# burns the full retry budget (LLM facade, then planner, each with backoff)
# and surfaces ~45s later as a silent stall instead of an immediate,
# actionable "that model isn't there".
_MODEL_NOT_FOUND_MARKERS = (
    "deploymentnotfound",
    "the api deployment for this resource does not exist",
    "api_not_supported",
    "requested api is currently not supported",
    "model_not_found",
    "does not exist or you do not have access to it",
    "modelnotfound",
    "could not find model",
    "unknown model",
    "invalid model",
    "resourcenotfoundexception",
)

# ---- quota vs rate limit -------------------------------------------------
#
# Both classes usually arrive as a 429, but they behave nothing alike: a rate
# limit heals in seconds (retry it), an exhausted quota / empty credit balance
# does not heal at all within a run (don't retry — switch providers or fail).
# Providers don't agree on a machine-readable marker, so we match on the
# message and keep the two apart with the transient markers below.

# Text that means "the account/project is out of budget or allowance".
_QUOTA_MARKERS = (
    "insufficient_quota",
    "insufficient quota",
    "exceeded your current quota",
    "exceeded your assigned quota",
    "quota exceeded",
    "exceeded the service quota",
    "servicequotaexceededexception",
    "resource_exhausted",
    "resource has been exhausted",
    "credit balance is too low",
    "insufficient credits",
    "insufficient balance",
    "out of credits",
    "check your plan and billing",
    # Billing phrasings are kept specific on purpose: a bare "billing" would
    # also match a provider echoing the user's prompt back in a 400 body.
    "enable billing",
    "billing to be enabled",
    "billing is not enabled",
    "billing account for project",
)

# Text that means "too fast right now" — wins over the quota markers above,
# because Google reuses RESOURCE_EXHAUSTED / "Quota exceeded for quota metric"
# for plain per-minute throttling, which is transient and worth retrying.
_TRANSIENT_LIMIT_MARKERS = (
    "rate limit",
    "rate_limit",
    "per minute",
    "per-minute",
    "per min",
    "requests per",
    "tokens per",
    "try again in",
    "retry after",
    "retry_after",
    "slow down",
)

# Our *own* usage caps (app.services.usage_policy_service.UsageLimitExceeded)
# say "quota exceeded" too. They must never read as a provider quota error:
# the provider is fine, the org spent its budget, and substituting another
# model would only spend someone else's. They stay 'unknown' → no retry, no
# fallback, message surfaced verbatim.
_INTERNAL_QUOTA_MARKERS = (
    "monthly llm token quota",
    "monthly llm spend quota",
    "monthly usage quota",
)

# AWS surfaces everything as botocore ClientError: no HTTP status on the
# exception (it lives in a dict) and none in ``str(exc)`` either, which reads
# "An error occurred (ThrottlingException) when calling the Converse
# operation: ...". Map the error names we care about directly.
_AWS_ERROR_CODES = (
    ("servicequotaexceededexception", "quota"),
    ("throttlingexception", "rate_limit"),
    ("toomanyrequestsexception", "rate_limit"),
    ("requestthrottled", "rate_limit"),
    ("modelnotreadyexception", "provider_error"),
    ("modeltimeoutexception", "provider_error"),
    ("serviceunavailableexception", "provider_error"),
    ("internalserverexception", "provider_error"),
)

_AWS_SUMMARIES = {
    "quota": "{provider} quota or credit balance exhausted",
    "rate_limit": "{provider} is rate-limiting requests",
    "provider_error": "{provider} could not serve the request",
}


@dataclass
class LLMError:
    """Structured LLM error suitable for SSE emission and DB storage.

    Fields are designed so we never lose information:
      - ``code`` lets the frontend route + localize the toast.
      - ``summary`` is a short friendly headline for known codes.
      - ``provider_message`` is the actual error text from the provider,
        cleaned of secrets but NOT abstracted. ALWAYS shown to the user.
      - ``raw_tail`` is the last ~400 chars of the original exception for
        the "Show technical details" expandable.
    """

    code: str
    provider: str
    model: Optional[str] = None
    status: Optional[int] = None
    summary: str = ""           # friendly headline for known codes (e.g. "API key invalid")
    provider_message: str = ""  # actual provider error text — verbatim, never abstracted away
    request_id: Optional[str] = None
    raw_tail: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def classify(
    exc: BaseException,
    *,
    provider: str = "unknown",
    model: Optional[str] = None,
) -> LLMError:
    """Best-effort classification of a provider exception.

    Always extracts a usable ``provider_message`` and ``raw_tail`` so the
    user sees something real even when ``code == 'unknown'``.
    """
    raw = str(exc)
    raw_tail = raw[-400:] if len(raw) > 400 else raw

    status = _extract_status(exc, raw)
    cls_name = type(exc).__name__.lower()
    provider_message = _extract_provider_message(exc, raw) or raw_tail
    request_id = _extract_request_id(exc, raw)

    low = raw.lower()
    pmsg_low = provider_message.lower()

    # Auth failures
    if status == 401 or "authenticationerror" in cls_name or "invalid x-api-key" in pmsg_low or "invalid api key" in pmsg_low or "incorrect api key" in pmsg_low:
        return LLMError(
            code="auth",
            provider=provider,
            model=model,
            status=status,
            summary="LLM API key invalid",
            provider_message=provider_message,
            request_id=request_id,
            raw_tail=raw_tail,
        )

    # Context length. Ahead of the quota and rate-limit branches: an
    # over-long prompt is unambiguous, and checking it first means a provider
    # that happens to echo prompt text into the error body can't have that text
    # matched as a billing marker.
    # Marker coverage is per-provider wording:
    #   - "prompt is too long"  — Anthropic ("prompt is too long: 250000
    #     tokens > 200000 maximum"), also passed through by Bedrock Claude
    #   - "input is too long"   — Bedrock's own wording for non-Claude /
    #     legacy models ("Input is too long for requested model.")
    #   - "input token count"   — Gemini ("The input token count (N) exceeds
    #     the maximum number of tokens allowed (M)")
    # Without these, real overflows fell through to provider_error, which is
    # façade-retried and fallback-eligible: both wrong for a deterministic 400.
    if status == 400 and any(t in pmsg_low for t in ("maximum context", "context length", "too many tokens", "context_length_exceeded", "prompt is too long", "input is too long", "input token count")):
        return LLMError(
            code="context_length",
            provider=provider,
            model=model,
            status=status,
            summary="Conversation too long for this model",
            provider_message=provider_message,
            request_id=request_id,
            raw_tail=raw_tail,
        )

    # Provider quota / credit exhaustion. Checked before rate_limit because
    # most providers report it as a 429 (OpenAI ``insufficient_quota``, Google
    # RESOURCE_EXHAUSTED); 402 is Payment Required by definition. Our own usage
    # caps are excluded — see _INTERNAL_QUOTA_MARKERS.
    haystack = f"{pmsg_low}\n{low}"
    if not any(t in haystack for t in _INTERNAL_QUOTA_MARKERS):
        quota_hit = status == 402 or any(t in haystack for t in _QUOTA_MARKERS)
        if quota_hit and not any(t in haystack for t in _TRANSIENT_LIMIT_MARKERS):
            return LLMError(
                code="quota",
                provider=provider,
                model=model,
                status=status,
                summary=f"{provider} quota or credit balance exhausted",
                provider_message=provider_message,
                request_id=request_id,
                raw_tail=raw_tail,
            )

    # AWS error names — botocore carries no usable status, so name-match first
    # (a ThrottlingException must not fall through to 'unknown').
    for marker, aws_code in _AWS_ERROR_CODES:
        if marker in low:
            return LLMError(
                code=aws_code,
                provider=provider,
                model=model,
                status=status,
                summary=_AWS_SUMMARIES[aws_code].format(provider=provider),
                provider_message=provider_message,
                request_id=request_id,
                raw_tail=raw_tail,
            )

    # Rate limit
    if status == 429 or "ratelimit" in cls_name or "rate limit" in pmsg_low:
        return LLMError(
            code="rate_limit",
            provider=provider,
            model=model,
            status=status,
            summary=f"{provider} is rate-limiting requests",
            provider_message=provider_message,
            request_id=request_id,
            raw_tail=raw_tail,
        )

    # Network / connection
    if any(t in cls_name for t in ("connect", "timeout")) or any(
        t in low for t in ("connection refused", "name or service not known", "timed out", "tls handshake")
    ):
        return LLMError(
            code="network",
            provider=provider,
            model=model,
            status=None,
            summary=f"Could not reach {provider}",
            provider_message=provider_message,
            request_id=request_id,
            raw_tail=raw_tail,
        )

    # Model / deployment / route does not exist. Checked before the generic
    # 4xx branch: both are 404s, but only this one is permanent, and the two
    # differ in whether a retry is worth anything.
    if any(t in pmsg_low or t in low for t in _MODEL_NOT_FOUND_MARKERS) or (
        status == 404 and "notfound" in cls_name
    ):
        return LLMError(
            code="model_not_found",
            provider=provider,
            model=model,
            status=status,
            summary=(
                f"{provider} has no model '{model}' at this endpoint"
                if model else f"{provider} could not find the requested model"
            ),
            provider_message=provider_message,
            request_id=request_id,
            raw_tail=raw_tail,
        )

    # Provider 5xx and other 4xx — known to be a provider-side issue
    if status and 400 <= status < 600:
        return LLMError(
            code="provider_error",
            provider=provider,
            model=model,
            status=status,
            summary=f"{provider} returned an error ({status})",
            provider_message=provider_message,
            request_id=request_id,
            raw_tail=raw_tail,
        )

    # Truly unknown — surface the raw provider message verbatim. Don't pretend.
    return LLMError(
        code="unknown",
        provider=provider,
        model=model,
        status=status,
        summary=f"{provider} call failed",
        provider_message=provider_message,
        request_id=request_id,
        raw_tail=raw_tail,
    )


# ---- internals -----------------------------------------------------

def _extract_status(exc: BaseException, raw: str) -> Optional[int]:
    """Pull HTTP status off common exception shapes."""
    s = getattr(exc, "status_code", None) or getattr(exc, "status", None)
    if isinstance(s, int):
        return s
    # google.genai APIError keeps the int status on ``.code`` — ``.status`` is
    # a string enum ('RESOURCE_EXHAUSTED'), so the check above skips it.
    c = getattr(exc, "code", None)
    if isinstance(c, int) and 100 <= c < 600:
        return c
    resp = getattr(exc, "response", None)
    if resp is not None:
        s2 = getattr(resp, "status_code", None) or getattr(resp, "status", None)
        if isinstance(s2, int):
            return s2
        # botocore ClientError.response is a dict, not an object.
        if isinstance(resp, dict):
            meta = resp.get("ResponseMetadata")
            s3 = meta.get("HTTPStatusCode") if isinstance(meta, dict) else None
            if isinstance(s3, int):
                return s3
    m = re.search(r"\b(?:Error code:|status[_ ]code[=:]|HTTP/\d\.\d)\s*(\d{3})\b", raw)
    if m:
        try:
            return int(m.group(1))
        except Exception:
            pass
    # Stringified google.genai error: "429 RESOURCE_EXHAUSTED. {...}". The
    # trailing name must be 4+ chars so token counts ("... 429 TPM") don't
    # masquerade as a status.
    m = re.search(r"(?<!\d)(\d{3})(?!\d)\s+[A-Z][A-Z_]{3,}\b", raw)
    if m:
        try:
            code = int(m.group(1))
            if 100 <= code < 600:
                return code
        except Exception:
            pass
    # Stringified botocore ClientError: "An error occurred (ValidationException)
    # when calling the Converse operation: ...". Carries no numeric status at
    # all once str()-ed, so map the exception name — only names with an
    # unambiguous HTTP meaning. (Throttling/quota names are already handled by
    # the _AWS_ERROR_CODES name match, which classifies without needing a
    # status; ValidationException must resolve to 400 so the context_length
    # branch — which requires status == 400 — can see Bedrock overflows.)
    m = re.search(r"an error occurred \((\w+)\)", raw, re.IGNORECASE)
    if m and m.group(1).lower() == "validationexception":
        return 400
    return None


_PROVIDER_BODY_PATTERNS = (
    # SDK python-repr bodies: 'message': '...' or 'message': "...". The
    # value's quote style depends on its content — repr() puts apostrophe-
    # containing text in double quotes ("This model's maximum context…") —
    # so match the opening quote and scan to ITS closing twin. A naive
    # [^'"]+ truncated at the first apostrophe ("This model"), and the
    # truncated text never matched downstream markers (context_length etc.).
    re.compile(r"'message'\s*:\s*(?P<q>['\"])(?P<msg>.+?)(?P=q)"),
    # Real JSON bodies: "message": "..."
    re.compile(r'"message"\s*:\s*"(?P<msg>[^"]+)"'),
    # Generic: extract everything after "Error code: NNN -"
    re.compile(r"Error code:\s*\d+\s*-\s*(?P<msg>.+)$", re.DOTALL),
)


def _extract_provider_message(exc: BaseException, raw: str) -> str:
    """Pull the provider's actual error text out of common SDK exception
    string formats. Falls back to the full ``str(exc)`` if no pattern
    matches. Cleaned of obvious noise but NOT genericized.
    """
    # Try common provider patterns to grab just the human-readable bit.
    for pat in _PROVIDER_BODY_PATTERNS:
        m = pat.search(raw)
        if m:
            text = m.group("msg").strip()
            # Trim common prefixes that look noisy
            text = text.strip("{}").strip()
            if text and len(text) < 500:
                return text
    # Best-effort: use response body if SDK exposes it
    resp = getattr(exc, "response", None)
    if resp is not None:
        body = getattr(resp, "text", None)
        if isinstance(body, str) and body:
            return body[:500]
    # Default: the exception text itself, truncated
    return raw[:500]


def _extract_request_id(exc: BaseException, raw: str) -> Optional[str]:
    """Anthropic and OpenAI both expose a request_id useful for support
    tickets. Pull it from the exception's response or the message body.
    """
    rid = getattr(exc, "request_id", None)
    if isinstance(rid, str):
        return rid
    resp = getattr(exc, "response", None)
    if resp is not None:
        h = getattr(resp, "headers", None)
        try:
            if h:
                v = h.get("request-id") or h.get("x-request-id")
                if v:
                    return v
        except Exception:
            pass
    m = re.search(r"['\"]?request[_-]id['\"]?\s*[:=]\s*['\"]([A-Za-z0-9_\-]+)['\"]", raw)
    if m:
        return m.group(1)
    return None
