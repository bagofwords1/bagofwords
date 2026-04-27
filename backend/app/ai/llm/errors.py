"""Centralized LLM error classification.

Turns provider-raised exceptions into a structured, UI-renderable shape
that the agent loop and side-paths (scoring, title generation, etc.) can
emit through SSE so the user sees real failure reasons instead of silent
'completion finished successfully' with empty blocks.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Optional


# Stable codes the frontend keys on for localized messages and toast variants.
# Add new values cautiously — every code needs a matching i18n key.
ERROR_CODES = (
    "auth",            # 401 / invalid credentials
    "rate_limit",      # 429
    "context_length",  # 400 with "maximum context" / "tokens" indicator
    "provider_error",  # 5xx or other 4xx not covered above
    "network",         # connection refused, DNS, timeout
    "unknown",
)


@dataclass
class LLMError:
    """Structured LLM error suitable for SSE emission and DB storage."""

    code: str            # one of ERROR_CODES
    provider: str        # 'anthropic' | 'openai' | 'azure' | 'google' | 'bedrock' | 'custom'
    model: Optional[str] = None
    status: Optional[int] = None  # HTTP status when applicable
    message: str = ""    # short human-facing summary (no secrets)
    raw: str = ""        # tail of original exception string, for debug

    def to_dict(self) -> dict:
        return asdict(self)


def classify(
    exc: BaseException,
    *,
    provider: str = "unknown",
    model: Optional[str] = None,
) -> LLMError:
    """Best-effort classification of a provider exception.

    Inspects HTTP status, exception class name, and the message body.
    Avoids leaking the API key in ``message`` (anthropic 401 bodies don't
    include it, but ``raw`` is tail-truncated regardless).
    """
    raw = str(exc)
    raw_tail = raw[-400:] if len(raw) > 400 else raw

    status = _extract_status(exc, raw)
    cls_name = type(exc).__name__.lower()

    # Auth failures
    if status == 401 or "authenticationerror" in cls_name or "invalid x-api-key" in raw.lower() or "invalid api key" in raw.lower():
        return LLMError(
            code="auth",
            provider=provider,
            model=model,
            status=status,
            message=f"{provider} rejected the API key. Update credentials in Settings → LLM Providers.",
            raw=raw_tail,
        )

    # Rate limit
    if status == 429 or "ratelimit" in cls_name or "rate limit" in raw.lower():
        return LLMError(
            code="rate_limit",
            provider=provider,
            model=model,
            status=status,
            message=f"{provider} is rate-limiting requests. Will retry shortly.",
            raw=raw_tail,
        )

    # Context length
    low = raw.lower()
    if status == 400 and ("maximum context" in low or "context length" in low or "too many tokens" in low or "max_tokens" in low):
        return LLMError(
            code="context_length",
            provider=provider,
            model=model,
            status=status,
            message="The conversation is too long for this model. Start a new report or pick a model with a larger context window.",
            raw=raw_tail,
        )

    # Provider 5xx and other 4xx
    if status and 400 <= status < 600:
        return LLMError(
            code="provider_error",
            provider=provider,
            model=model,
            status=status,
            message=f"{provider} returned an error ({status}). Will retry.",
            raw=raw_tail,
        )

    # Network / connection
    if any(t in cls_name for t in ("connect", "timeout", "network")) or any(
        t in low for t in ("connection refused", "name or service not known", "timed out", "tls")
    ):
        return LLMError(
            code="network",
            provider=provider,
            model=model,
            status=None,
            message=f"Could not reach {provider}. Check network / proxy settings.",
            raw=raw_tail,
        )

    return LLMError(
        code="unknown",
        provider=provider,
        model=model,
        status=status,
        message=f"{provider} call failed: {type(exc).__name__}",
        raw=raw_tail,
    )


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
    # Fall back to scanning the message ("Error code: 401 - ...")
    import re
    m = re.search(r"\b(?:Error code:|status[_ ]code[=:]|HTTP/\d\.\d\s)(\d{3})\b", raw)
    if m:
        try:
            return int(m.group(1))
        except Exception:
            pass
    return None
