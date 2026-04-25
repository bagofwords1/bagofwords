from __future__ import annotations

import functools
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)


_DEFAULT_ENCODING = "cl100k_base"
_USE_TIKTOKEN = os.getenv("BOW_USE_TIKTOKEN", "0") == "1"


@functools.lru_cache(maxsize=16)
def _get_encoding(model_name: Optional[str]):
    try:
        import tiktoken  # lazy — only imported when tiktoken is enabled
    except Exception as exc:
        logger.warning("tiktoken not available: %s", exc)
        return None
    try:
        if model_name and hasattr(tiktoken, "encoding_for_model"):
            return tiktoken.encoding_for_model(model_name)
    except Exception:
        pass
    try:
        return tiktoken.get_encoding(_DEFAULT_ENCODING)
    except Exception:
        return None


def estimate_tokens_fast(text: str) -> int:
    """Cheap token estimate for latency-sensitive paths.

    Avoids tokenizer work entirely. Providers that return usage can still
    overwrite the estimate after the response completes.
    """
    if not text:
        return 0
    return max(1, (len(text) + 3) // 4)


def count_tokens(text: str, model_name: Optional[str] = None) -> int:
    """Count tokens. Defaults to a fast char/4 estimate.

    Set BOW_USE_TIKTOKEN=1 to opt back into tiktoken-based counting.
    """
    if not text:
        return 0
    if not _USE_TIKTOKEN:
        return estimate_tokens_fast(text)
    enc = _get_encoding(model_name)
    if enc is None:
        return estimate_tokens_fast(text)
    try:
        return len(enc.encode(text))
    except Exception as e:
        logger.warning("tiktoken encode failed, using fast estimate: %s", e)
        return estimate_tokens_fast(text)
