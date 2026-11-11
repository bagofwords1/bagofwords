"""Make exception text safe to show the admin who triggered it.

Configuration endpoints (connections, git, OAuth) deliberately surface the
underlying error — "password authentication failed for user X" is what lets
an admin fix a connection. Driver and library messages can echo secrets,
though (a clone URL with a token in it, a full connection string), so every
such message goes through ``redact_error`` before it reaches a response.
Endpoints reachable without that context should return a fixed message
instead and log the exception with ``describe_for_log``, which keeps the type
and frames but not the message. A message can quote client content (prompts,
tool arguments, bound SQL parameters), so logging ``str(e)`` there would write
that content to the log.
"""
from __future__ import annotations

import re
import sysconfig
import traceback
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


def validation_reasons(exc: object) -> list[tuple[str, str]]:
    """``(field, message)`` for each error of a pydantic ``ValidationError``, [] for anything else.

    Read from the error list with the input left out: the text of the exception
    quotes the input and cuts it anywhere, even mid-key, so nothing can mask it.
    """
    errors = getattr(exc, "errors", None)
    if not callable(errors):
        return []
    return [(".".join(str(part) for part in error.get("loc", ())),
             str(error.get("msg", "")).removeprefix("Value error, "))
            for error in errors(include_input=False, include_url=False, include_context=False)]


def credentials_error_detail(exc: object) -> str:
    """The 422 detail for credentials a schema rejected: field names and rule messages, never the typed values."""
    reasons = "; ".join(f"{field}: {message}" if field else message for field, message in validation_reasons(exc))
    if not reasons:
        return "The required credential fields are missing or invalid"
    return f"Invalid credentials: {redact_error(reasons)}"


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
_STDLIB_ROOT = (sysconfig.get_paths().get("stdlib") or "") + "/"


def _log_path(filename: str) -> str:
    """A frame's file, relative to the backend or its package directory."""
    # Packages first: a virtualenv may live inside the backend directory.
    for marker in ("/site-packages/", "/dist-packages/"):
        if marker in filename:
            return filename.rsplit(marker, 1)[1]
    if filename.startswith(_BACKEND_ROOT):
        return filename[len(_BACKEND_ROOT):]
    if _STDLIB_ROOT != "/" and filename.startswith(_STDLIB_ROOT):
        return filename[len(_STDLIB_ROOT):]
    return filename.rsplit("/", 1)[-1]


def _format_frames(tb_frames) -> list:
    return [
        f"{_log_path(f.filename)}:{f.lineno} {f.name}"
        for f in traceback.StackSummary.extract(iter(tb_frames), lookup_lines=False)
    ]


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
    parts = _describe_one(exc)
    tb_frames = list(traceback.walk_tb(exc.__traceback__))
    if tb_frames:
        # Cut to the outermost and innermost frames before formatting any:
        # a RecursionError carries about a thousand.
        if len(tb_frames) > _MAX_FRAMES:
            half = _MAX_FRAMES // 2
            listed = _format_frames(tb_frames[:half]) + ["..."] + _format_frames(tb_frames[-half:])
        else:
            listed = _format_frames(tb_frames)
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


# ── User-facing error text ──────────────────────────────────────────────────
# What an admin sees in the connection-status log and test result. It has to
# say *what* failed clearly enough to act on, so the host, port, user and
# table names a driver reports stay: they are the admin's own settings and
# the thing to fix. Only credentials are masked (``redact_error``), plus the
# server's directory layout (``redact_paths``).

# Keys whose values are secrets, wherever they sit.
_SECRET_KEY = re.compile(
    r"(?i)(password|passwd|pwd|passphrase|secret|token|credential|"
    r"api[_\-]?key|access[_\-]?key|private[_\-]?key)"
)
# Keys of the credentials blob that are not secrets: who signs in and how.
_NOT_SECRET_KEY = re.compile(
    r"(?i)(user|login|email|account|role|tenant|domain|host|port|database|schema|"
    r"warehouse|region|scope|mode|type|_id$|^id$)"
)


def _is_secret_key(key: str, in_credentials: bool) -> bool:
    if re.search(r"(?i)(_id|_type)$", key):
        return False
    if _SECRET_KEY.search(key):
        return True
    return in_credentials and not _NOT_SECRET_KEY.search(key)


def connection_secrets(config: object = None, credentials: object = None) -> list:
    """A connection's secret values: passwords, tokens, keys.

    Passed to ``redact_error`` so a driver that echoes one in a shape the
    patterns don't recognise is still masked. Everything in the credentials
    blob counts unless its key names an identity or a mode (``user``,
    ``client_id``, ``auth_type``); in the config, only keys that name a secret.
    """
    out: set = set()

    def _walk(obj, secret: bool, in_credentials: bool):
        if isinstance(obj, dict):
            for k, v in obj.items():
                _walk(v, _is_secret_key(str(k), in_credentials), in_credentials)
        elif isinstance(obj, (list, tuple)):
            for v in obj:
                _walk(v, secret, in_credentials)
        elif isinstance(obj, str) and secret and obj.strip():
            out.add(obj.strip())

    _walk(credentials or {}, False, True)
    _walk(config or {}, False, False)
    # Longest first so a value is masked before any shorter value inside it.
    return sorted(out, key=len, reverse=True)


def sanitize_user_message(text: object, secrets: Optional[Iterable[str]] = None,
                          max_len: int = MAX_LEN) -> str:
    """``text`` with credentials masked, server paths trimmed, length capped."""
    text = "" if text is None else str(text)
    # Bound the work first: the patterns are applied to the whole text, and a
    # driver can hand back a page of HTML or a megabyte of SQL.
    text = text[: max_len * 4]
    return redact_paths(redact_error(text, secrets, max_len=max_len)).strip()


def _message_of(exc: BaseException) -> str:
    from starlette.exceptions import HTTPException

    if isinstance(exc, HTTPException):
        detail = exc.detail
        return detail if isinstance(detail, str) else str(detail)
    message = getattr(exc, "message", None)
    if isinstance(message, str) and message:
        return message
    if isinstance(exc, KeyError) and exc.args:
        return f"missing key {exc.args[0]!r}"
    return str(exc)


def _label(exc: BaseException) -> str:
    """``Type: message`` — the type is what makes ``KeyError: 'x'`` or an empty
    ``TimeoutError()`` intelligible. Our own exceptions (and HTTP errors)
    already speak plainly, so they keep just the message."""
    from starlette.exceptions import HTTPException

    message = _message_of(exc).strip()
    own = type(exc).__module__.startswith("app.") or isinstance(exc, HTTPException)
    if own and message:
        return message
    name = type(exc).__name__
    return f"{name}: {message}" if message else f"{name} (no details provided)"


def user_facing_error(exc: BaseException, secrets: Optional[Iterable[str]] = None,
                      max_len: int = MAX_LEN) -> str:
    """An exception as one clear, safe line for the admin who triggered it.

    ``Type: message``, followed by the exception it was raised from (drivers
    often wrap the useful error: ``OperationalError … caused by
    ConnectionRefusedError …``). Credentials are masked; ``secrets`` are the
    connection's own (see ``connection_secrets``), masked wherever they appear.
    """
    parts = [_label(exc)]
    seen = {id(exc)}
    current = exc
    for _ in range(2):
        nxt = current.__cause__ or (None if current.__suppress_context__ else current.__context__)
        if nxt is None or id(nxt) in seen:
            break
        seen.add(id(nxt))
        label = _label(nxt)
        if label and label not in " ".join(parts):
            parts.append(f"caused by {label}")
        current = nxt
    return sanitize_user_message("; ".join(parts), secrets, max_len=max_len)
