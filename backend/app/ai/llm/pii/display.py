"""Display-time PII redaction for content served to the frontend.

PII is redacted at two boundaries: outbound to the LLM (the chokepoint in
``llm.py``) and outbound to the UI (here). Stored data is never mutated — only
the *serialized view* the frontend receives is masked, so compute / load_step /
exports keep operating on the real values while every rendered component
(chat text, table/widget cells) shows the redacted form.

A request-scoped :class:`ContextVar` holds the org's redactor so the shared
*sync* serializers (e.g. ``serialize_block_v2_sync``) can redact without
threading a redactor through every call site. Async entry points wrap their
serialization in :func:`display_redaction`.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import Any, Optional

from .loader import load_redactor_for_org
from .redactor import PiiRedactor

logger = logging.getLogger(__name__)

_display_redactor: ContextVar[Optional[PiiRedactor]] = ContextVar(
    "_pii_display_redactor", default=None
)


@asynccontextmanager
async def display_redaction(organization_id: Optional[str], session_maker):
    """Load the org's redactor (enterprise-gated + cached) and expose it to the
    sync serializers for the duration of the block. No-op when unlicensed /
    disabled — the redactor resolves to None and every helper passes text
    through unchanged."""
    redactor = None
    try:
        redactor = await load_redactor_for_org(organization_id, session_maker)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("display redactor load failed: %s", exc)
    token = _display_redactor.set(redactor)
    try:
        yield redactor
    finally:
        _display_redactor.reset(token)


def current_display_redactor() -> Optional[PiiRedactor]:
    return _display_redactor.get()


def redact_text_display(text: Any) -> Any:
    """Redact a string for display. Non-strings and the no-redactor case pass
    through untouched."""
    redactor = _display_redactor.get()
    if redactor is None or not isinstance(text, str) or not text:
        return text
    return redactor.redact_display(text)


def redact_prompt_display(prompt: Any) -> Any:
    """Redact the ``content`` of a completion prompt/answer dict
    (``{"content": "...", ...}``) without disturbing the other keys."""
    redactor = _display_redactor.get()
    if redactor is None or not isinstance(prompt, dict):
        return prompt
    content = prompt.get("content")
    if isinstance(content, str) and content:
        return {**prompt, "content": redactor.redact_display(content)}
    return prompt


def redact_grid_display(data: Any) -> Any:
    """Deep-redact a widget-format grid ``{columns, rows, info, ...}`` — masks
    row cells AND the ``info``/``stats`` column summaries (e.g. top values)."""
    redactor = _display_redactor.get()
    if redactor is None:
        return data
    return redactor.redact_deep(data)


def redact_deep_display(obj: Any) -> Any:
    """Deep-redact an arbitrary nested payload (e.g. a tool observation
    ``result_json`` carrying raw rows / previews / stats)."""
    redactor = _display_redactor.get()
    if redactor is None:
        return obj
    return redactor.redact_deep(obj)


async def load_and_redact_grid(data: Any, organization_id: Optional[str], session_maker) -> Any:
    """Standalone helper for endpoints outside the contextvar scope (full
    step/widget data fetches): load the org redactor and redact a grid dict.
    Returns ``data`` unchanged when unlicensed / disabled."""
    try:
        redactor = await load_redactor_for_org(organization_id, session_maker)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("display redactor load failed: %s", exc)
        return data
    if redactor is None:
        return data
    return redactor.redact_deep(data)
