"""The organization's "Allow LLM to see data" switch, and what an error's text
may show the model when it is off.

One reader for the switch, so every call site fails the same way: on without
settings, off when the settings exist but cannot be read (never "yes" by
accident).

Errors are closed by default. With data hidden, any error's text stops where
it starts naming a value, unless its type only ever names structure (a missing
column or table, a syntax error, a timeout). DB drivers quote cells in their
messages ("invalid input syntax for type integer: <cell>",
"Numeric value '<cell>' is not recognized"), and a new driver must not leak
just because nobody listed its error type.
"""
from __future__ import annotations

import re
from typing import Any, Optional

HIDDEN_VALUE = "<value hidden>"

# Error types whose message names structure, not a cell. Matched on the short
# type name: the exception's own, the sandbox's original type, or the driver
# error a SQLAlchemy wrapper names in its leading "(pkg.Type)".
_STRUCTURAL_ERROR_NAMES = frozenset({
    # Python: the generated code itself
    "SyntaxError", "IndentationError", "NameError", "AttributeError",
    "ImportError", "ModuleNotFoundError", "ZeroDivisionError", "NotImplementedError",
    # Postgres (psycopg) and DuckDB: missing objects, bad SQL, permissions
    "UndefinedColumn", "UndefinedTable", "UndefinedFunction", "UndefinedObject",
    "UndefinedParameter", "AmbiguousColumn", "InsufficientPrivilege",
    "BinderException", "CatalogException",
    # BOW's own
    "QueryTimeoutError",
})
_WRAPPED_TYPE = re.compile(r"^\s*\(([\w.]+)\)")


def allow_llm_see_data(organization_settings: Any) -> bool:
    """The org's "Allow LLM to see data" switch: on without settings, off when
    they cannot be read. Accepts the settings object or its config dict."""
    if organization_settings is None:
        return True
    try:
        return bool(organization_settings.get_config("allow_llm_see_data").value)
    except Exception:
        pass
    try:
        config = getattr(organization_settings, "config", organization_settings) or {}
        value = config.get("allow_llm_see_data", {})
        return bool(value.get("value", True)) if isinstance(value, dict) else bool(value)
    except Exception:
        return False


def _short_name(type_name: Any) -> str:
    return str(type_name or "").rsplit(".", 1)[-1]


def names_only_structure(type_name: Optional[str], text: str = "") -> bool:
    """Whether an error of this type (or the driver error its text says it
    wraps) only ever names structure."""
    wrapped = _WRAPPED_TYPE.match(text or "")
    if wrapped:
        return _short_name(wrapped.group(1)) in _STRUCTURAL_ERROR_NAMES
    return _short_name(type_name) in _STRUCTURAL_ERROR_NAMES


def cut_before_value(text: str) -> str:
    """``text`` up to where an error names a value: its first quote, ": " or
    " (" ("could not convert string to float: <cell>", "new category (<cell>)").
    The cell may hold anything (quotes, line breaks, a repr cut short), so
    nothing after that point is kept. A leading "(pkg.Type)" is kept."""
    wrapped = _WRAPPED_TYPE.match(text)
    head, rest = (text[:wrapped.end()], text[wrapped.end():]) if wrapped else ("", text)
    match = re.search(r"['\"]|: | \(", rest)
    if match is None:
        return text
    kept = (head + rest[:match.start()]).rstrip(" :")
    return f"{kept}: {HIDDEN_VALUE}" if kept else HIDDEN_VALUE


def redact_error_text(text: Any, type_name: Optional[str] = None) -> Any:
    """An error's text as the model may see it with data hidden: unchanged
    for a structural error type, cut before the value otherwise."""
    if not isinstance(text, str) or not text:
        return text
    if names_only_structure(type_name, text):
        return text
    return cut_before_value(text)
