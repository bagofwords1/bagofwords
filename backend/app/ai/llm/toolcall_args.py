"""Tolerant parsing for streamed tool-call argument JSON.

Providers stream tool-call arguments as raw text fragments that the model is
trusted to emit as valid JSON. Models slip in predictable ways:

- prose wrapped around the object (``functions.write_csv: {...}``, trailing
  backticks);
- Python-style literals (single-quoted strings) instead of JSON;
- unescaped double quotes inside string values — endemic for Hebrew/Arabic
  content, where the ASCII ``"`` appears INSIDE words as an abbreviation mark
  (e.g. ארה"ב, מנכ"ל), so any text-heavy argument is full of them;
- raw control characters (newlines/tabs) inside string values.

Dropping the whole call over one of these wastes an agent round-trip on a
misleading "field required" error. This module runs a conservative salvage
ladder instead; every rung must produce a dict that round-trips ``json.loads``
semantics, and anything ambiguous falls through to the explicit
``_unparsable`` marker so the runner can hand the model accurate feedback.
"""
from __future__ import annotations

import ast
import json
import logging
import re
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

# Keys of the failure marker returned when every rung fails. The runner keys
# off UNPARSABLE_KEY to produce a JSON-syntax error message instead of a
# missing-field one.
UNPARSABLE_KEY = "_unparsable"
RAW_KEY = "_raw"
ERROR_KEY = "_error"

# A leading wrapper is only stripped when it is short and looks like tool-call
# prose (tool name, colon, markdown fence) — not when it looks like lost data.
_MAX_LEADING_CHARS = 96
_LEADING_ALLOWED_RE = re.compile(r"^[\w\s\"'`.:/\\-]+$")
_MAX_TRAILING_CHARS = 3

_STRUCTURAL_AFTER_CLOSE = {",", "}", "]", ":"}

_CONTROL_ESCAPES = {"\n": "\\n", "\r": "\\r", "\t": "\\t"}


def _as_dict(value: Any) -> Optional[Dict[str, Any]]:
    if isinstance(value, dict) and all(isinstance(k, str) for k in value.keys()):
        return value
    return None


def _try_json(text: str) -> Optional[Dict[str, Any]]:
    try:
        return _as_dict(json.loads(text))
    except Exception:
        return None


def _try_python_literal(text: str) -> Optional[Dict[str, Any]]:
    """Parse Python-literal syntax (accepts single-quoted strings).

    ``ast.literal_eval`` evaluates literals only — no names, calls, or
    operators — so this is safe on model output.
    """
    try:
        return _as_dict(ast.literal_eval(text))
    except Exception:
        return None


def _extract_balanced_object(raw: str) -> Optional[str]:
    """Return the first balanced ``{...}`` span, honoring strings and escapes.

    Only accepts the span when whatever surrounds it is small, plausible
    wrapper junk — a large stripped prefix would mean discarding real data.
    """
    start = raw.find("{")
    if start < 0:
        return None

    prefix = raw[:start].strip()
    if prefix and (
        len(prefix) > _MAX_LEADING_CHARS or not _LEADING_ALLOWED_RE.match(prefix)
    ):
        return None

    depth = 0
    in_string = False
    escaped = False
    for i in range(start, len(raw)):
        ch = raw[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                suffix = raw[i + 1 :].strip()
                if len(suffix) > _MAX_TRAILING_CHARS:
                    return None
                return raw[start : i + 1]
    return None


def _looks_like_key_start(text: str, index: int) -> bool:
    """True when ``text[index:]`` begins a ``"key":`` member (short key)."""
    if index >= len(text) or text[index] not in "\"'":
        return False
    quote = text[index]
    end = text.find(quote, index + 1)
    if end < 0 or end - index > 100:
        return False
    rest = text[end + 1 :].lstrip()
    return rest.startswith(":")


def _skip_ws(text: str, index: int) -> int:
    while index < len(text) and text[index].isspace():
        index += 1
    return index


def _repair_string_damage(raw: str) -> Optional[str]:
    """Re-serialize ``raw`` fixing in-string damage: bare ``"`` and control chars.

    Walks the text with JSON structure awareness. Inside a string, an
    unescaped ``"`` only closes the string when what follows reads as JSON
    structure (``:`` for a key; ``}``/``]``/end for a value; ``,`` only when
    followed by another ``"key":``). Any other ``"`` is treated as an embedded
    literal quote and escaped — the Hebrew abbreviation case. Raw control
    characters inside strings are escaped as well.

    Returns the repaired text, or None when the input has no string damage
    this pass can address.
    """
    out: list[str] = []
    in_string = False
    escaped = False
    changed = False

    for i, ch in enumerate(raw):
        if not in_string:
            if ch == '"':
                in_string = True
            out.append(ch)
            continue

        if escaped:
            out.append(ch)
            escaped = False
            continue
        if ch == "\\":
            out.append(ch)
            escaped = True
            continue
        if ch in _CONTROL_ESCAPES:
            out.append(_CONTROL_ESCAPES[ch])
            changed = True
            continue
        if ch != '"':
            out.append(ch)
            continue

        # Candidate closing quote: close only when what follows is structure.
        nxt = _skip_ws(raw, i + 1)
        follower = raw[nxt] if nxt < len(raw) else ""
        if follower in (":", "}", "]") or nxt >= len(raw):
            in_string = False
            out.append(ch)
        elif follower == "," and _looks_like_key_start(raw, _skip_ws(raw, nxt + 1)):
            in_string = False
            out.append(ch)
        else:
            out.append('\\"')
            changed = True

    return "".join(out) if changed else None


def parse_tool_call_arguments(raw: str, tool_name: str = "") -> Dict[str, Any]:
    """Parse a streamed tool-call argument buffer into a dict.

    Runs the salvage ladder; on total failure returns the ``_unparsable``
    marker dict carrying the raw buffer and the original JSON error, which the
    tool runner turns into accurate model feedback.
    """
    text = (raw or "").strip()
    if not text:
        return {}

    try:
        loaded = json.loads(text)
        as_dict = _as_dict(loaded)
        if as_dict is not None:
            return as_dict
        json_error = f"arguments are valid JSON but not an object (got {type(loaded).__name__})"
    except Exception as e:
        json_error = str(e)

    salvaged, rung = _salvage(text)
    if salvaged is not None:
        logger.warning(
            "tool-call arguments for %r salvaged via %s (raw length %d)",
            tool_name or "<unknown>", rung, len(text),
        )
        return salvaged

    return {UNPARSABLE_KEY: True, RAW_KEY: raw, ERROR_KEY: json_error}


def _salvage(text: str) -> Tuple[Optional[Dict[str, Any]], str]:
    extracted = _extract_balanced_object(text)
    if extracted is not None and extracted != text:
        parsed = _try_json(extracted)
        if parsed is not None:
            return parsed, "wrapper_strip"

    parsed = _try_python_literal(text)
    if parsed is not None:
        return parsed, "python_literal"

    for candidate, rung in ((text, "string_repair"), (extracted, "wrapper_strip+string_repair")):
        if candidate is None:
            continue
        repaired = _repair_string_damage(candidate)
        if repaired is not None:
            parsed = _try_json(repaired)
            if parsed is not None:
                return parsed, rung

    return None, ""
