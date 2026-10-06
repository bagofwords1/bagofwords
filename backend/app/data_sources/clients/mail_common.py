"""Small, provider-neutral helpers shared by native mail connectors."""

from __future__ import annotations

import base64
import binascii
import html
import json
import re
from datetime import datetime, timezone
from typing import Any, Dict, Optional

_BREAK_TAG_RE = re.compile(
    r"<\s*(?:br\s*/?|/p|/div|/li|/tr|/h[1-6])\s*>",
    flags=re.IGNORECASE,
)
_TAG_RE = re.compile(r"<[^>]+>")


def strip_html(value: str) -> str:
    """Render a conservative plain-text representation of an HTML email.

    This is deliberately not an HTML sanitizer for browser rendering: mail
    bodies never reach the browser as HTML.  It preserves common block breaks,
    removes tags, decodes entities, and normalizes whitespace for the model.
    """
    text = _BREAK_TAG_RE.sub("\n", value or "")
    text = _TAG_RE.sub(" ", text)
    text = html.unescape(text).replace("\xa0", " ")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


# --------------------------------------------------------------- paging

#: Default and hard ceiling for one list/search call. A daily digest over a
#: busy mailbox needs every message in the window, not the first page — but a
#: single observation still has to fit the planner's context, so anything past
#: the ceiling comes back as a cursor rather than more rows.
DEFAULT_MAIL_RESULTS = 100
MAX_MAIL_RESULTS = 200


def encode_cursor(state: Dict[str, Any]) -> str:
    """Opaque, URL-safe paging cursor handed to the agent."""
    raw = json.dumps(state, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: Optional[str]) -> Dict[str, Any]:
    if not cursor:
        return {}
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        state = json.loads(base64.urlsafe_b64decode(padded.encode()))
    except (ValueError, TypeError, binascii.Error) as exc:
        raise ValueError("Invalid cursor — pass back the next_cursor value exactly as returned.") from exc
    if not isinstance(state, dict):
        raise ValueError("Invalid cursor — pass back the next_cursor value exactly as returned.")
    return state


def parse_mail_datetime(value: Optional[str]) -> Optional[datetime]:
    """Parse an agent-supplied date bound into an aware UTC datetime.

    Accepts a bare date (``2026-10-05``) or an ISO-8601 timestamp with or
    without an offset; a naive timestamp is taken as UTC.
    """
    if value is None or not str(value).strip():
        return None
    text = str(value).strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(
            f"Unrecognised date {value!r} — use ISO-8601, e.g. 2026-10-05 or 2026-10-05T06:00:00Z."
        ) from exc
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def clamp_mail_results(value: Optional[int]) -> int:
    try:
        n = int(value) if value is not None else DEFAULT_MAIL_RESULTS
    except (TypeError, ValueError):
        n = DEFAULT_MAIL_RESULTS
    return max(1, min(n, MAX_MAIL_RESULTS))


def graph_search_value(query: str) -> str:
    """Render a free-text/KQL query as one Graph ``$search`` value.

    Graph wants the whole KQL expression inside ONE pair of double quotes
    (``"from:alice subject:report"``). An inner phrase quote must be escaped,
    otherwise Graph rejects the request with ``Syntax error: character '"' is
    not valid`` — which is how a search like ``breakdown OR "main engine"``
    failed outright. A query the model already wrapped in quotes is unwrapped
    first so it is not double-wrapped.
    """
    q = (query or "").strip()
    if len(q) >= 2 and q[0] == '"' and q[-1] == '"' and '"' not in q[1:-1]:
        q = q[1:-1].strip()
    q = q.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{q}"'
