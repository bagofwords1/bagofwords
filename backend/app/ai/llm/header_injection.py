"""Custom HTTP headers + identity forwarding for LLM provider requests.

Admins can configure two things on an LLM provider (stored in
``LLMProvider.additional_config``, mirroring MCP connections):

  * ``headers`` — static key/value headers attached to every request the
    provider's clients make (e.g. a gateway routing key or cost-center tag).
  * ``header_injection`` — rules that forward the signed-in user's identity as
    headers, e.g. ``{"header": "X-User-Email", "source": "user.email"}``, so a
    gateway/proxy can attribute cost per user.

Sources use the same whitelisted grammar as MCP context forwarding
(:mod:`app.services.mcp_context_injection`): ``user.email`` / ``user.name`` /
``user.id`` / ``membership.role`` / ``membership.attr:<key>`` /
``static:<text>`` with ``{atom}`` interpolation.

The user's identity is ambient: LLM calls happen deep in the agent/tool stack,
so AgentV2 stamps an :class:`IdentityContext` contextvar at run start (next to
usage attribution) and the LLM facade resolves the provider's rules against it
when it constructs a client. Outside a user-triggered run (test connection,
scheduled jobs) the identity is empty, so injection rules resolve to nothing
and only static headers apply.
"""

from __future__ import annotations

import contextlib
import re
from contextvars import ContextVar
from typing import Any, Dict, List, Optional

from app.services.mcp_context_injection import IdentityContext, resolve_source

# RFC 7230 token characters — the only thing a header NAME may contain.
_HEADER_NAME_RE = re.compile(r"^[A-Za-z0-9!#$%&'*+.^_`|~-]+$")

MAX_HEADERS = 24
MAX_HEADER_NAME_LEN = 128
MAX_HEADER_VALUE_LEN = 4096

_current_identity: ContextVar[Optional[IdentityContext]] = ContextVar(
    "llm_identity", default=None
)


def get_llm_identity() -> Optional[IdentityContext]:
    return _current_identity.get()


def set_llm_identity(identity: Optional[IdentityContext]):
    """Set the ambient identity, returning the contextvar token for reset."""
    return _current_identity.set(identity)


def reset_llm_identity(token) -> None:
    with contextlib.suppress(Exception):
        _current_identity.reset(token)


def _clean_header_value(value: Any) -> Optional[str]:
    """Coerce to a single-line header value, or None when unusable.

    CR/LF are stripped rather than rejected — they can only arrive through a
    resolved identity attribute, and dropping the newline beats failing the
    whole LLM call over a malformed directory field.
    """
    if value is None:
        return None
    text = str(value).replace("\r", " ").replace("\n", " ").strip()
    if not text:
        return None
    return text[:MAX_HEADER_VALUE_LEN]


def validate_header_config(
    headers: Optional[Dict[str, Any]],
    header_injection: Optional[List[Dict[str, Any]]],
) -> tuple[Dict[str, str], List[Dict[str, str]]]:
    """Validate + normalize admin-supplied header config for storage.

    Returns ``(headers, header_injection)`` normalized; raises ``ValueError``
    on an invalid header name or an oversized rule set so the API surfaces a
    clear 400 instead of persisting config that would be dropped at call time.
    """
    clean_headers: Dict[str, str] = {}
    for name, value in (headers or {}).items():
        name = str(name or "").strip()
        if not name:
            continue
        if len(name) > MAX_HEADER_NAME_LEN or not _HEADER_NAME_RE.match(name):
            raise ValueError(f"Invalid header name: {name!r}")
        cleaned = _clean_header_value(value)
        if cleaned is None:
            continue
        clean_headers[name] = cleaned

    clean_rules: List[Dict[str, str]] = []
    for rule in header_injection or []:
        if not isinstance(rule, dict):
            continue
        name = str(rule.get("header") or "").strip()
        source = str(rule.get("source") or "").strip()
        if not name and not source:
            continue
        if not name or len(name) > MAX_HEADER_NAME_LEN or not _HEADER_NAME_RE.match(name):
            raise ValueError(f"Invalid header name in forwarding rule: {name!r}")
        if not source:
            raise ValueError(f"Forwarding rule for {name!r} has no source")
        clean_rules.append({"header": name, "source": source})

    if len(clean_headers) + len(clean_rules) > MAX_HEADERS:
        raise ValueError(f"Too many custom headers (max {MAX_HEADERS})")

    return clean_headers, clean_rules


def build_provider_headers(additional_config: Optional[Dict[str, Any]]) -> Dict[str, str]:
    """Resolve a provider's header config into the headers to send.

    Static ``headers`` first, then ``header_injection`` rules against the
    ambient identity (dynamic wins). Rules that resolve empty are omitted —
    an empty identity header is noise, not context. Invalid names are dropped
    defensively (storage already validates; config may predate validation).
    """
    config = additional_config or {}
    resolved: Dict[str, str] = {}

    static = config.get("headers") or {}
    if isinstance(static, dict):
        for name, value in static.items():
            name = str(name or "").strip()
            cleaned = _clean_header_value(value)
            if name and cleaned is not None and _HEADER_NAME_RE.match(name):
                resolved[name] = cleaned

    rules = config.get("header_injection") or []
    if isinstance(rules, list):
        identity = get_llm_identity() or IdentityContext()
        for rule in rules:
            if not isinstance(rule, dict):
                continue
            name = str(rule.get("header") or "").strip()
            if not name or not _HEADER_NAME_RE.match(name):
                continue
            value = _clean_header_value(resolve_source(rule.get("source", ""), identity))
            if value:
                resolved[name] = value

    return resolved
