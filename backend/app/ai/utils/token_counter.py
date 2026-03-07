from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)

try:
    import tiktoken
    logger.info("tiktoken loaded successfully")
except Exception as _exc:  # pragma: no cover
    tiktoken = None  # type: ignore
    logger.warning("tiktoken is not available, falling back to word-split token counting: %s", _exc)


_DEFAULT_ENCODING = "cl100k_base"


def _get_encoding(model_name: Optional[str]):
    if tiktoken is None:
        return None
    try:
        if model_name and hasattr(tiktoken, "encoding_for_model"):
            enc = tiktoken.encoding_for_model(model_name)
            logger.debug("tiktoken encoding loaded for model=%s", model_name)
            return enc
    except Exception as e:
        logger.debug("tiktoken encoding_for_model failed for model=%s: %s, falling back to %s", model_name, e, _DEFAULT_ENCODING)
    try:
        enc = tiktoken.get_encoding(_DEFAULT_ENCODING)
        logger.debug("tiktoken using fallback encoding=%s", _DEFAULT_ENCODING)
        return enc
    except Exception as e:
        logger.warning("tiktoken failed to load fallback encoding=%s: %s", _DEFAULT_ENCODING, e)
        return None


def count_tokens(text: str, model_name: Optional[str] = None) -> int:
    """Count approximate tokens using tiktoken when available; fallback to words.

    Args:
        text: input string
        model_name: optional model identifier for a better encoding match
    """
    if not text:
        return 0
    enc = _get_encoding(model_name)
    if enc is None:
        logger.debug("tiktoken unavailable, using word-split fallback for token count")
        return max(1, len(text.split()))
    try:
        return len(enc.encode(text))
    except Exception as e:
        logger.warning("tiktoken encode failed, using word-split fallback: %s", e)
        return max(1, len(text.split()))

