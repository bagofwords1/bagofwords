"""Tag the queries BOW sends to a warehouse with who caused them.

A warehouse admin reading their query history (Snowflake's
`ACCOUNT_USAGE.QUERY_HISTORY`, say) sees every BOW query come from one service
user. Without a tag they cannot tell which report, chat turn or refresh ran a
query, or what that cost. So every query carries a small JSON tag naming its
origin:

    {"app":"bow","source":"agent","org_id":"…","report_id":"…",…}

The tag is assembled here, from three ambient layers, narrowest last:

1. The LLM usage attribution AgentV2 already stamps for each run
   (`app.ai.llm.usage_attribution`): org, user, report, agent run.
2. `query_tag_defaults(...)`: what an entry point knows about itself (a
   query rerun, a dashboard refresh). It only fills gaps, so an outer, more
   specific caller wins: a scheduled refresh stays "scheduled_refresh" when it
   reaches the generic step runner.
3. `query_tag_scope(...)`: values that must win (the step being run).

Only IDs and short enum-like strings go in. Values are restricted to a safe
character set, so the tag never carries prompt text or PII and can be put in
a SQL string literal without escaping problems.

Queries run on worker threads that do not inherit context variables (the
query-timeout thread, the sandbox RPC thread). The query wrapper therefore
snapshots the tag when it is built (`current_query_tag()`) and re-applies it
on its query thread (`applied_query_tag(...)`).
"""

from __future__ import annotations

import contextlib
import json
import re
from collections.abc import Iterator
from contextvars import ContextVar
from typing import Any

APP_NAME = "bow"

# The other direction: a client that knows the warehouse's own id for a
# statement puts it on the frame it returns (`df.attrs[SOURCE_QUERY_ID_ATTR]`),
# or on the exception for a failed statement. The query wrapper copies it onto
# the query's timing record as `source_query_id`, so a BOW step links to the
# exact row in the warehouse's query history.
SOURCE_QUERY_ID_ATTR = "bow_source_query_id"

# Snowflake caps QUERY_TAG at 2000 characters.
MAX_TAG_CHARS = 2000

# Every key a tag may carry, in priority order: when a tag has to be shortened
# the keys at the end are dropped first.
TAG_KEYS = (
    "app",
    "source",
    "org_id",
    "report_id",
    "completion_id",
    "agent_execution_id",
    "step_id",
    "query_id",
    "entity_id",
    "release_id",
    "connection_id",
    "data_source_id",
    "user_id",
    "ref_id",
    "label",
)

# IDs, enum-like sources ("mcp.create_data"), and an admin's label. No quotes,
# backslashes or semicolons, so the value is inert inside a SQL literal.
_VALUE_RE = re.compile(r"^[A-Za-z0-9 _.:/@-]+$")
_MAX_VALUE_CHARS = 100

_scope: ContextVar[dict[str, str] | None] = ContextVar("bow_query_tag_scope", default=None)


def label_error(value: Any) -> str | None:
    """Why `value` cannot be a connection's query tag label, or None if it can."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if not isinstance(value, str):
        return "must be text"
    if len(value.strip()) > _MAX_VALUE_CHARS:
        return f"must be at most {_MAX_VALUE_CHARS} characters"
    if not _VALUE_RE.match(value.strip()):
        return "may only contain letters, digits, spaces and _ . : / @ -"
    return None


def _clean(value: Any) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    if not text or len(text) > _MAX_VALUE_CHARS or not _VALUE_RE.match(text):
        return None
    return text


def _clean_fields(fields: dict[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, value in fields.items():
        if key not in TAG_KEYS or key == "app":
            continue
        cleaned = _clean(value)
        if cleaned is not None:
            out[key] = cleaned
    return out


@contextlib.contextmanager
def query_tag_scope(**fields: Any) -> Iterator[None]:
    """Set tag fields for the block. These override outer values."""
    merged = {**(_scope.get() or {}), **_clean_fields(fields)}
    token = _scope.set(merged)
    try:
        yield
    finally:
        _scope.reset(token)


def set_query_tag_scope(**fields: Any):
    """`query_tag_scope` for code that cannot nest a `with` block (a long run
    body with its own try/finally). Returns the token for `reset_query_tag_scope`."""
    return _scope.set({**(_scope.get() or {}), **_clean_fields(fields)})


def reset_query_tag_scope(token) -> None:
    with contextlib.suppress(Exception):
        _scope.reset(token)


@contextlib.contextmanager
def query_tag_defaults(**fields: Any) -> Iterator[None]:
    """Set tag fields for the block only where nothing set them already."""
    merged = {**_clean_fields(fields), **(_scope.get() or {})}
    token = _scope.set(merged)
    try:
        yield
    finally:
        _scope.reset(token)


@contextlib.contextmanager
def applied_query_tag(tag: dict[str, str] | None) -> Iterator[None]:
    """Make `tag` (a `current_query_tag()` snapshot) current on this thread."""
    token = _scope.set(_clean_fields(tag or {}))
    try:
        yield
    finally:
        _scope.reset(token)


def _from_usage_attribution() -> dict[str, str]:
    try:
        from app.ai.llm.usage_attribution import get_usage_attribution
    except Exception:  # pragma: no cover - import guard for slim test envs
        return {}
    attribution = get_usage_attribution()
    return _clean_fields({
        "org_id": attribution.get("organization_id"),
        "user_id": attribution.get("user_id"),
        "report_id": attribution.get("report_id"),
        "agent_execution_id": attribution.get("agent_execution_id"),
        "data_source_id": attribution.get("data_source_id"),
    })


def current_query_tag(**extra: Any) -> dict[str, str]:
    """The tag for a query issued now, as an ordered dict.

    Always names the app and a source ("system" when no entry point said
    otherwise), so every BOW query can be found in the warehouse's history.
    """
    merged = {**_from_usage_attribution(), **(_scope.get() or {}), **_clean_fields(extra)}
    merged.setdefault("source", "system")
    tag = {"app": APP_NAME}
    for key in TAG_KEYS[1:]:
        if key in merged:
            tag[key] = merged[key]
    return tag


def render_query_tag(tag: dict[str, str], max_chars: int = MAX_TAG_CHARS) -> str:
    """Compact JSON for `tag`, dropping low-priority keys to fit `max_chars`."""
    ordered = {k: tag[k] for k in TAG_KEYS if k in tag}
    keys = list(ordered)
    while True:
        text = json.dumps({k: ordered[k] for k in keys}, separators=(",", ":"))
        if len(text) <= max_chars or len(keys) <= 2:
            return text[:max_chars]
        keys.pop()
