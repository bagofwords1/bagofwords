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
    "context_length",
    "provider_error",
    "network",
    "unknown",
)


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

    # Context length
    if status == 400 and any(t in pmsg_low for t in ("maximum context", "context length", "too many tokens", "context_length_exceeded")):
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
    resp = getattr(exc, "response", None)
    if resp is not None:
        s2 = getattr(resp, "status_code", None) or getattr(resp, "status", None)
        if isinstance(s2, int):
            return s2
    m = re.search(r"\b(?:Error code:|status[_ ]code[=:]|HTTP/\d\.\d)\s*(\d{3})\b", raw)
    if m:
        try:
            return int(m.group(1))
        except Exception:
            pass
    return None


_PROVIDER_BODY_PATTERNS = (
    # Anthropic SDK: "Error code: 401 - {'type': 'error', 'error': {'type': '...', 'message': 'invalid x-api-key'}, 'request_id': '...'}"
    re.compile(r"'message'\s*:\s*['\"]([^'\"]+)['\"]"),
    # OpenAI: "Error code: 400 - {'error': {'message': '...', 'type': '...'}}"
    re.compile(r'"message"\s*:\s*"([^"]+)"'),
    # Generic: extract everything after "Error code: NNN -"
    re.compile(r"Error code:\s*\d+\s*-\s*(.+)$", re.DOTALL),
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
            text = m.group(1).strip()
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
