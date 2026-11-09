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


_BACKEND_ROOT = __file__.rsplit("/app/errors/", 1)[0] + "/"
_MAX_FRAMES = 40
_MAX_CHAIN = 5


def _log_path(filename: str) -> str:
    """A frame's file, relative to the backend or its package directory."""
    import sysconfig

    # Packages first: a virtualenv may live inside the backend directory.
    for marker in ("/site-packages/", "/dist-packages/"):
        if marker in filename:
            return filename.rsplit(marker, 1)[1]
    if filename.startswith(_BACKEND_ROOT):
        return filename[len(_BACKEND_ROOT):]
    stdlib = sysconfig.get_paths().get("stdlib", "") + "/"
    if stdlib != "/" and filename.startswith(stdlib):
        return filename[len(stdlib):]
    return filename.rsplit("/", 1)[-1]


def _constraint_name(orig: object) -> Optional[str]:
    """The constraint a driver error names: asyncpg sets it on the error (the
    SQLAlchemy adapter keeps that as ``__cause__``), psycopg2 on ``diag``."""
    for source in (orig, getattr(orig, "diag", None), getattr(orig, "__cause__", None)):
        name = getattr(source, "constraint_name", None) if source is not None else None
        if isinstance(name, str) and name:
            return name
    return None


def _describe_one(exc: BaseException) -> list:
    from pydantic import ValidationError
    from sqlalchemy.exc import DBAPIError
    from starlette.exceptions import HTTPException

    parts = [type(exc).__name__]
    if isinstance(exc, HTTPException):
        parts.append(str(exc.status_code))
    if isinstance(exc, ValidationError):
        fields = [f"{tuple(e['loc'])!r}:{e['type']}" for e in exc.errors(include_input=False, include_url=False)]
        parts.append(f"{exc.title}[" + ", ".join(fields) + "]")
    if isinstance(exc, DBAPIError) and exc.orig is not None:
        orig = exc.orig
        driver = type(orig).__name__
        inner = getattr(orig, "__cause__", None)
        if inner is not None and type(inner).__name__ != driver:
            driver += f"({type(inner).__name__})"
        parts.append(f"orig={driver}")
        constraint = _constraint_name(orig)
        if constraint:
            parts.append(f"constraint={constraint}")
    return parts


def describe_for_log(exc: BaseException) -> str:
    """An exception as a log line without its message.

    For paths that handle content a client sent (prompts, tool arguments,
    queries and their rows): the message of a pydantic error quotes the
    submitted input, a DB error carries the statement's bound parameters, and
    driver or conversion errors quote cells. Keeps what is structural: the
    type, an HTTP status, the model and fields that failed validation, the
    driver error and the constraint, every frame (repo-relative
    ``file:line func``, outermost first) and the types of the exceptions it
    was raised from or during. Never calls ``str()`` on any exception.
    """
    import traceback

    parts = _describe_one(exc)
    frames = traceback.StackSummary.extract(traceback.walk_tb(exc.__traceback__), lookup_lines=False)
    if frames:
        listed = [f"{_log_path(f.filename)}:{f.lineno} {f.name}" for f in frames]
        if len(listed) > _MAX_FRAMES:
            half = _MAX_FRAMES // 2
            listed = listed[:half] + ["..."] + listed[-half:]
        parts.append("at " + " > ".join(listed))

    seen = {id(exc)}
    current = exc
    for _ in range(_MAX_CHAIN):
        if current.__cause__ is not None:
            current, how = current.__cause__, "caused by"
        elif current.__context__ is not None and not current.__suppress_context__:
            current, how = current.__context__, "during"
        else:
            break
        if id(current) in seen:
            break
        seen.add(id(current))
        parts.append(f"; {how} " + " ".join(_describe_one(current)))
    return " ".join(parts)
