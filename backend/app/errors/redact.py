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
# say *what* failed clearly enough to act on, without the server's identity
# (hosts, IPs, filesystem layout, internal ids) or anybody's credentials or
# identity (emails, usernames) in it.

_EMAIL = re.compile(r"(?i)\b[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}\b")
_IPV4 = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])")
# Full (>= 4 groups) or compressed ("::") IPv6. A bare "10:29:54" timestamp
# has 3 groups and no "::", so it is left alone.
_IPV6 = re.compile(
    r"(?i)(?<![\w:])(?:(?:[0-9a-f]{1,4}:){3,7}[0-9a-f]{1,4}"
    r"|(?:[0-9a-f]{1,4}:){1,6}:(?:[0-9a-f]{1,4}(?::[0-9a-f]{1,4})*)?"
    r"|::(?:[0-9a-f]{1,4}:)*[0-9a-f]{1,4})(?![\w:])"
)
# A bare hostname ("could not resolve bi01.corp.example.net"). Limited to
# common public/intranet TLDs so dotted identifiers the admin needs —
# schema.table, table.column, file.py — are left alone.
_HOSTNAME = re.compile(
    r"(?i)(?<![\w.\-])(?:[a-z0-9](?:[a-z0-9\-]{0,61}[a-z0-9])?\.)+"
    r"(?:com|net|org|io|gov|edu|mil|biz|info|local|localdomain|internal|corp|lan|intra|"
    r"cloud|il|uk|de|fr|nl|ca|au|ru|cn|jp|br|eu|ch|se|pl)(?![\w\-])"
)
_UUID = re.compile(r"(?i)\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b")
# The host part of any URL: scheme://host[:port]
_URL_HOST = re.compile(r"(?P<scheme>[a-zA-Z][a-zA-Z0-9+.\-]*://)(?:\*\*\*@)?(?P<host>\[[^\]\s]+\]|[^/\s:?#'\"]+)")

# Config keys whose values identify the server or a person.
_IDENTITY_KEY = re.compile(
    r"(?i)(host|server|endpoint|url|uri|dsn|domain|tenant|account|username|user_?id|login|email|principal)"
)


def connection_secrets(config: object = None, credentials: object = None) -> list:
    """Values of a connection that must never appear in a user-facing message.

    Every credential string (passwords, tokens, keys) plus the config values
    that identify the server or a person (host, URL, account, username …).
    For a URL value, its host is listed too, since a driver usually echoes
    only the host.
    """
    from urllib.parse import urlparse

    out: list = []

    def _walk(obj, key_ok):
        if isinstance(obj, dict):
            for k, v in obj.items():
                _walk(v, key_ok(str(k)))
        elif isinstance(obj, (list, tuple)):
            for v in obj:
                _walk(v, key_ok)
        elif isinstance(obj, str) and key_ok is True and obj.strip():
            value = obj.strip()
            out.append(value)
            if "://" in value:
                try:
                    host = urlparse(value).hostname
                except ValueError:
                    host = None
                if host:
                    out.append(host)

    # Every credential value is secret, whatever its key.
    _walk(credentials or {}, lambda _k: True)
    _walk(config or {}, lambda k: bool(_IDENTITY_KEY.search(k)))
    # Longest first so a value is masked before any shorter value inside it.
    return sorted({s for s in out if len(s) >= 3}, key=len, reverse=True)


def _mask_identity(text: str, secrets: Iterable[str]) -> str:
    for s in secrets:
        if not s or not isinstance(s, str):
            continue
        if len(s) >= 4 and not s.isalnum():
            # Passwords, tokens, hosts, emails: mask wherever they appear.
            text = re.sub(re.escape(s), MASK, text, flags=re.IGNORECASE)
        else:
            # Short / plain-word values ("admin", "db1") only as whole words,
            # so "admin" doesn't turn "administrator" into "***istrator".
            text = re.sub(rf"(?<![\w]){re.escape(s)}(?![\w])", MASK, text, flags=re.IGNORECASE)
    return text


def sanitize_user_message(text: object, secrets: Optional[Iterable[str]] = None,
                          max_len: int = MAX_LEN) -> str:
    """Mask credentials, server identity and personal identity in ``text``."""
    text = "" if text is None else str(text)
    secrets = list(secrets or ())
    text = _mask_identity(text, secrets)
    # Credentials in URLs / headers / key=value pairs (no length cap yet).
    text = redact_error(text, max_len=10**9)
    text = _URL_HOST.sub(lambda m: f"{m.group('scheme')}{MASK}", text)
    text = _EMAIL.sub(MASK, text)
    text = _HOSTNAME.sub(MASK, text)
    text = _UUID.sub(MASK, text)
    text = _IPV4.sub(MASK, text)
    text = _IPV6.sub(MASK, text)
    text = redact_paths(text)
    text = text.strip()
    if len(text) > max_len:
        text = text[:max_len] + "…"
    return text


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
    ConnectionRefusedError …``). Credentials, hosts, IPs, emails, internal
    ids and filesystem paths are masked; ``secrets`` are the connection's own
    values (see ``connection_secrets``), masked wherever they appear.
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
