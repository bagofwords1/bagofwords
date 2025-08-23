from __future__ import annotations

from typing import Optional

try:
    import tiktoken
except Exception:  # pragma: no cover
    tiktoken = None  # type: ignore


_DEFAULT_ENCODING = "cl100k_base"


def _get_encoding(model_name: Optional[str]):
    if tiktoken is None:
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
        # simple fallback
        return max(1, len(text.split()))
    try:
        return len(enc.encode(text))
    except Exception:
        return max(1, len(text.split()))

