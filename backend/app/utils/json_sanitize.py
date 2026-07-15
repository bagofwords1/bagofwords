"""Recursive UTF-8 sanitization for JSON-bound payloads.

Tool outputs can carry strings Python tolerates but JSON transport does not:
lone UTF-16 surrogates (U+D800–DFFF, e.g. from pypdf reading a PDF with a
broken ToUnicode CMap) blow up `json.dumps(...).encode("utf-8")` at response
time ("surrogates not allowed" → a permanent 500 on every load of the row),
and NUL bytes are rejected by Postgres JSONB. Sanitizing at the persistence
boundary (ToolExecutionService) keeps new rows clean; sanitizing again at the
serialization boundary (completion serializers) keeps rows written BEFORE the
fix loadable without a data migration.
"""
from __future__ import annotations

from typing import Any


def sanitize_utf8(text: str) -> str:
    """Replace lone surrogates with U+FFFD and strip NUL. No-op fast path for
    clean strings (the overwhelmingly common case)."""
    if not text:
        return text
    try:
        text.encode("utf-8")
        clean = text
    except UnicodeEncodeError:
        clean = text.encode("utf-8", errors="replace").decode("utf-8")
    return clean.replace("\x00", "") if "\x00" in clean else clean


def sanitize_json_strings(obj: Any) -> Any:
    """Recursively sanitize every string in a JSON-shaped structure
    (dict/list/str). Non-string scalars pass through. Returns the same object
    when nothing needed changing (dicts/lists are rebuilt only on demand)."""
    if isinstance(obj, str):
        return sanitize_utf8(obj)
    if isinstance(obj, dict):
        out = None
        for k, v in obj.items():
            sv = sanitize_json_strings(v)
            sk = sanitize_utf8(k) if isinstance(k, str) else k
            if sv is not v or sk is not k:
                if out is None:
                    out = dict(obj)
                if sk is not k:
                    out.pop(k, None)
                out[sk] = sv
        return out if out is not None else obj
    if isinstance(obj, list):
        out_list = None
        for i, v in enumerate(obj):
            sv = sanitize_json_strings(v)
            if sv is not v:
                if out_list is None:
                    out_list = list(obj)
                out_list[i] = sv
        return out_list if out_list is not None else obj
    return obj
