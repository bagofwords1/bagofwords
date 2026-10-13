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
``static:<text>`` with ``{atom}`` interpolation. LLM headers also get
``agent.name``: the agents the current question has fetched data from (see
:class:`QuestionAgents`), or the conversation's chosen agents / ``auto``
before it has fetched any.

Header values must be ASCII on the wire (httpx encodes them as ASCII and
raises otherwise), so a value with non-ASCII characters — a Hebrew agent or
user name — is sent percent-encoded as UTF-8 instead of failing the LLM call.

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
import string
from contextvars import ContextVar
from typing import Any, Dict, List, Optional
from urllib.parse import quote

from app.services.mcp_context_injection import IdentityContext, resolve_source

# RFC 7230 token characters — the only thing a header NAME may contain.
_HEADER_NAME_RE = re.compile(r"^[A-Za-z0-9!#$%&'*+.^_`|~-]+$")

MAX_HEADERS = 24
MAX_HEADER_NAME_LEN = 128
MAX_HEADER_VALUE_LEN = 4096

# agent.name for a conversation with no agents pinned: Auto resolves its
# agents per run, so there is no fixed name to send.
AUTO_AGENT_NAME = "auto"

# Printable ASCII stays readable when a value is percent-encoded; only "%"
# itself is escaped so the encoded value decodes back unambiguously.
_ASCII_SAFE = "".join(c for c in string.printable if c not in "%\t\n\r\x0b\x0c")

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
    if not text.isascii():
        # Encode per character so the length cap never splits a character's
        # escapes, which would leave the value undecodable.
        pieces, size = [], 0
        for ch in text:
            piece = quote(ch, safe=_ASCII_SAFE)
            if size + len(piece) > MAX_HEADER_VALUE_LEN:
                break
            pieces.append(piece)
            size += len(piece)
        return "".join(pieces)
    return text[:MAX_HEADER_VALUE_LEN]


def agent_name_for_report(report: Any) -> str:
    """The ``agent.name`` value a question starts with, before any tool.

    What the user chose in the chat: the report's attached agents, comma-joined
    by name. A report with none attached is Auto (agents resolve per run) and
    sends ``auto``. Empty — so the header is omitted — when there is no report,
    or for an artifact chat with no agents, which is limited to the dashboard's
    own data rather than Auto.
    """
    if report is None:
        return ""
    try:
        attached = list(getattr(report, "data_sources", None) or [])
    except Exception:
        # Unloadable relationship: an unknown scope is not Auto.
        return ""
    names = _join_names(str(getattr(ds, "name", "") or "") for ds in attached)
    if names:
        return names
    if attached or getattr(report, "report_type", "regular") == "artifact_chat":
        return ""
    return AUTO_AGENT_NAME


def _join_names(names) -> str:
    return ", ".join(sorted({n.strip() for n in names} - {""}, key=str.casefold))


# Tools whose ``data_source_ids`` list names the agents they read about. The
# same field on other tools (create_prompt, create_eval …) attaches something
# to an agent rather than using it, so it is not read there.
AGENT_LIST_TOOLS = frozenset({"describe_tables"})


def agent_ids_from_tool_input(tool_name: str, tool_input: Any) -> List[str]:
    """Agent ids a tool call's input names, so known before the tool runs.

    Data tools (create_data, inspect_data, write_csv) name the agent of each
    table in ``tables_by_source[].data_source_id``; describe_tables lists them
    in ``data_source_ids`` (empty = every agent, so no specific one). File
    tools resolve theirs while running and record it in the run's
    ``used_agent_ids``. Tools that name no agent (saved queries, MCP) leave the
    value unchanged.
    """
    if not isinstance(tool_input, dict):
        return []
    ids: List[str] = []
    if tool_name in AGENT_LIST_TOOLS:
        ids += [str(d) for d in tool_input.get("data_source_ids") or [] if d]
    for entry in tool_input.get("tables_by_source") or []:
        if isinstance(entry, dict):
            did = entry.get("data_source_id")
        else:
            did = getattr(entry, "data_source_id", None)
        if did and not str(did).startswith("builtin:"):
            ids.append(str(did))
    return ids


def agent_ids_from_tool_output(tool_name: str, tool_output: Any) -> List[str]:
    """Agent ids a finished tool reports working on.

    describe_tables returns each matched table with its agent, which covers a
    call that searched by table name without naming any agent."""
    if tool_name not in AGENT_LIST_TOOLS:
        return []
    if isinstance(tool_output, dict):
        tables = tool_output.get("top_tables")
    else:
        tables = getattr(tool_output, "top_tables", None)
    ids: List[str] = []
    for t in tables or []:
        did = t.get("data_source_id") if isinstance(t, dict) else getattr(t, "data_source_id", None)
        if did:
            ids.append(str(did))
    return ids


def provider_uses_agent_name(additional_config: Optional[Dict[str, Any]]) -> bool:
    """Whether any forwarding rule reads ``agent.name`` (alone or interpolated)."""
    rules = (additional_config or {}).get("header_injection") or []
    return any(
        "agent.name" in str((r or {}).get("source", ""))
        for r in rules
        if isinstance(r, dict)
    )


class QuestionAgents:
    """The ``agent.name`` value for one question (one agent run).

    Starts at what the user chose in the chat (see agent_name_for_report).
    Agents the question fetches data from accumulate: once any is known the
    value is their names, comma-joined. A new question starts a new instance,
    so nothing carries over from earlier questions in the conversation.

    ``agents`` arguments are the agents the run can reach (id → name lookup);
    an id outside them is ignored rather than sent as a raw id.
    """

    def __init__(self, start_value: str = "") -> None:
        self.start_value = start_value
        self._names: Dict[str, str] = {}

    @staticmethod
    def _named(ids, agents) -> Dict[str, str]:
        by_id: Dict[str, str] = {}
        try:
            for ds in agents or []:
                name = str(getattr(ds, "name", "") or "").strip()
                if name:
                    by_id[str(getattr(ds, "id", ""))] = name
        except Exception:
            pass
        return {str(i): by_id[str(i)] for i in ids or [] if str(i) in by_id}

    def value(self, extra: Optional[Dict[str, str]] = None) -> str:
        names = {**self._names, **(extra or {})}
        return _join_names(names.values()) if names else self.start_value

    def value_during(self, tool_name: str, tool_input: Any, agents) -> str:
        """The value while this tool runs: the question's agents plus its own.

        Read-only, so tools running in parallel each see their own agents
        rather than whichever sibling registered first."""
        return self.value(self._named(agent_ids_from_tool_input(tool_name, tool_input), agents))

    def add(self, ids, agents) -> bool:
        """Record agents the question used; True when the value changed."""
        before = self.value()
        self._names.update(self._named(ids, agents))
        return self.value() != before

    def add_tool(self, tool_name: str, tool_input: Any, agents, tool_output: Any = None) -> bool:
        """Record a finished tool call: the agents its input named plus those
        its output reports (describe_tables)."""
        ids = agent_ids_from_tool_input(tool_name, tool_input)
        ids += agent_ids_from_tool_output(tool_name, tool_output)
        return self.add(ids, agents)


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
