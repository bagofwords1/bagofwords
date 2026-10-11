"""Make exception text safe to show the admin who triggered it.

Configuration endpoints (connections, git, OAuth) deliberately surface the
underlying error — "password authentication failed for user X" is what lets
an admin fix a connection. Driver and library messages can echo secrets,
though (a clone URL with a token in it, a full connection string), so every
such message goes through ``redact_error`` before it reaches a response.
Endpoints reachable without that context should return a fixed message
instead and log the exception.
"""
from __future__ import annotations

import re
from typing import Iterable, Optional

MASK = "***"
MAX_LEN = 500

# scheme://user:password@host  or  scheme://token@host
_URL_USERINFO = re.compile(r"(?P<scheme>[a-zA-Z][a-zA-Z0-9+.\-]*://)[^/\s@]+@")
# Authorization: Bearer xxx / Basic xxx
_AUTH_HEADER = re.compile(r"(?i)\b(bearer|basic|token)\s+[A-Za-z0-9\-._~+/=]{8,}")
# key=value (connection strings, query strings) or "key": "value" (JSON).
# A bare "key: words" is left alone: that is prose ("Invalid token: expired").
_SECRET_KV = re.compile(
    r"""(?ix)
    (?P<key>["']?[\w\-]*(?:password|passwd|pwd|secret|token|api[_\-]?key|access[_\-]?key|private[_\-]?key)["']?)
    (?:
        (?P<sep>\s*=\s*)(?P<val>"[^"]*"|'[^']*'|[^\s,;&)}\]]+)
      | (?P<sep2>\s*:\s*)(?P<qval>"[^"]*"|'[^']*')
    )
    """
)


def redact_error(exc: object, secrets: Optional[Iterable[str]] = None, max_len: int = MAX_LEN) -> str:
    """``str(exc)`` with credentials masked and the length capped.

    ``secrets`` are known values (e.g. the connection's own stored password)
    that are masked wherever they appear, for drivers that echo them in a
    shape the patterns don't recognise.
    """
    text = str(exc)
    for s in secrets or ():
        if s and isinstance(s, str) and len(s) >= 4:
            text = text.replace(s, MASK)
    text = _URL_USERINFO.sub(lambda m: f"{m.group('scheme')}{MASK}@", text)
    text = _AUTH_HEADER.sub(lambda m: f"{m.group(1)} {MASK}", text)

    def _kv(m: re.Match) -> str:
        sep = m.group("sep") if m.group("sep") is not None else m.group("sep2")
        val = m.group("val") if m.group("val") is not None else m.group("qval")
        quote = val[0] if val[:1] in ("'", '"') else ""
        return f"{m.group('key')}{sep}{quote}{MASK}{quote}"

    text = _SECRET_KV.sub(_kv, text)
    if len(text) > max_len:
        text = text[:max_len] + "…"
    return text


# An absolute POSIX path of two or more segments; keep only the last one.
_ABS_PATH = re.compile(r"(?<![\w:/.])/(?:[\w.\-]+/)+([\w.\-]+)")


def redact_paths(text: str) -> str:
    """Replace absolute filesystem paths with their file name.

    For error text that goes back to a model or a user: the server's
    directory layout is not theirs to see, and the file name is enough to
    act on ("slide.pptx not found").
    """
    return _ABS_PATH.sub(lambda m: m.group(1), text)
