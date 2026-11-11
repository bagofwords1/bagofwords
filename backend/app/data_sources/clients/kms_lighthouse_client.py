"""Read-only KMS Lighthouse connector over the KMS Lighthouse public API.

Talks to ``{base_url}{api_prefix}/...`` only (default prefix ``/kms/lh/api``).
Every call runs live as one identity, so KMS permissions decide what is seen:

  * ``userpass`` - ``POST {prefix}/login?username&password``. The session is
    the response cookies and, when the body carries one, a Bearer token.
  * ``token``    - a pasted API token, sent as ``Authorization: Bearer``. It
    never logs in.

Messages leaving this module are fixed strings: they never carry a request
URL, a query string, a provider response body or a credential.
"""
from __future__ import annotations

import base64
import binascii
import functools
import hashlib
import hmac
import html
import http.client
import errno
import json
import logging
import os
import re
import socket
import ssl
import threading
import time
import unicodedata
from collections import Counter, OrderedDict, deque
from contextlib import contextmanager
from html.entities import html5 as _HTML5_ENTITIES
from datetime import datetime
from urllib.parse import unquote, urlsplit

import requests
import urllib3
from app.core.egress import pinned_session

from app.data_sources.clients.base import Capability, DataSourceClient, SearchStopped
from app.data_sources.clients._file_source_common import DocumentText, FileTooLargeError, GlobScopeError
from app.data_sources.clients.progress import discovery_progress
from app.schemas.data_sources.configs import KMS_TOKEN_FORMAT_MESSAGE, KMS_TOKEN_RE

logger = logging.getLogger(__name__)

# ADR-2: the login sends the password in the query string, and urllib3 logs
# every request line at DEBUG. While this thread is inside a KMS exchange, the
# urllib3 records are dropped; every other connector's records are untouched.
_IN_KMS_REQUEST = threading.local()
_URLLIB3_LOGGERS = ("urllib3.connectionpool", "urllib3.connection", "urllib3.util.retry")


class _KmsRequestLogGate(logging.Filter):
    kms_lighthouse_gate = True

    def filter(self, record: logging.LogRecord) -> bool:
        return not getattr(_IN_KMS_REQUEST, "active", False)


# A diagnostics run calls the connector's public methods to prove them, with
# at most one login in the whole run. While ``proof_calls`` is active on this
# thread a login is refused, and every exchange takes its deadline from ``take_call``.
_PROOF = threading.local()


@contextmanager
def proof_calls(take_call):
    """No login on this thread inside the block; ``take_call()`` budgets each exchange, or raises to stop."""
    _PROOF.take_call = take_call
    try:
        yield
    finally:
        _PROOF.take_call = None


def _refuse_login_in_proof() -> None:
    if getattr(_PROOF, "take_call", None) is not None:
        logger.debug("kms_lighthouse %s refused: %s", "login", "diagnostics sends one login at most")
        raise _LoginFailure(401, "auth", _SESSION_REFUSED)


def _install_urllib3_gate() -> None:
    for name in _URLLIB3_LOGGERS:
        target = logging.getLogger(name)
        # Once per logger, even if this module is imported again.
        if not any(getattr(f, "kms_lighthouse_gate", False) for f in target.filters):
            target.addFilter(_KmsRequestLogGate())


_install_urllib3_gate()

_PREFIX_RE = re.compile(r"^(/[A-Za-z0-9._~-]+)+$")
_DIGITS_RE = re.compile(r"[0-9]{1,12}")      # used with fullmatch only ("279\n" is not an id)
_DEFAULT_PREFIX = "/kms/lh/api"
_DEFAULT_MAX_RESULTS = 200

_USERPASS_AUTH = (
    "KMS Lighthouse did not accept the login. Check the KMS username and password. "
    "If this connection uses each person's own KMS account, that person should reconnect it."
)
_TOKEN_AUTH = (
    "KMS Lighthouse did not accept the API token; it may be wrong or no longer valid. "
    "If this connection uses each person's own KMS account, that person should reconnect it."
)
_NO_SESSION = (
    "KMS Lighthouse accepted the login but returned no session, so it cannot be used. "
    "Ask the KMS administrator whether this account may use the KMS API."
)
_HELD_AUTH = _USERPASS_AUTH + " New login attempts with this account are paused for a few minutes."
_SESSION_REFUSED = (
    "KMS Lighthouse accepted the login but refused the session it returned. "
    "Ask the KMS administrator whether this account may use the KMS API."
)
_UNSENDABLE_LOGIN = (
    "The KMS username or password contains a character that cannot be sent to KMS Lighthouse. "
    "Re-enter the KMS username and password."
)
_MESSAGES = {
    "auth": _USERPASS_AUTH,
    "forbidden": "KMS Lighthouse denied access to this content for the current identity.",
    "not_found": "Not found in KMS Lighthouse, or not available to this identity.",
    "throttled": "KMS Lighthouse is limiting requests right now; try again shortly.",
    "bad_request": "KMS Lighthouse rejected the request.",
    "server": "KMS Lighthouse returned an error or an unexpected response.",
    "no_data": "KMS Lighthouse returned no data (check the layout id and the account's permissions).",
    "redirect": "KMS Lighthouse answered with a redirect, which is not followed.",
    "rejected": "KMS Lighthouse reported an error for this request.",
    "unreachable": "KMS Lighthouse could not be reached.",
    "too_large": "The KMS Lighthouse response was too large.",
}
_SEARCH_REJECTED = (
    "KMS Lighthouse rejected the search; the query may be too long (try fewer words), "
    "or the Layout ID setting may be wrong."
)
_BAD_LIMIT = "The search limit must be a whole number."
# What the file tools say when the selected KMS connection, or its agent, was turned off.
INACTIVE_CONNECTION_MESSAGE = (
    "The KMS Lighthouse connection '{name}' is inactive after failed connection tests. Do NOT retry: "
    "tell the user to ask an admin to test the connection, then continue with the remaining sources."
)
# The same, when only the agent (data source) was turned off and the KMS connection itself is active.
INACTIVE_AGENT_MESSAGE = (
    "The agent that holds the KMS Lighthouse connection '{name}' was turned off because one of its "
    "connections failed its tests. Do NOT retry: tell the user to ask an admin to test that agent's "
    "connections, then continue with the remaining sources."
)
_TOP_LEVEL_HINT = (
    "Connected to KMS Lighthouse, but the top level of the folder tree came back empty. Set Root folder ID "
    "to a folder this account can open, or check the account's KMS permissions."
)
_EMPTY_ROOT = (
    "Connected to KMS Lighthouse, but root folder {root} came back empty. KMS answers the same way for a folder "
    "id that is not valid, so check Root folder ID and that this account can open that folder."
)
_BAD_LAYOUT = "Connected to KMS Lighthouse, but KMS refused layout {layout}. Check Layout ID."
_MAX_RESPONSE_BYTES = 16 * 1024 * 1024
_DEFAULT_TIMEOUT = (10, 60)    # (connect, read) seconds
_MIN_TIMEOUT_S = 1
_CHUNK_BYTES = 64 * 1024
# Everything one HTTP exchange can raise. Several are not RequestException:
# urllib3 errors from a broken body, OSError (e.g. a bad CA bundle path),
# UnicodeError (a header value that is not Latin-1; its .object holds the
# value) and CookieConflictError (a RuntimeError).
_TRANSPORT_ERRORS = (requests.RequestException, urllib3.exceptions.HTTPError, http.client.HTTPException,
                     OSError, UnicodeError, requests.cookies.CookieConflictError)
# A firewall or a missing route, whatever urllib3 calls it.
_BLOCKED_ROUTE_ERRNOS = (errno.EHOSTUNREACH, errno.ENETUNREACH, errno.EACCES, errno.EPERM)
_URL_MESSAGE = ("KMS Lighthouse URL must be an HTTP(S) URL such as https://example.lighthouse-cloud.com, "
                "without a user name or password in the URL, a query, or a fragment.")
_NO_CREDENTIAL = "Provide a KMS username and password or an API token."


# ADR-6: userpass clients for one identity share one KMS session per process.
# Every tool call builds a new client, so a per-instance session would mean
# one login per call; a rejected login is not repeated for a while, so a wrong
# password cannot lock the KMS account. Nothing is persisted.
_SESSION_TTL_S = 600
_FAILED_LOGIN_HOLD_S = 300
_SESSION_POOL_MAX = 256
# A per-process random key: the pool key is neither the password nor a hash
# of it that could be checked offline.
_POOL_SECRET = os.urandom(32)
_clock = time.monotonic          # the only time source for the TTL and the hold (a test seam)


class _PoolEntry:
    """Login state for one identity, shared by every client instance and thread using it.

    ``login_lock`` serialises logins (so one expiry causes one login);
    ``state_lock`` guards the fields below. Lock order: login_lock, then state_lock
    (the pool's own lock, when held, comes before state_lock too).
    """

    def __init__(self):
        self.login_lock = threading.Lock()
        self.state_lock = threading.Lock()
        self.generation = 0
        self.cookies: dict = {}
        self.token: str | None = None
        self.created_at: float | None = None   # when the current session was logged in
        self.failed_at: float | None = None    # when KMS last rejected this login


_POOL: OrderedDict = OrderedDict()
_POOL_LOCK = threading.Lock()


def _pool_key(base: str, prefix: str, username: str, password: str) -> str:
    # Length-prefixed fields, so no two identities join to the same text (a field may hold "\0" or ":").
    message = "".join(f"{len(field)}:{field}" for field in (base, prefix, username, password))
    # surrogatepass: a lone surrogate from a JSON credential must not raise (with the password in .object).
    return hmac.new(_POOL_SECRET, message.encode("utf-8", "surrogatepass"), hashlib.sha256).hexdigest()


def _young(entry: _PoolEntry, now: float) -> bool:
    """The entry holds a session young enough to reuse (caller holds ``state_lock``)."""
    return entry.created_at is not None and now - entry.created_at < _SESSION_TTL_S


def _held(entry: _PoolEntry, now: float) -> bool:
    """A rejected login of this identity is recent enough to refuse new ones."""
    return entry.failed_at is not None and now - entry.failed_at < _FAILED_LOGIN_HOLD_S


def _is_idle(entry: _PoolEntry, now: float) -> bool:
    """No young session, no active login hold and no login in flight: cheap to forget.

    A login in flight counts as busy: forgetting its entry would let its
    outcome (a session, or a rejection and its hold) land on an orphan.
    """
    if entry.login_lock.locked():
        return False
    with entry.state_lock:
        return not _young(entry, now) and not _held(entry, now)


def _pool_entry(key: str) -> _PoolEntry:
    with _POOL_LOCK:
        entry = _POOL.get(key)
        if entry is not None:
            _POOL.move_to_end(key)          # least recently used goes first
            return entry
        entry = _POOL[key] = _PoolEntry()
        while len(_POOL) > _SESSION_POOL_MAX:
            # Forget an idle entry before a live session or a hold (forgetting a
            # hold would let a rejected password be sent again); else the LRU one.
            now = _clock()
            victim = next((k for k, e in _POOL.items() if k != key and _is_idle(e, now)), None)
            if victim is None:
                _POOL.popitem(last=False)
            else:
                del _POOL[victim]
        return entry


def clear_session_cache() -> None:
    """Forget every pooled session and login hold in this process (tests and operations).

    Live client instances keep their entry, so it is reset too: their next
    call logs in again instead of reusing the forgotten session or hold. A
    login in flight during the clear may still land on live instances (its
    session or hold is stored when it finishes).
    """
    with _POOL_LOCK:
        for entry in _POOL.values():
            with entry.state_lock:
                entry.cookies, entry.token = {}, None
                entry.created_at = entry.failed_at = None
                entry.generation += 1     # thread-local sessions drop the old cookies
        _POOL.clear()


class KmsError(ValueError):
    """A KMS Lighthouse failure with a fixed, secret-free message."""


class KmsHTTPError(KmsError):
    transport: str | None = None     # how an unreachable KMS failed (``_transport_failure``): a fixed word
    answered: int = 0                # the HTTP status, when the failure came after it was read
    verify_code: int | None = None   # OpenSSL's code, when a certificate failed verification
    connect_timeout: bool = False    # the connect timed out, not the read

    def __init__(self, status: int, kind: str, message: str | None = None):
        if kind not in _MESSAGES:
            raise ValueError(f"unknown KMS error kind {kind!r}")
        self.status = status
        self.kind = kind
        super().__init__(message or _MESSAGES[kind])


class _DeadlineTimeout(KmsHTTPError):
    """A request timed out with the caller's deadline reached: "unreachable", but search keeps what it found."""

    def __init__(self):
        super().__init__(0, "unreachable")


class _LoginFailure(KmsHTTPError):
    """A login that failed (rejected, held, a 5xx, an unusable answer): it concerns every call, never one branch or item."""


# Kinds whose message speaks of the content asked for; a failed login says nothing about it.
_CONTENT_KINDS = frozenset(("not_found", "forbidden", "bad_request", "no_data", "rejected"))


def _is_timeout(exc: BaseException) -> bool:
    """A connect or read timeout, also when requests wraps urllib3's (a read timeout while streaming)."""
    timeouts = (requests.Timeout, urllib3.exceptions.TimeoutError, TimeoutError)

    def timed_out(link) -> bool:
        # NewConnectionError subclasses urllib3's timeout but only says the connect failed.
        # requests excludes it too; a real timeout shows in its cause.
        return isinstance(link, timeouts) and not isinstance(link, urllib3.exceptions.NewConnectionError)

    return timed_out(exc) or any(timed_out(arg) for arg in exc.args)


def _exception_chain(exc: BaseException) -> list:
    """``exc`` and what it wraps (requests and urllib3 nest exceptions in args, ``reason`` and causes)."""
    found, stack = [], [exc]
    while stack and len(found) < 32:
        current = stack.pop()
        if any(current is seen for seen in found):
            continue
        found.append(current)
        linked = [current.__cause__, current.__context__, getattr(current, "reason", None), *current.args]
        stack.extend(link for link in linked if isinstance(link, BaseException))
    return found


def _transport_failure(exc: BaseException) -> str:
    """A fixed word for a transport failure, by exception type only (the text holds the URL)."""
    chain = _exception_chain(exc)
    if any(isinstance(link, ssl.SSLCertVerificationError) for link in chain):
        return "untrusted_certificate"
    if any(isinstance(link, (ssl.SSLError, requests.exceptions.SSLError, urllib3.exceptions.SSLError)) for link in chain):
        return "tls_failed"
    if any(isinstance(link, (socket.gaierror, urllib3.exceptions.NameResolutionError)) for link in chain):
        return "dns"
    if any(isinstance(link, ConnectionRefusedError) for link in chain):
        return "refused"
    if any(isinstance(link, OSError) and link.errno in _BLOCKED_ROUTE_ERRNOS for link in chain):
        return "blocked_route"
    if any(_is_timeout(link) for link in chain):
        return "timeout"
    return "connection_failed"


def _body_failure(exc: BaseException) -> str:
    """A fixed word for a failure after the status was read: KMS answered, but its body broke."""
    undecodable = (requests.exceptions.ContentDecodingError, urllib3.exceptions.DecodeError)
    return "bad_encoding" if any(isinstance(link, undecodable) for link in _exception_chain(exc)) else "bad_body"


def _verify_code(exc: BaseException) -> int | None:
    """OpenSSL's integer code of a failed certificate verification, or None."""
    for link in _exception_chain(exc):
        if isinstance(link, ssl.SSLCertVerificationError) and isinstance(getattr(link, "verify_code", None), int):
            return link.verify_code
    return None


# Errors whose messages are already fixed and secret-free; anything else is closed.
_OPEN_ERRORS = (KmsError, GlobScopeError, FileTooLargeError, SearchStopped)


def _closed_errors(method):
    """Let fixed errors through and turn any other exception into a fixed `server` error.

    The new error is raised after the ``except`` block, so neither
    ``__context__`` nor ``__cause__`` carries the original text (it may hold a
    URL, a body or a secret).
    """
    @functools.wraps(method)
    def wrapper(*args, **kwargs):
        unexpected = None
        try:
            return method(*args, **kwargs)
        except _OPEN_ERRORS:
            raise
        except Exception as exc:
            unexpected = type(exc).__name__
        # The class name only: the exception text may hold a URL or a secret.
        logger.warning("kms_lighthouse %s failed unexpectedly: %s", method.__name__, unexpected)
        raise KmsHTTPError(0, "server")
    return wrapper


def _flag(value) -> bool:
    """A boolean setting; an unvalidated config may carry it as text."""
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def _text(value) -> str | None:
    """A non-blank string setting, or None."""
    return value if isinstance(value, str) and value.strip() else None


def _kind_for_status(status: int) -> str:
    if status == 403:
        return "forbidden"
    if status == 404:
        return "not_found"
    if status == 429:
        return "throttled"
    if 300 <= status < 400:
        return "redirect"
    if 400 <= status < 500:
        return "bad_request"
    return "server"


def _session_lost(status: int) -> bool:
    """A userpass session is gone: KMS answers 401 or redirects to its login page."""
    return status == 401 or 300 <= status < 400


def _clean_text(value, limit: int = 200) -> str:
    text = "".join(ch for ch in str(value) if ch.isprintable())
    return " ".join(text.split())[:limit]


# ------------------------------------------------------------------ content helpers (pure)
_MAX_QUERY_CHARS = 200        # a URL-length guard: the PDF states no query limit
_SEARCH_PAGE_ROWS = 50
_MAX_SEARCH_PAGES = 20
_SNIPPET_CHARS = 300
_MAX_SEGMENT_CHARS = 200
_KINDS = ("item", "scenario", "folder")
# ASCII case only: a prefix that matches only by a Unicode case fold (a long s U+017F for 's',
# a dotless i U+0131 or a dotted capital I U+0130 for 'i') names no kind.
_KIND_PREFIX_RE = re.compile(r"^(item|scenario|folder):", re.IGNORECASE | re.ASCII)
# Every pattern below is linear: possessive runs never backtrack, and every
# Python scan is bounded. Titles and descriptions are scrubbed at full length
# (a cut before the scrub could split a secret), and so are article bodies.
# A JWT is any base64url run that starts like a JWT header ("eyJ"), with up to
# two dotted parts; whitespace (left by a <br>) may sit next to a dot.
_JWT_RE = re.compile(r"eyJ[A-Za-z0-9_-]{4,}+(?:\s{0,3}\.\s{0,3}[A-Za-z0-9_-]++){0,2}")
# A quote may open the token ('--oauth2-bearer "ya29.X"'): an ASCII, backtick, smart
# double or single quote (also a closing one used as an opener, and the low-9 ones), a
# single or double guillemet either way round, also escaped ('Bearer \"X\"'), or one of
# '(', '[' and '{'; it stays with the scheme word, and so does a ':' right after it
# ("Bearer: X"). Not openers (a documented limitation): '<' (an HTML tag), a CJK
# corner bracket and '*'.
_BEARER_OPENERS = "\"'`([{" + "".join(chr(code) for code in (
    0x201C, 0x201D, 0x201E, 0x2018, 0x2019, 0x201A, 0x00AB, 0x00BB, 0x2039, 0x203A))
# The gap after "Bearer" or "Bearer:" stays on its line, so a YAML key on the next
# line keeps its value; a "bearer:" key may have its token on the next line, indented
# or not (plain text drops the indent), and ``_redact_bearer`` keeps that value when a
# ':' follows it (it is a key). A blank is any whitespace but '\r' and '\n', so a Unicode
# space ("&ensp;", a thin or an ideographic space) is a gap too.
_BEARER_RE = re.compile(r"(?i)\b(bearer(?::[^\S\r\n]*+(?:\r?+\n[^\S\r\n]*+)?+|[^\S\r\n]++)(?:\\?+["
                        + re.escape(_BEARER_OPENERS) + r"])?+)([A-Za-z0-9._~+/=-]{8,}+)")
_KEY_AFTER_RE = re.compile(r"[^\S\r\n]*+:")
_BASIC_RE = re.compile(r"(?i)\b(basic\s++)([A-Za-z0-9+/]{12,}+=*+)")
# Each opening quote of a structured key or value and its closing quote: ASCII
# quotes and the matched smart pairs (built with chr() so the source stays ASCII).
_QUOTE_CLOSERS = {'"': '"', "'": "'", chr(0x201C): chr(0x201D), chr(0x2018): chr(0x2019)}
_ASCII_QUOTE_CLOSERS = {'"': '"', "'": "'"}
# A structured key and its ':' or '=' (JSON, YAML, a config or env line): a quoted
# key (groups 1-3: opening quote, name, closing quote; '"newPassword":',
# "'api_key':", and JSON escaped inside a string, '\"password\":', with a
# backslash before the closing quote) or a bare one (group 4) that starts after no
# key character. Only blanks may follow the separator, so a heading with its text
# on the next line has no value.
_STRUCTURED_NAME_RE = re.compile(
    "(?:([" + re.escape("".join(_QUOTE_CLOSERS)) + "])([A-Za-z0-9_-]++)\\\\?+(["
    + re.escape("".join(dict.fromkeys(_QUOTE_CLOSERS.values()))) + "])"
    r"|(?<![A-Za-z0-9_-])([A-Za-z0-9_-]++))[ \t]*+[:=][ \t]*+")
# An unquoted structured value: a bracketed run ("[abc]", "[redacted]") or the run
# up to whitespace, ',', '}', ']', '&' or ';'.
_STRUCTURED_VALUE_RE = re.compile(r"\[[^\s,}\]&;]*+\]|[^\s,}\]&;]*+")
_VALUE_GAP_RE = re.compile(r"[ \t]*+")       # never fails: the blanks between a scheme word and its credential
_SCHEME_WORDS = frozenset(("bearer", "basic", "token"))
_REDACTED = "[redacted]"
# Identifier shapes: two lower-case letters before an inner capital (apiToken,
# clientSecret), an acronym before a capitalised word (APIKey, APIToken), and a
# dotted config prefix of two or more characters right before a name that starts
# in lower case (db.password, spring.datasource.password, app.apiKey; not
# "e.g.password" or "Reset it.Password").
_CAMEL_CASE_RE = re.compile(r"[a-z]{2}[A-Z]")
_ACRONYM_CAMEL_RE = re.compile(r"[A-Z]{3}[a-z]")
_DOTTED_PREFIX_RE = re.compile(r"[A-Za-z0-9_]{2}\.")
# The authority of "scheme://user:password@host": the password runs up to the
# last '@' (it may hold a raw '@'). A scheme starts after any non-letter; it is
# at most 32 characters long, so each start reads a bounded run.
_USERINFO_RE = re.compile(r"(?i)(?<![a-z])[a-z][a-z0-9+.-]{0,31}+://[^\s/?#@:\"'<>]*+:([^\s/?#\"'<>]*+)")
# A '=' or '&' still written as a character reference, a named "&equals;" or a JSON
# escape. A numeric reference may lack its ';' when no digit (or hex digit) follows;
# a named one needs it, so "&section=2" and "&notifications" stay.
_ESCAPED_SEPARATOR_RE = re.compile(
    r"(?i)&(?:amp;)*+(?:#(?:0*+(61|38)(?![0-9])|x0*+(3d|26)(?![0-9a-f]));?|(equals);)|\\u00(3d|26)")
# "name=" followed by whitespace or a quote. The whitespace must not lead to
# another separator: "?q= &token= x" is an empty "q", then "token".
_SPACED_NAME_RE = re.compile(r"([?#;&][^\s\"'<>=?#;&]++=)(?:\s++(?![?#;&])|(?=[\"']))")
_UNQUOTED_VALUE_RE = re.compile(r"[^\s\"'<>]*+")
_MAX_QUOTED_CHARS = 256       # a quote not closed within this many characters on its line is no quote
# A parameter list: from any separator up to whitespace, a quote or a tag bracket.
_PARAMS_RE = re.compile(r"[?#;&][^\s\"'<>]*+")
_PARAM_SEPARATOR_RE = re.compile(r"([?#;&])")
_PARAM_NAME_NOISE_RE = re.compile(r"[-_\[\]]")
_AMP_PREFIXES_RE = re.compile(r"(?:amp;)*+")      # never fails: the run of "amp;" that starts a parameter name
_MAX_SCRUB_DEPTH = 4          # rounds of percent-decoding a nested value gets
_DAY_FIRST_RE = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})$")


def _param_key(name: str) -> str:
    """A parameter name as compared: lower case, no "amp;" left from unescaping, no '_', '-' or '[]'."""
    key = name.strip().lower()
    key = key[_AMP_PREFIXES_RE.match(key).end():]      # one slice, however long the run
    return _PARAM_NAME_NOISE_RE.sub("", key)


_SECRET_PARAMS = frozenset(_param_key(name) for name in (
    "token", "access_token", "id_token", "refresh_token", "jwt", "auth", "password", "passwd", "pwd",
    "api_key", "apikey", "key", "secret", "sig", "signature", "code", "jsessionid", "session", "sessionid",
    "x-amz-security-token", "x-amz-signature", "x-amz-credential",
    "client_secret", "auth_token", "private_token", "access_key", "ticket"))
# A compound or camel-case name ending with one of these is secret too (newPassword, apiToken, secret_key).
_SECRET_SUFFIXES = ("token", "password", "passwd", "pwd", "secret", "apikey", "accesskey", "privatekey",
                    "authkey", "secretkey", "sessionid", "subscriptionkey")
# Structured keys (JSON, YAML, config, env) have their own, shorter list: "code",
# "key", "auth", "sig", "signature", "session" and "ticket" name a secret only as
# URL parameters ('{"code": "E1001"}', '{"key": "Enter"}' are content).
_SECRET_KEYS = frozenset(_param_key(name) for name in (
    "token", "access_token", "id_token", "refresh_token", "auth_token", "private_token", "jwt", "password",
    "passwd", "pwd", "api_key", "secret", "client_secret", "access_key", "jsessionid", "sessionid",
    "x-amz-security-token", "x-amz-signature", "x-amz-credential"))
# A structured key ending with "pass" after a separator ('_', '-' or a camel-case
# capital: DB_PASS, db-pass, dbPass) and a word that names a service; never the
# word alone ("Pass:"), "bypass", or another word ("boarding_pass", "LastPass").
_COMPOUND_PASS_RE = re.compile(r"(?:[A-Z]?[a-z0-9]+|[A-Z0-9]+)(?=(?:[_-](?i:pass)|(?<=[a-z0-9])Pass)$)")
# The words before "pass" that name a service or an account in a lower-case or
# camel-case key ("admin_pass", "emailPass"); an upper-case env key needs none.
_PASS_PREFIXES = ("db", "smtp", "ftp", "sftp", "ldap", "mysql", "redis", "proxy", "mail", "imap", "root",
                  "postgres", "pg", "mongo", "mongodb", "rabbitmq", "amqp", "email", "ssh", "vpn", "wifi",
                  "keystore", "database", "sql", "sa", "app", "api", "admin")
# The words of an upper-case env key before its "_PASS" (ADMIN, DB_ADMIN, LDAP_BIND).
_UPPER_ENV_WORDS_RE = re.compile(r"[A-Z0-9]++(?:_[A-Z0-9]++)*+")
# Two words a hyphen may join into a secret key name (auth-token, client-secret, api-key, db-pass).
_COMPOUND_SECRET_KEYS = frozenset(key for key in _SECRET_KEYS | set(_SECRET_SUFFIXES) if key not in (
    "token", "password", "passwd", "pwd", "secret", "jwt", "jsessionid")) | {word + "pass" for word in _PASS_PREFIXES}


def _is_listed(key: str, names: frozenset[str]) -> bool:
    return key in names or key.endswith(_SECRET_SUFFIXES)


def _is_secret_name(name: str) -> bool:
    """A URL parameter that names a credential: a listed name, or one ending with a secret word.

    A name may still carry the "amp" of an "&amp" written without its ';'
    ("?id=1&ampsig=X"); it is secret when the rest is ("&amplitude=5" stays).
    """
    key = _param_key(name)
    return _is_listed(key, _SECRET_PARAMS) or (key.startswith("amp") and _is_listed(key[3:], _SECRET_PARAMS))


def _is_title_label(name: str) -> bool:
    """A hyphenated name with each part capitalised and not all in capitals ("Root-Pass", "Mail-Pass") is a label."""
    return "-" in name and name != name.upper() and all(part[:1].isupper() for part in name.split("-"))


def _is_pass_key(name: str) -> bool:
    """A "...pass" key: any upper-case env key ("ADMIN_PASS"), or a service word before "pass" ("db-pass", "smtpPass")."""
    if name.endswith("_PASS") and _UPPER_ENV_WORDS_RE.fullmatch(name, 0, len(name) - len("_PASS")):
        return True
    if _is_title_label(name):
        return False
    word = _COMPOUND_PASS_RE.search(name[-40:])      # bounded: the word before "pass" is short
    return word is not None and word.group(0).lower() in _PASS_PREFIXES


# A documentation placeholder in a credential's place ("<password>", "{secret}",
# "[api key]", "***", "xxxx", "....", "changeme", "your_password", "YOUR_API_KEY",
# "MY_SECRET", "TOKEN_HERE", "your_api_key_here", "YOUR_API_KEY_GOES_HERE", "ACCESS_TOKEN",
# an upper-case env name with no digit ending _TOKEN, _KEY, _SECRET or _PAT ("AUTH_TOKEN",
# "SLACK_BOT_TOKEN"), up to three words after "your", "my", "insert" or "replace with"
# ("YOUR_CLIENT_SECRET", "your-api-key", "INSERT_KEY_HERE", "REPLACE_WITH_TOKEN", "YOUR API
# KEY"), a variable: "${DB_PASS}", "${PG_PASSWORD:-postgres}" (a default of letters and '-'
# only, or a word placeholder), "$DB_PASSWORD", "%API_KEY%", and a template that is a
# reference, with bare filters after it: a dotted path ("{{ .Values.apiKey | quote }}",
# "${{ secrets.API_KEY }}") or one identifier with no digit ("{{apiKey}}")): kept as written.
_BRACKETED_PLACEHOLDER = r"<[a-z][a-z _-]{0,63}+>|\{[a-z][a-z _-]{0,63}+\}|\[[a-z][a-z _-]{0,63}+\]"
# How far an unquoted "{{ ... }}" reaches (it may hold blanks); no ':' or '=', so no key hides inside.
_TEMPLATE_SPAN = r"\$?+\{\{[^{}\n:=]{1,60}+\}\}"
# A template reference: a dotted path whose segments hold no digit, or any dotted path under a known
# root (".Values.image2", "steps.login2.outputs.token"; "sk_live_51Hx.x" is none), or one identifier with
# no digit; then a chain of bare filters ("| quote", "| b64enc | quote"): letters and '_', or a listed
# filter with a digit.
_TEMPLATE_ROOTS = r"(?:values|secrets|env|vars|inputs|steps|matrix|github|chart|release)"
_TEMPLATE_FILTER = r"(?:(?:b64enc|b64dec|b32enc|b32dec|sha1sum|sha256sum|adler32sum)(?![a-z0-9_])|[a-z_]{1,20}+)"
_TEMPLATE_PLACEHOLDER = (r"\$?+\{\{[ \t]*+(?:\.?+" + _TEMPLATE_ROOTS + r"(?:\.[a-z_][a-z0-9_]*+)++"
                         r"|\.?+[a-z_]++(?:\.[a-z_]++)++|[a-z_]++)"
                         r"(?:[ \t]*+\|[ \t]*+" + _TEMPLATE_FILTER + r")*+[ \t]*+\}\}")
# The spans that may hold blanks, so an unquoted value run (it stops at a blank) cannot see them whole.
_SPACED_PLACEHOLDER_RE = re.compile("(?i)" + _BRACKETED_PLACEHOLDER + "|" + _TEMPLATE_SPAN)
# The word placeholders; also a variable's default may be one ("${OPENAI_API_KEY:-your_api_key_here}").
# An env name in capitals ending _TOKEN, _KEY, _SECRET or _PAT, with no digit, is one: 2 to 20 letters
# a word, up to four words before the suffix ("SLACK_BOT_TOKEN", "OPENAI_API_KEY", "GITLAB_PAT").
_WORD_PLACEHOLDER = (
    r"\*{3,}+|x{3,}+|\.{3,}+"
    r"|(?:your_?)?password|pass|passwd|secret|token|changeme"
    r"|(?:(?:your|my)_)?(?:(?:api|access)_)?(?:key|token|secret|password|pass|pwd)(?:(?:_goes)?_here)?"
    r"|(?:your|my|insert|replace[_ -]with)[_ -][a-z]++(?:[_ -][a-z]++){0,2}"
    # The word count may give back a word for the suffix (at most four tries, on a bounded value).
    r"|(?-i:[A-Z]{2,20}+(?:_[A-Z]{2,20}+){0,3}_(?:TOKEN|KEY|SECRET|PAT))")
# A variable's default is an allow-list: letters and '-' ("postgres", "changeme", "dev-secret"), or a
# word placeholder; any other character ("Ch@ngeMe!", "Qx.Lm-Rt@Kp", "Xk29") makes it a value.
_PLACEHOLDER_RE = re.compile(
    "(?i)" + _BRACKETED_PLACEHOLDER + "|" + _TEMPLATE_PLACEHOLDER + "|" + _WORD_PLACEHOLDER
    + r"|\$\{[a-z_][a-z0-9_]{0,61}+(?::?[-=?+](?:[a-z-]{0,40}+|" + _WORD_PLACEHOLDER + r"))?\}"
    r"|\$[a-z_][a-z0-9_]{0,63}+|%[a-z_][a-z0-9_]{0,63}+%")
_MAX_PLACEHOLDER_CHARS = 66
# What may end a placeholder: up to two sentence marks ("api_key: YOUR_API_KEY.)"), or, when the
# value did not open with a quote, one closing quote with a mark before or after it
# ("curl -H 'X-Api-Key: YOUR_API_KEY'", "Send `API_KEY=your_api_key_here`."); either may end
# with a shell line continuation ('curl -H "X-Api-Key: YOUR_API_KEY"\').
_MAX_ENDING_CHARS = 4
_CLOSING_QUOTES = "\"'`" + chr(0x201D) + chr(0x2019)
_ENDING_RE = re.compile(r"[.)!]{0,2}+\\?+")
_QUOTED_ENDING_RE = re.compile(r"[.)!]?+[" + re.escape(_CLOSING_QUOTES) + r"][.)!]?+\\?+")
# A value that is a URL ("proxy_pass: http://backend:8080" names an upstream, not a password).
_URL_VALUE_RE = re.compile(r"(?i)(?:https?|ftp|wss?)://")


def _is_ending(text: str, quote: bool = False) -> bool:
    """What may follow a placeholder: nothing, one or two sentence marks, or (``quote``) a closing quote."""
    return _ENDING_RE.fullmatch(text) is not None or (quote and _QUOTED_ENDING_RE.fullmatch(text) is not None)


def _is_placeholder(value: str, quote: bool = False) -> bool:
    """A placeholder, also with an ending after it (``_is_ending``); an earlier "[redacted]" is none.

    (A scheme word before an earlier "[redacted]" still goes with it.) Bounded:
    at most five full matches over at most 70 characters.
    """
    if value == _REDACTED or len(value) > _MAX_PLACEHOLDER_CHARS + _MAX_ENDING_CHARS:
        return False
    for cut in range(min(_MAX_ENDING_CHARS, len(value) - 1) + 1):
        end = len(value) - cut
        if _is_ending(value[end:], quote) and _PLACEHOLDER_RE.fullmatch(value, 0, end) is not None:
            return True
    return False


def _without_controls(text: str) -> str:
    """Control, format and surrogate characters become spaces; whitespace is collapsed."""
    return " ".join("".join(" " if unicodedata.category(ch)[0] == "C" else ch for ch in text).split())


def _is_invisible(ch: str) -> bool:
    """Any control, format, private-use, unassigned or surrogate character except whitespace.

    The same set ``_without_controls`` turns into spaces, so nothing that
    survives the scrub can later become a space that rebuilds a split secret.
    """
    return unicodedata.category(ch)[0] == "C" and not ch.isspace()


_JOINERS = frozenset("\u200c\u200d")               # ZWNJ and ZWJ
_JOINER_RUN_RE = re.compile("[\u200c\u200d]++")


def _joinable(ch: str) -> bool:
    return bool(ch) and not ch.isascii() and not ch.isspace()


def _keep_joiners(match: re.Match) -> str:
    """A joiner run stays only between two non-ASCII characters (a Persian word, an emoji)."""
    text, start, end = match.string, match.start(), match.end()
    before = text[start - 1] if start else ""
    after = text[end] if end < len(text) else ""
    return match.group(0) if _joinable(before) and _joinable(after) else ""


# Default-ignorable characters outside the C categories (letters and marks that
# render as nothing), and the variation selectors among them.
def _char_class(*ranges: tuple[int, int]) -> str:
    return "[" + "".join(f"{chr(low)}-{chr(high)}" for low, high in ranges) + "]"


_IGNORABLE_RUN_RE = re.compile(_char_class(
    (0x034F, 0x034F), (0x115F, 0x1160), (0x17B4, 0x17B5), (0x180B, 0x180F), (0x3164, 0x3164),
    (0xFE00, 0xFE0F), (0xFFA0, 0xFFA0), (0xE0000, 0xE0FFF)) + "++")
_SELECTOR_RE = re.compile(_char_class((0x180B, 0x180D), (0x180F, 0x180F), (0xFE00, 0xFE0F), (0xE0100, 0xE01EF)))


def _keep_selectors(match: re.Match) -> str:
    """Only variation selectors stay, and only right after a non-ASCII character (an emoji or an ideograph)."""
    start = match.start()
    before = match.string[start - 1] if start else ""
    if before in _JOINERS or not _joinable(before):
        return ""
    return "".join(_SELECTOR_RE.findall(match.group(0)))


def _drop_invisible(text: str) -> str:
    """Drop every invisible character, so none can split a secret; a joiner or a selector that shapes a word stays."""
    text = "".join(ch for ch in text if ch in _JOINERS or not _is_invisible(ch))
    text = _IGNORABLE_RUN_RE.sub(_keep_selectors, text)
    return _JOINER_RUN_RE.sub(_keep_joiners, text)


def _unescape_separator(match: re.Match) -> str:
    code = next(group for group in match.groups() if group).lower()
    return "=" if code in ("61", "3d", "equals") else "&"


def _scrub_part(part: str, depth: int) -> str | None:
    """One ``name=value`` part: None when it is secret, else the part (scrubbed if it decodes)."""
    decoded = unquote(part)
    name, equals, _value = decoded.partition("=")
    if equals and _is_secret_name(name):
        return None
    if decoded == part:
        return part
    if depth >= _MAX_SCRUB_DEPTH:
        return None      # still encoded after every decoding round: dropped, not kept unread
    # It decodes to something else (an encoded '=', '&' or a nested URL): scrub
    # the decoded form as a parameter list of its own.
    cleaned = _scrub_at("&" + decoded, depth + 1)
    if cleaned == "&" + decoded:
        return part
    cleaned = cleaned[1:] if cleaned.startswith("&") else cleaned
    return cleaned or None


def _scrub_params(params: str, depth: int) -> str:
    """Drop the secret parts of a parameter list that starts with a separator."""
    pieces = _PARAM_SEPARATOR_RE.split(params)      # ["", sep, part, sep, part, ...]
    kept = []
    for separator, part in zip(pieces[1::2], pieces[2::2], strict=True):
        cleaned = _scrub_part(part, depth)
        if cleaned is not None:
            kept.append(separator + cleaned)
    text = "".join(kept)
    if text and params[0] in "?#" and text[0] != params[0]:
        text = params[0] + text[1:]      # the URL keeps its "?" or "#"
    return text


def _value_span(text: str, start: int, unquoted: re.Pattern,
                closers: dict[str, str] = _ASCII_QUOTE_CLOSERS) -> tuple[int, int, bool]:
    """``(value start, value end, quoted)`` for the value at ``start``.

    A quote (a key of ``closers``) closed within ``_MAX_QUOTED_CHARS`` on the
    same line holds the value; an unclosed one does not hide the rest of the
    text: the value is then the run right after it.
    """
    if start < len(text) and text[start] in closers:
        close = text.find(closers[text[start]], start + 1, start + 1 + _MAX_QUOTED_CHARS)
        if close >= 0 and "\n" not in text[start + 1:close]:
            return start + 1, close, True
        start += 1
    return start, unquoted.match(text, start).end(), False


def _drop_spaced_values(text: str) -> str:
    """``?name= value`` or ``?name='value'``: a secret name loses its value (the bare part is dropped next).

    Any other name keeps what follows it, which is scanned again, so it cannot
    hide a secret name behind it.
    """
    pieces, last, pos = [], 0, 0
    while (match := _SPACED_NAME_RE.search(text, pos)) is not None:
        if not _is_secret_name(unquote(match.group(1)[1:-1])):
            pos = match.end(1)
            continue
        _value_start, end, quoted = _value_span(text, match.end(), _UNQUOTED_VALUE_RE)
        pieces.append(text[last:match.end(1)])
        last = pos = end + 1 if quoted else end       # a closing quote goes with the value
    pieces.append(text[last:])
    return "".join(pieces)


def _is_identifier_shaped(text: str, start: int, name: str) -> bool:
    """The name at ``text[start:]`` is a machine key, never a plain word or a label.

    ``api_key``, ``clientSecret``, ``APIKey``, ``db.password``, ``--password``,
    ``x-api-key`` and ``auth-token`` are; "Password", "PASSWORD", "eToken",
    "e.g.password", "Top-Secret" and "One-Time-Password" are not. A hyphen
    makes a key only after "x-" or "--", or when it joins two words into a
    compound secret name.
    """
    if "_" in name or _CAMEL_CASE_RE.search(name) or _ACRONYM_CAMEL_RE.search(name):
        return True
    if start >= 3 and "a" <= name[0] <= "z" and _DOTTED_PREFIX_RE.match(text, start - 3):
        return True      # a capital after the dot is a sentence boundary ("Reset it.Password:")
    if "-" not in name:
        return False
    lower = name.lower()
    if lower.startswith(("x-", "--")):
        return True
    words = [word for word in lower.split("-") if word]
    return len(words) >= 2 and words[-2] + words[-1] in _COMPOUND_SECRET_KEYS


def _at_key_start(text: str, start: int) -> bool:
    """A quoted key at ``start`` begins a line (after blanks) or follows '{', ',' or '[' (also escaped: '{\\"')."""
    i = start - 1
    if i >= 0 and text[i] == "\\":
        i -= 1
    while i >= 0 and text[i] in " \t":
        i -= 1
    return i < 0 or text[i] in "\n\r{,["


def _unquoted_value_end(text: str, start: int) -> int:
    """The end of the unquoted value at ``start``; a "[redacted]" inside it does not cut it short."""
    end = _STRUCTURED_VALUE_RE.match(text, start).end()
    tail = len(_REDACTED) - 1                 # "[redacted" before the ']' that stopped the run
    while text.startswith("]", end) and end - start >= tail and text.startswith(_REDACTED[:-1], end - tail):
        end = _STRUCTURED_VALUE_RE.match(text, end + 1).end()
    return end


def _redact_structured_values(text: str) -> str:
    """The value of a secret structured key.

    The key is identifier-shaped (bare, or quoted anywhere), or a quoted plain
    word where a key starts (a line start, '{', ',' or '['); a plain word's
    value goes only when it is quoted. A quoted value (ASCII or smart quotes)
    closed on its line within ``_MAX_QUOTED_CHARS`` is redacted whole;
    otherwise the run up to whitespace, ',', '}', ']', '&' or ';' (a bracketed
    run and an earlier "[redacted]" inside the run are taken whole, so no
    bracket is left over); a scheme word ("Bearer", "Basic", "Token") goes
    with the run after it, and stays when no run follows on its line. A
    backslash may come before each quote (escaped JSON), and a quoted name
    whose quotes do not pair still counts when it is identifier-shaped or a
    listed key. Every scan goes on after the
    key (or after the redacted value), so the loop is linear.
    """
    pieces, last, pos = [], 0, 0
    while (match := _STRUCTURED_NAME_RE.search(text, pos)) is not None:
        pos = match.end()
        opener, quoted_name, closer, bare_name = match.group(1, 2, 3, 4)
        name = quoted_name if quoted_name is not None else bare_name
        listed = _is_listed(_param_key(name), _SECRET_KEYS)
        if not (listed or _is_pass_key(name)):
            continue
        shaped = _is_identifier_shaped(text, match.start(2 if quoted_name is not None else 4), name)
        if quoted_name is None and not shaped:
            continue
        if quoted_name is not None:
            # Quotes that do not pair (a smart opener, an ASCII closer) still mark a key whose name says it all.
            if closer != _QUOTE_CLOSERS[opener] and not (shaped or _param_key(name) in _SECRET_KEYS):
                continue
            if not (shaped or _at_key_start(text, match.start())):
                continue
        # JSON escaped inside a string: a backslash before each quote of the value.
        escaped = text.startswith("\\", pos) and text.startswith(tuple(_QUOTE_CLOSERS), pos + 1)
        value_at = pos + 1 if escaped else pos
        if not shaped and not text.startswith(tuple(_QUOTE_CLOSERS), value_at):
            continue                  # a plain word's unquoted value is prose ('"Password": your PIN')
        start, end, quoted = _value_span(text, value_at, _STRUCTURED_VALUE_RE, _QUOTE_CLOSERS)
        if quoted and escaped and end > start and text[end - 1] == "\\":
            end -= 1                  # the backslash of the closing quote stays with it
        credential_at, glued = start, False
        # A closing quote may end an unquoted placeholder ("curl -H 'X-Api-Key: YOUR_API_KEY'"), unless
        # the value itself opened with a quote that did not close.
        quote_tail = not quoted and start == value_at
        if not quoted:
            # A bracketed placeholder or a template may hold blanks ("api_key: <your api key>"); the match
            # is bounded. It is one only when nothing but an ending follows it; glued to a value
            # ("<your key>Sv1x"), or a template that is no reference ("{{ sk_live_51Hx }}"), the whole
            # run goes.
            spaced = None if text.startswith(_REDACTED, start) else _SPACED_PLACEHOLDER_RE.match(text, start)
            if spaced:
                end = _unquoted_value_end(text, spaced.end())
                if _is_ending(text[spaced.end():end], quote_tail) and _is_placeholder(spaced.group(0)):
                    pos = end         # a kept placeholder is not scanned again for a key inside it
                    continue
                glued = True
            else:
                end = _unquoted_value_end(text, start)
            if not glued and text[start:end].lower() in _SCHEME_WORDS:
                credential_at = _VALUE_GAP_RE.match(text, end).end()
                credential_end = _unquoted_value_end(text, credential_at)
                if credential_end == credential_at:
                    continue          # a scheme word with no credential after it is text ("X-Auth-Token: Bearer")
                end = credential_end
        # An unquoted "{placeholder" or "${VAR" stops before its '}'; a placeholder is short, so the check
        # reads a bounded run. A value glued after the '}' ("${DB}Sv1x") makes it no placeholder, and the
        # whole run goes.
        closer = ""
        if not quoted and not glued and text.startswith(("{", "${"), credential_at) and text.startswith("}", end):
            after = _unquoted_value_end(text, end + 1)
            if _is_ending(text[end + 1:after], quote_tail):
                closer = text[end:after]
            else:
                end, glued = after, True
        if (not glued and end - credential_at <= _MAX_PLACEHOLDER_CHARS + _MAX_ENDING_CHARS
                and _is_placeholder(text[credential_at:end] + closer, quote_tail)):
            pos = end + len(closer)   # "${PG_PASSWORD:-x}" holds no key of its own
            continue
        if closer:
            end += 1                  # "${PG_PASSWORD:-Xk29}" is no placeholder: its '}' goes with it
        if not listed and _URL_VALUE_RE.match(text, start):
            pos = end                 # a "pass" name whose value is a URL names an upstream ("proxy_pass: http://x")
            continue
        if end > start:
            pieces.append(text[last:start] + _REDACTED)
            last = pos = end
    pieces.append(text[last:])
    return "".join(pieces)


def _redact_userinfo(match: re.Match) -> str:
    """``scheme://user:password@host``: the password, up to the last '@' of the authority, is redacted."""
    authority = match.group(1)
    at = authority.rfind("@")
    if at < 0:
        return match.group(0)
    return match.group(0)[:match.start(1) - match.start()] + "[redacted]" + authority[at:]


# The last word of a Bearer placeholder names what goes there ("YOUR_ACCESS_TOKEN", "TOKEN_GOES_HERE").
_BEARER_PLACEHOLDER_ENDS = tuple(sep + word for sep in "_-" for word in ("token", "key", "secret", "jwt", "here"))
_CAPS_WORDS_RE = re.compile(r"[A-Z_]++")


def _redact_bearer(match: re.Match) -> str:
    value = match.group(2)
    if "\n" in match.group(1) and _KEY_AFTER_RE.match(match.string, match.end()):
        return match.group(0)             # "bearer:" then, on the next line, a key ("token_file: /etc/token")
    # A token has a digit or one of "_~+/="; a word ("Bearer authentication", "token-based") stays, and
    # so does a documentation placeholder with no digit that is written in capitals and '_' ("Bearer
    # YOUR_PAT", "Bearer SLACK_BOT_TOKEN") or, in lower or mixed case, whose last word names what goes
    # there ("Bearer my_token_here"; "Bearer my_token_abcdef" is a token).
    if not any(ch.isdigit() or ch in "_~+/=" for ch in value):
        return match.group(0)
    if not any(ch.isdigit() for ch in value) and _is_placeholder(value):
        core = value.rstrip(".")
        if _CAPS_WORDS_RE.fullmatch(core) or core.lower().endswith(_BEARER_PLACEHOLDER_ENDS):
            return match.group(0)
    return match.group(1) + "[redacted]"


def _is_userpass_base64(value: str) -> bool:
    """Base64 of printable "user:password" text, the only thing a Basic credential holds."""
    try:
        decoded = base64.b64decode(value, validate=True).decode("utf-8")
    except (binascii.Error, ValueError):
        return False
    return ":" in decoded and decoded.isprintable()


def _redact_basic(match: re.Match) -> str:
    value = match.group(2)
    # A digit, '+', '/' or '=' is base64; letters only must decode to "user:password"
    # ("Basic SuccessFactors setup", "Basic troubleshooting" stay).
    if any(ch.isdigit() or ch in "+/=" for ch in value) or _is_userpass_base64(value):
        return match.group(1) + "[redacted]"
    return match.group(0)


def _scrub_at(text: str, depth: int) -> str:
    text = _drop_invisible(text)
    text = _ESCAPED_SEPARATOR_RE.sub(_unescape_separator, text)
    text = _USERINFO_RE.sub(_redact_userinfo, text)
    text = _drop_spaced_values(text)
    text = _PARAMS_RE.sub(lambda m: _scrub_params(m.group(0), depth), text)
    text = _JWT_RE.sub("[redacted]", text)
    text = _BEARER_RE.sub(_redact_bearer, text)
    text = _BASIC_RE.sub(_redact_basic, text)
    # After the JWT, Bearer and Basic rules, so a scheme word is never taken as the
    # whole value ("X-Auth-Token: Bearer X"); their "[redacted]" is not redacted again.
    return _redact_structured_values(text)


# Scope (user decision A): only machine credentials are removed. A password, key
# or code that an author writes in plain prose ("Password: 123456", "Ticket:
# INC0012345", "the password is X") is content for its readers and is shown as
# written, as other BOW document connectors show it; so is a value after a
# plain-word label ("password=X", "Token: X", "PASSWORD: X", '"Password": your PIN',
# "Top-Secret: X", "One-Time-Password: X", "e.g.password: X").
# Not handled (a documented limitation; defence in depth, since the text is
# already visible to the identity that reads it; P10 docs repeat this list):
#   * XML forms ('<add key="ApiKey" value="X"/>', "<password>X</password>");
#   * a non-ASCII space (no-break, ideographic) between a separator and its value;
#   * an all-caps single-word key ("PGPASSWORD=X", "JSESSIONID: X"), a plain
#     lower-case one ("sessionid: X"), and "Authorization: Token X";
#   * a Bearer value of letters, '.' and '-' only, unless a structured secret
#     key comes before the scheme word ("X-Auth-Token: Bearer X" is redacted);
#   * a secret cut in two by a line break or a block tag: the part after the
#     break is shown (a JWT's payload and signature still start a JWT of their
#     own, so only the rest of a cut header shows); also a secret split by a
#     closing then an opening inline tag ("ab</span><span>cd" reads as two
#     words, as "Home</a><a>Contact" must);
#   * fullwidth or lookalike names and fullwidth dots, and a bare secret with no
#     name, separator or scheme next to it;
#   * JWTs whose header does not start with "eyJ";
#   * authored prose secrets, by design (user decision A, above);
#   * an unquoted structured value that holds whitespace, ',', ';', '&', '}' or
#     ']' (only the run before it goes), and a quoted value whose closing quote
#     is more than 256 characters away or on another line (the run after the
#     opening quote is taken instead);
#   * a numeric '=' or '&' reference without ';' that a digit (or a hex digit)
#     follows ("&#x3dab12": HTML reads it as another character);
#   * a URL scheme longer than 32 characters;
#   * credentials inside command examples (curl -u/--user/-U/--proxy-user, wget
#     --password, mysql -p and other tools' flags) are authored text and are
#     shown as written (decision A, 2026-11-05); so is a scheme-less proxy
#     userinfo ("curl -x user:pass@proxy:3128"). A header, a URL password, a
#     secret parameter or a "--...password=" key inside a command still goes by
#     its own rule ("curl -H 'Authorization: Bearer X'", "wget --password=X");
#   * a token-only URL userinfo ("https://TOKEN@host", no ':'), and a userinfo
#     whose user name is split from its ':' by a tag ("https://<b>admin</b>:<b>S</b>@host",
#     "https://admin<b>:S</b>@host" read "https:// admin :S@host" and "https://admin
#     :S@host", which no longer look like a URL userinfo);
#   * a label element that closes right after a ':' with the value straight
#     after it ("<b>https://admin:</b>S@host" reads "https://admin: S@host",
#     as "<b>Note:</b>Use the portal" must read "Note: Use the portal");
#   * a "<word>pass" key in lower or camel case unless the word right before
#     "pass" (after '_', '-' or a lower-to-upper camel boundary) names a service
#     or an account (_PASS_PREFIXES: db-pass, dbPass, smtp_pass, adminPass,
#     emailPass); an upper-case env key ending "_PASS" is always one
#     (ADMIN_PASS, DB_ADMIN_PASS, LDAP_BIND_PASS). Shown as written:
#     "dbUserPass: X", "boardingPass", "LastPass", an upper-case run before
#     "Pass" ("DBPass", no camel boundary), and a Title-Hyphen label, each part
#     capitalised and not all in capitals ("Root-Pass: X", "User-Pass",
#     "Admin-Pass", also "DB-Pass: X"; "db-pass" and "DB-PASS" stay keys); the
#     URL value of a "pass" key ("proxy_pass: http://backend:8080"; a password
#     inside its userinfo still goes);
#   * a structured value that is a documentation placeholder ("api_key: <your
#     api key>", '"password": "<password>"', "DB_PASSWORD=changeme", "api_key:
#     YOUR_API_KEY", "API_KEY=your_api_key_here", "DB_PASS=${DB_PASS}",
#     "DB_PASSWORD=$DB_PASSWORD", "set API_KEY=%API_KEY%", "Bearer YOUR_ACCESS_TOKEN",
#     "client_secret: YOUR_CLIENT_SECRET", "api_key: {{ .Values.apiKey }}",
#     "DB_PASSWORD=${PG_PASSWORD:-postgres}", "${OPENAI_API_KEY:-your_api_key_here}",
#     "api_key: {{ .Values.apiKey | b64enc }}", "Bearer SLACK_BOT_TOKEN", "api_key:
#     STRIPE_SECRET_KEY", also with one or two of '.', ')' or '!' after it, and,
#     unless the value opened with a quote, a closing quote, either then a shell
#     line continuation '\': 'curl -H "X-Api-Key: YOUR_API_KEY"\'); a real value of
#     the same shape is shown too ("db_password: $ecret1", "api_key:
#     your_secretvalue", a default of letters and '-' only "${PG_PASSWORD:-hunter}",
#     a digit-free dotted template path "{{ a.Secretvalue }}", any path under a known
#     root "{{ secrets.X1 }}", a letters-only filter "{{ .Values.k | Secretword }}",
#     a capital env name "Bearer ABCDEFGH_TOKEN"). A default with any other
#     character ("${PG_PASSWORD:-Xk29}", "${DB_PASS:-Ch@ngeMe!}") that is not a word
#     placeholder itself, a template that is no reference ("{{ sk_live_51Hx }}",
#     "{{ sk_live_51Hx.x }}", "{{ .Values.k | Sv1x9 }}"), and a placeholder glued to
#     a value ("api_key: <your key>S", "DB_PASS=${DB}S") are none: the run goes;
#   * a Bearer token opened by '<' (as an entity, "Bearer &lt;S": an HTML tag
#     otherwise), by a CJK corner bracket or by '*';
#   * a quoted plain-word key in prose before a variable with a default ('see
#     "password": "${DB_PASS:-none}"', not at a key start) reads the variable's
#     own name as a key, so its default goes (over-redaction, crafted);
#   * shell quoting after an unquoted structured value: "curl -H 'X-Api-Key: S'"
#     loses the closing quote with the value;
#   * a Bearer token on the line after a "Bearer" with no ':' ("Bearer" then a
#     line break, then the token), and a "bearer:" key whose next-line value has
#     a ':' after it (read as a YAML key: "bearer:\n  token_file: /etc/token");
#   * a path under a known template root, kept even when a segment is shaped
#     like a key ("{{ secrets.sk_live_51Hx }}");
#   * a letters-only placeholder keep has no bound on its total length;
#   * a doubled opener or a spaced colon after the scheme word ("Bearer ((S))",
#     "Bearer : S");
#   * a value glued right after a closing quote ('"YOUR_API_KEY"S');
#   * (not a scrub rule; listed for the P10 docs) ``_list_cap`` reads a few
#     limits unlike pydantic would (True, Unicode digits); no impact, since the
#     shared list_files tool never passes a limit.
# Over-redaction accepted: a camel-case word starting "eyJ" ("eyJumpsMonkey"), an
# identifier-shaped quoted key in prose ('the "api_key": field'), parameter names
# that end with a secret word ("nextPageToken", "showPassword"), and these
# crafted shapes: a dotted identifier-shaped key in prose ("etc.password: see
# below" loses "see"); a word after a scheme-word value ("x_token: Token based
# login" loses "based"; a scheme word with nothing after it on its line stays);
# bracket debris inside a value ("(api_key=S)" loses the ')', "api_key:
# Sv1[x9Qk2]abc" leaves "]abc" after the redaction); a stray ']' after a YAML flow
# list value; "Subscription-Key: found on your account page" loses "found"; an
# unquoted placeholder with blanks and no brackets ("api_key: YOUR API KEY" loses
# "YOUR"; quoted, it stays).
# Joined as the page shows them: an inline tag that opens right after ':' or '='
# ("Note:<b>Read this</b>" reads "Note:Read this"; "Note: <b>Read</b>" keeps its
# space), and inline tags right before '@' ("Mail <b>me</b>@home" reads "Mail
# me@home").
# A ZWNJ or ZWJ kept inside a word (a format character) becomes a space in a title
# or a snippet (``_without_controls``), not in an article body; a variation
# selector is a mark (Mn), so it stays everywhere.
def _scrub(text: str) -> str:
    """Drop secret-named URL parameters and redact JWTs and other machine credentials (pure, linear time).

    Run on plain text (``_html_to_text`` output), so entity-encoded and
    plain-text URLs are caught too. Every invisible character (control,
    format, private-use, unassigned, surrogate, and the default-ignorable
    letters and marks such as U+3164 or U+034F) is dropped first, so none can
    split a secret; whitespace is kept, a ZWNJ/ZWJ between two non-ASCII
    characters stays, and so does a variation selector right after one. A '='
    or '&' still written as ``&#61;``/``&#38;``/``&equals;`` (a numeric one
    also without its ';') or as a JSON ``\\u003d``/``\\u0026`` escape is
    decoded. Query strings, fragments and ';' path parameters are split on '&'
    and ';'; each part is percent-decoded before its name is compared, a part
    that decodes to something else (a nested URL) is scrubbed again in decoded
    form, and one still encoded after ``_MAX_SCRUB_DEPTH`` rounds is dropped.
    A parameter name is secret when, in lower case and without '_', '-', '[]'
    and a leading "amp;" (or a bare "amp"), it is on the list or ends with a
    secret word (``newPassword``, ``apiToken``, ``aws_secret_access_key``). A
    secret name followed by '=' and whitespace or a quote loses the next run
    or the quoted text. URL passwords (``user:...@``, up to the last '@'),
    Bearer tokens (with a digit or one of ``_~+/=``) and Basic credentials
    (base64 with a digit, '+', '/' or '=', or letters that decode to
    ``user:password``) are redacted next; a quote may open the Bearer token.
    Command-line flags ("curl -u user:pass") are authored text and are not
    read (decision A, 2026-11-05). Last, the value of a structured secret key (JSON, YAML,
    config or env; its own, shorter name list, plus an upper-case env name
    ending "_PASS" and a compound name ending with "pass" after a separator and
    a service word) is redacted when the key is identifier-shaped
    (``api_key=``, ``clientSecret:``, ``APIKey:``, ``db.password=``,
    ``x-api-key:``, ``DB_PASS=``), or a quoted plain word where a key starts
    with a quoted value (``{"password": "..."}``); JSON escaped inside a string
    (a backslash before each quote) and quotes that do not pair around a
    self-evident or identifier-shaped name count as quotes. A scheme word as
    the value goes with its credential (alone, it stays), an earlier
    "[redacted]" is not redacted again, and a placeholder value (or the URL
    value of a "pass" key) stays.
    """
    return _scrub_at(text, 0)


# Inline tags never break a line. A run of them inside a token (base64url
# characters on both sides) is removed outright, so it cannot split the token;
# between two elements (a closing, then an opening tag) or next to anything
# else it becomes a space, so words stay apart. A run that opens an element
# right after ':' or '=', or ends right before '@', is removed too, so a
# structured value or a URL userinfo reads as one ("api_key:<b>S</b>").
_INLINE_TAG_NAMES = ("wbr|span|b|i|u|em|strong|font|a|small|sup|sub|mark|code|s|del|ins|kbd|abbr|q|cite"
                     "|var|samp|tt")
_INLINE_TAG_RUN_RE = re.compile(r"(?i)(?:</?(?:" + _INLINE_TAG_NAMES + r")\b[^<>]*+>)++")
_OPENING_TAG_RE = re.compile(r"<(?!/)")
_BASE64URL_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-")
# strip_html's rules, linear: its tag pattern backtracks on a run of '<'.
_BREAK_TAG_RE = re.compile(r"(?i)<\s*+(?:br\s*+/?+|/p|/div|/li|/tr|/h[1-6])\s*+>")
# A '<' starts a tag only before a letter, '/', '!' or '?': "x <= 5", "< 100 and > 50" are text.
_TAG_RE = re.compile(r"<(?=[A-Za-z/!?])[^<>]++>")
# A character reference that ends with ';'. html.unescape also decodes legacy names
# without one ("&section=2" loses "&sect"), which mangles URLs and prose.
_ENTITY_RE = re.compile(r"&(?:#[0-9]{1,8}+|#[xX][0-9A-Fa-f]{1,8}+|[A-Za-z][A-Za-z0-9]{0,31}+);")
_BLANKS_RE = re.compile(r"[ \t]++")
_BLANK_LINES_RE = re.compile(r"\n{3,}+")
# An ai element is ambiguous when its text holds anything tag-shaped ('<' before a letter, '/', '!'
# or '?', as _TAG_RE strips) or a character reference: it may be the editor's markup or an author's
# placeholder, and only the item HTML tells them apart. One search; each match is at most 34 characters.
_AMBIGUOUS_AI_TEXT_RE = re.compile(r"<[A-Za-z/!?]|" + _ENTITY_RE.pattern)


def _join_inline_tags(match: re.Match) -> str:
    text, start, end = match.string, match.start(), match.end()
    before = text[start - 1] if start else ""
    after = text[end] if end < len(text) else ""
    run = match.group(0)
    first_close = run.find("</")
    between_elements = first_close >= 0 and _OPENING_TAG_RE.search(run, first_close) is not None
    if before and after and before in _BASE64URL_CHARS and after in _BASE64URL_CHARS and not between_elements:
        return ""
    if after and not after.isspace():
        # A value that opens an element right after ':' or '=' ("api_key:<b>S</b>"), and the end of a
        # userinfo right before '@' ("https://admin:<b>S</b>@host"), join as the page shows them. Closing
        # tags alone after ':' end a label ("<b>Note:</b>Use the portal") and keep the space.
        if before in (":", "=") and _OPENING_TAG_RE.search(run) is not None:
            return ""
        if after == "@" and before and not before.isspace():
            return ""
    return " "


def _unescape_entity(match: re.Match) -> str:
    ref = match.group(0)
    if ref[1] == "#":
        return html.unescape(ref)
    return _HTML5_ENTITIES.get(ref[1:], ref)      # an unknown name stays as written


def _html_to_text(value: str) -> str:
    """Plain text like ``strip_html``'s (block breaks kept, tags removed, entities decoded), in linear time.

    Unlike it, a '<' that cannot start a tag stays, and only references that end with ';' are decoded.
    """
    text = _INLINE_TAG_RUN_RE.sub(_join_inline_tags, value)
    text = _BREAK_TAG_RE.sub("\n", text)
    text = _TAG_RE.sub(" ", text)
    return _tidy_lines(_ENTITY_RE.sub(_unescape_entity, text))


def _tidy_lines(text: str) -> str:
    """No-break spaces as spaces, blanks collapsed and stripped on each line, no run of blank lines."""
    lines = [_BLANKS_RE.sub(" ", line).strip() for line in text.replace("\xa0", " ").splitlines()]
    return _BLANK_LINES_RE.sub("\n\n", "\n".join(lines)).strip()


def _plain_text(value) -> str:
    """KMS HTML (a title, a description, an item element) as plain, scrubbed text, at full length (linear)."""
    return _scrub(_html_to_text("" if value is None else str(value)))


def _plain_ai_text(text: str) -> str:
    """Plain ai_answers_item text (API PDF), scrubbed, lines tidied; nothing is stripped or decoded."""
    return _scrub(_tidy_lines(text))


def _safe_title(title) -> str:
    """A KMS title as one path segment the folder-path rules accept (Durable Decisions)."""
    text = _without_controls(_plain_text(title).replace("/", "-"))
    text = _KIND_PREFIX_RE.sub(lambda m: m.group(1) + " -", text)
    text = text[:_MAX_SEGMENT_CHARS].rstrip()
    if not text.strip("."):
        return "untitled"
    return text


def _kind_of(record: dict) -> str:
    if str(record.get("type") or "").upper() == "FOLDER":
        return "folder"
    if any("SCENARIO" in str(record.get(key) or "").upper() for key in ("type", "subType", "sub_type")):
        return "scenario"
    return "item"


def _is_online(record: dict) -> bool | None:
    """From the first status field present; None when KMS sent none (shown, and checked again on read)."""
    for key in ("statusCode", "status_id", "status"):
        value = record.get(key)
        if value is None or not str(value).strip():
            continue
        return str(value).strip().lower() in ("2", "online")
    return None


def _make_id(kind: str, raw_id) -> str:
    raw = str(raw_id)
    if kind not in _KINDS or not _DIGITS_RE.fullmatch(raw):
        logger.debug("kms_lighthouse %s refused: %s", "content", "record without a numeric id")
        raise KmsHTTPError(200, "server")
    return f"{kind}:{raw}"


_MAX_FOLDER_CALLS = 40        # tree calls per list_files call, path resolution included
_MAX_PATH_SEGMENTS = 25
_BAD_ID = ("That is not a KMS Lighthouse id; use an id exactly as listed "
           "(item:<digits>, scenario:<digits> or folder:<digits>).")
_NOT_A_FOLDER = "That id is not a folder; pass a folder path from an entry's path, or a folder:<digits> id."
_BAD_FOLDER_PATH = "That folder path is not valid; use the folder part of an entry's path exactly as listed."
_FOLDER_NOT_FOUND = "Folder not found; use a folder path exactly as it appears in an entry's path."
_OUTSIDE_ROOT = "Folder is outside the configured root folder."
_BEYOND_BROWSE_LIMIT = ("Folder not found, or beyond the browse limit; pass its folder path "
                        "(the folder part of an entry's path) instead.")
_FOUND_BEYOND_BROWSE_LIMIT = ("Folder found, but beyond the browse limit; pass its folder path "
                              "(the folder part of an entry's path) instead.")
# Tree-call failures that answer for that one branch, so a folder lookup may walk on past a hidden
# folder that fails with one; "auth", "throttled", "unreachable" and "redirect" concern every call.
_BRANCH_FAILURES = frozenset(("forbidden", "not_found", "bad_request", "no_data", "rejected", "server",
                              "too_large"))
# A gateway answer says the service is down, not that this one branch is refused (a plain 500 still is).
_GATEWAY_STATUSES = frozenset((502, 503, 504))


def _concerns_one_branch(failed: KmsHTTPError) -> bool:
    """A tree-call failure that answers for that branch alone: a branch kind, no gateway status, not from a login."""
    return (failed.kind in _BRANCH_FAILURES and failed.status not in _GATEWAY_STATUSES
            and not isinstance(failed, _LoginFailure))


# ai_answers_item and scenario step failures that answer for that one item or step (/public/item, or
# the scenario, already proved access, so a tenant without the AI module may refuse every ai call with
# a 403); "auth", "throttled", "unreachable" and "redirect" concern every call.
_AI_ITEM_FAILURES = frozenset(("not_found", "bad_request", "server", "no_data", "rejected", "forbidden",
                               "too_large"))


def _concerns_one_item(failed: KmsHTTPError) -> bool:
    """An ai text or step failure about that item or step alone: an item kind, no gateway status, not a login."""
    return (failed.kind in _AI_ITEM_FAILURES and failed.status not in _GATEWAY_STATUSES
            and not isinstance(failed, _LoginFailure))


_BAD_LIST_LIMIT = "The list limit must be a positive whole number."
# A segment that already ends like a sibling suffix gets one of its own, so a
# suffixed segment can never equal another folder's plain one.
_SUFFIX_RE = re.compile(r" \[[0-9]{1,12}\]\Z")


def _parse_id(value, default_kind: str = "item") -> tuple[str, str]:
    """``(kind, digits)`` of a typed id (any case) or of bare digits; anything else is refused before any request."""
    text = str(value)
    prefix = _KIND_PREFIX_RE.match(text)
    kind = prefix.group(1).lower() if prefix else default_kind
    raw = text[prefix.end():] if prefix else text
    if kind not in _KINDS or not _DIGITS_RE.fullmatch(raw):
        raise KmsError(_BAD_ID)
    return kind, raw


def _is_path_segment(segment: str) -> bool:
    """A segment ``_safe_title`` can emit: 1-200 characters, not only dots, no kind prefix, no control character."""
    return (0 < len(segment) <= _MAX_SEGMENT_CHARS and segment.strip(".") != ""
            and not _KIND_PREFIX_RE.match(segment)
            and not any(unicodedata.category(ch)[0] == "C" for ch in segment))


def _folder_path_segments(text: str) -> tuple[str, ...]:
    """The segments of a folder path (ADR-7), checked before any request; a path never reaches a URL."""
    segments = text.split("/", _MAX_PATH_SEGMENTS)       # bounded: one part more than allowed is enough
    if len(segments) > _MAX_PATH_SEGMENTS or not all(_is_path_segment(s) for s in segments):
        raise KmsError(_BAD_FOLDER_PATH)
    return tuple(segments)


def _folder_target(folder_id):
    """``folder_id`` checked before any request: None (the origin), ``("id", digits)`` or ``("path", segments)``."""
    if folder_id is None or folder_id == "":
        return None
    text = str(folder_id)
    if _KIND_PREFIX_RE.match(text):
        kind, digits = _parse_id(text)
        if kind != "folder":
            raise KmsError(_NOT_A_FOLDER)
        return "id", str(int(digits))            # compared as a number: "folder:010" is folder 10
    path = text.strip("/")                       # "/Folder" and "Folder/Sub/" work; "/" is the origin
    return ("path", _folder_path_segments(path)) if path else None


def _folder_segments(folders: list) -> dict[str, str]:
    """Folder id -> path segment for the shown folders of one level.

    The segment is ``_safe_title(title)``, or ``<segment> [<id>]`` (cut so the
    whole is at most 200 characters) when a sibling has the same segment or
    the segment already ends like such a suffix, so every segment of a level
    names one folder.
    """
    bases = {str(record["id"]): _safe_title(record.get("title")) for record in folders}
    counts = Counter(bases.values())
    segments = {}
    for folder_id, base in bases.items():
        if counts[base] > 1 or _SUFFIX_RE.search(base):
            suffix = f" [{folder_id}]"
            base = base[:_MAX_SEGMENT_CHARS - len(suffix)].rstrip() + suffix
        segments[folder_id] = base
    return segments


def _entry_path(parent_segments: tuple[str, ...], record: dict) -> tuple[str, ...]:
    """An item's or a scenario's path: its folder's segments, then ``<title>.txt`` (search: no folder)."""
    return (*parent_segments, _safe_title(record.get("title")) + ".txt")


class _CallBudget:
    """The tree calls one ``list_files`` call may still make."""

    def __init__(self, calls: int):
        self.left = calls

    def take(self) -> bool:
        if self.left <= 0:
            return False
        self.left -= 1
        return True


def _normalise_date(value) -> str | None:
    """``dd/mm/yyyy`` (day first), ``yyyy-mm-dd hh:mm:ss[.f]`` or ISO, as ISO-8601; never raises."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        day_first = _DAY_FIRST_RE.match(text)
        if day_first:
            day, month, year = (int(part) for part in day_first.groups())
            return datetime(year, month, day).date().isoformat()
        return datetime.fromisoformat(text).isoformat()
    except (ValueError, OverflowError):
        return None


_ZERO_TOTAL_KEYS = ("total", "totalResults", "numFound")   # assumed names; the PDF documents no total


def _refuse_if_none_usable(skipped: int, files: list, hidden: int) -> None:
    """Records came back, but none had a numeric id and none was merely hidden: an unexpected answer."""
    if skipped and not files and not hidden:
        logger.warning("kms_lighthouse search refused: %d records, none with a numeric id", skipped)
        raise KmsHTTPError(200, "server")


def _zero_total(block: dict) -> bool:
    """The search block states a total of zero hits."""
    for key in _ZERO_TOTAL_KEYS:
        value = block.get(key)
        if value is not None and not isinstance(value, bool) and str(value).strip() == "0":
            return True
    return False


def _clean_query(query) -> str:
    return _without_controls("" if query is None else str(query))[:_MAX_QUERY_CHARS].rstrip()


# ------------------------------------------------------------------ read helpers (pure)
_FOLDER_READ = "That id is a folder; use list_files to see its contents."
_BAD_QUERY = ('A KMS Lighthouse query must be an id, or a JSON object with a file_id, '
              'such as {"file_id": "item:<digits>"}.')
_TOO_LARGE_READ = "File exceeds the configured byte limit; increase it explicitly to read this file."
_SCENARIO_ACCEPT = "application/vnp.kms-api.v2+json"
_IMAGE_FIELD = "ITEM_IMAGE_FIELD"
_GENERAL = "General"
_LANGUAGE_RE = re.compile(r"[A-Za-z]{2,3}(?:[-_][A-Za-z0-9]{1,8}){0,2}")     # used with fullmatch only


def _line(value) -> str:
    """A provider title, caption or label as one line of plain, scrubbed text."""
    return _without_controls(_plain_text(value))


def _same_id(value, digits: str) -> bool:
    """The record's id is the one asked for (compared as numbers)."""
    raw = "" if value is None or isinstance(value, bool) else str(value)
    return _DIGITS_RE.fullmatch(raw) is not None and int(raw) == int(digits)


def _version(value) -> str | None:
    raw = "" if value is None or isinstance(value, bool) else str(value).strip()
    return raw if _DIGITS_RE.fullmatch(raw) else None


def _language(value) -> str | None:
    return value.strip() if isinstance(value, str) and _LANGUAGE_RE.fullmatch(value.strip()) else None


def _elements(item: dict) -> dict:
    """The /public/item ``elements`` dict, records only (anything else is no element)."""
    elements = item.get("elements")
    if not isinstance(elements, dict):
        return {}
    records = {str(name): element for name, element in elements.items() if isinstance(element, dict)}
    if len(records) != len(elements):
        logger.debug("kms_lighthouse %s dropped: %s", "item", "an element is not a record")
    return records


def _header(title, kind: str, record: dict, version: str | None = None, language: str | None = None) -> list:
    lines = [f"Title: {_line(title) or 'untitled'}", f"Kind: {kind}"]
    if version:
        lines.append(f"Version: {version}")
    if language:
        lines.append(f"Language: {language}")
    updated = _normalise_date(record.get("updateDate") or record.get("updatedDate"))
    if updated:
        lines.append(f"Updated: {updated}")
    summary = _line(record.get("description"))
    if summary:
        lines.append(f"Summary: {summary}")
    return lines


def _render_sections(sections: list) -> list:
    """``[(caption, [(element title, text), ...]), ...]`` as "## caption" and "### title" blocks."""
    lines = []
    for caption, elements in sections:
        lines += ["", f"## {caption}"]
        for index, (title, text) in enumerate(elements):
            if index:
                lines.append("")
            if title:
                lines.append(f"### {title}")
            if text:
                lines.append(text)
    return lines


def _item_sections(item: dict, records: dict) -> list:
    """The /public/item elements (``records``: ``_elements(item)``) by tab, in tab order.

    Tabs match on ``str(tabId)`` against ``str(tabs[].id)``.

    The item image is skipped; an element with no tab, a ``null`` or ``-1``
    tabId, or one no tab names goes to an untitled "General" section, first.
    """
    tabs = item.get("tabs") if isinstance(item.get("tabs"), list) else []
    captions = {}
    for tab in tabs:
        if isinstance(tab, dict) and tab.get("id") is not None and _line(tab.get("caption")):
            captions.setdefault(str(tab["id"]), _line(tab.get("caption")))
    by_tab: dict = {}
    for element in records.values():
        if element.get("type") == _IMAGE_FIELD:
            continue
        tab_id = element.get("tabId")
        key = str(tab_id) if tab_id is not None and str(tab_id) in captions else None
        by_tab.setdefault(key, []).append((_line(element.get("title")), _plain_text(element.get("value"))))
    sections = [(_GENERAL, by_tab[None])] if None in by_tab else []
    return sections + [(caption, by_tab[key]) for key, caption in captions.items() if key in by_tab]


def _visible_ai_elements(elements: list, visible: set) -> list:
    """The ai elements whose ``element_name`` the same identity's /public/item returned, first of each name.

    The ai payload carries its own permissions, so it may hold elements
    /public/item hides from this identity (r4); only the intersection is shown.
    A repeated name is shown once, so it never renders its item HTML twice
    (one debug line per read, the reason only, when any is skipped).
    """
    shown, seen, repeated = [], set(), False
    for element in elements:
        if (isinstance(element, dict) and element.get("element_type") != _IMAGE_FIELD
                and isinstance(element.get("element_name"), str) and element["element_name"] in visible):
            if element["element_name"] in seen:
                repeated = True
                continue
            seen.add(element["element_name"])
            shown.append(element)
    if repeated:
        logger.debug("kms_lighthouse %s skipped: %s", "ai_text", "a repeated element name")
    return shown


def _ai_element_text(element: dict, records: dict) -> str | None:
    """One shown ai element's text; None when its item has no HTML to render it from (drop it).

    Plain ai text is shown as written (scrubbed). Text with anything
    tag-shaped or a character reference (``_AMBIGUOUS_AI_TEXT_RE``) renders
    from the same item's /public/item HTML for that element instead, where
    markup and an escaped "<placeholder>" are told apart; so does a null or
    blank text, which would leave a heading with nothing under it.
    """
    text = "" if element.get("element_text") is None else str(element["element_text"])
    if _AMBIGUOUS_AI_TEXT_RE.search(text) is None and _tidy_lines(text):
        return _plain_ai_text(text)
    value = records[element["element_name"]].get("value")
    if value is None:
        logger.debug("kms_lighthouse %s dropped: %s", "ai_text", "an element has no item HTML")
        return None
    return _plain_text(value)


def _ai_sections(elements, records: dict) -> list | None:
    """The ai text by ``tab_caption`` then ``element_caption``; None when no element is left (fall back).

    ``records`` is ``_elements(item)`` of the same read: only its non-image
    names are shown, and an ambiguous element renders from its HTML there.
    A fall back is logged with its reason only.
    """
    visible = {name for name, element in records.items() if element.get("type") != _IMAGE_FIELD}
    shown = _visible_ai_elements(elements, visible) if isinstance(elements, list) else []
    if not shown:
        logger.debug("kms_lighthouse %s fell back: %s", "ai_text", "no element the item shows")
        return None
    by_tab: dict = {}
    for element in shown:
        text = _ai_element_text(element, records)
        if text is not None:
            by_tab.setdefault(_line(element.get("tab_caption")) or None, []).append(
                (_line(element.get("element_caption")), text))
    if not by_tab:
        logger.debug("kms_lighthouse %s fell back: %s", "ai_text", "every shown element was dropped")
        return None
    general = by_tab.pop(None, None)
    return ([(_GENERAL, general)] if general else []) + list(by_tab.items())


# A scenario (decision tree) read is bounded: entries rendered, step fetches and nesting depth.
_MAX_SCENARIO_ENTRIES = 400
_MAX_STEP_FETCHES = 40
_MAX_SCENARIO_DEPTH = 25
_SCENARIO_PARAMS = {"v": "2", "plainText": "true"}
_TREE_TRUNCATED = "[tree truncated: some steps not shown]"
_STEP_CUT = "(not shown: tree truncated)"
_STEP_UNAVAILABLE = "(step not available)"
_STEPS_NOT_LOADED = "(some steps could not be loaded)"
_STEP_INDENT = "  "


def _scenario_text(value) -> str:
    """Step text, asked for with plainText=true (API PDF: "content without HTML tags"), as written.

    Nothing else tells markup from an author's text, so all of it is plain:
    lines tidied and scrubbed, nothing stripped or decoded ("Run reset
    <username> now." and "X-Api-Key: <key>" stay). Anything but a string is
    no text.
    """
    return _plain_ai_text(value if isinstance(value, str) else "")


def _scenario_line(value) -> str:
    """A label or a step name as one plain, scrubbed line (a number as written), like ``_line``."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        value = str(value)
    return _without_controls(_scenario_text(value))


def _step_key(value) -> str | None:
    """A step id as compared and sent (digits, no leading zeros), or None."""
    raw = "" if value is None or isinstance(value, bool) else str(value)
    return str(int(raw)) if _DIGITS_RE.fullmatch(raw) else None


def _step_branches(step: dict) -> list:
    """``(True, label, child id, inline child)`` per decision, then ``(False, None, id, child)`` per other child.

    The label is as KMS gives it (``_scenario_line`` runs when it is shown).
    A decision key is ``<scenario>:<n>:<child stepId>``; its child is the
    inline record ``children[<stepId>]`` when there is one, else None (to be
    fetched). A child no decision names is a continuation step. A child that
    is not a record and a decision whose step id is not digits are each
    logged once per step, by reason only.
    """
    children = step.get("children") if isinstance(step.get("children"), dict) else {}
    inline = [(_step_key(key), child) for key, child in children.items() if isinstance(child, dict)]
    if len(inline) < len(children):
        logger.debug("kms_lighthouse %s skipped: %s", "step", "child is not a record")
    by_id = {}
    for child_id, child in inline:
        if child_id is not None:
            by_id.setdefault(child_id, child)
    decisions = step.get("decisions") if isinstance(step.get("decisions"), dict) else {}
    branches, named = [], set()
    for key, label in decisions.items():
        child_id = _step_key(str(key).rpartition(":")[2])
        named.add(child_id)
        branches.append((True, label, child_id, by_id.get(child_id)))
    if None in named:
        logger.debug("kms_lighthouse %s skipped: %s", "step", "decision id is not digits")
    return branches + [(False, None, child_id, child) for child_id, child in inline
                       if child_id is None or child_id not in named]


def _prefetch_steps(root: dict, root_id: str | None, fetch_step) -> tuple[dict, set, dict, set, int]:
    """The entries and steps the tree shows, breadth first, so both budgets reach each level before the next.

    The root is one entry; then each queued step's branches, in order, take
    their option line and their step while ``_MAX_SCENARIO_ENTRIES``
    allows (``admitted``: ``(id() of the step record, branch index, "line"
    or "step")``); once it is spent nothing more is expanded. A queued step
    that has branches also holds one entry for the cut marker it renders
    when not all of them are admitted, released once they are (a step to be
    fetched holds it until its record shows none), so markers stay within
    the bound. An admitted child that is not inline is fetched while
    ``_MAX_STEP_FETCHES`` allows and it is no deeper than
    ``_MAX_SCENARIO_DEPTH``; an inline copy found later clears an earlier
    failed fetch of the same id. Returns the steps by id (inline or
    fetched), the ids whose fetch failed (never fetched again), each queued
    step's branches by ``id()`` of its record, the admitted entries and the
    number of fetches. Linear: each record's branches are listed once.
    """
    known = {} if root_id is None else {root_id: root}
    failed, admitted, fetches = set(), set(), 0
    branches_of = {id(root): _step_branches(root)}
    entries = 1 + bool(branches_of[id(root)])       # the root step and its reserved cut marker
    queue = deque([(root, 0)])
    while queue and entries < _MAX_SCENARIO_ENTRIES:
        step, depth = queue.popleft()
        branches = branches_of[id(step)]
        for index, (decision, _label, child_id, child) in enumerate(branches):
            if decision:
                if entries >= _MAX_SCENARIO_ENTRIES:
                    break
                entries += 1
                admitted.add((id(step), index, "line"))
            child_depth = depth + 1 if decision else depth
            queued = not ((child_id is not None and child_id in known) or child_depth > _MAX_SCENARIO_DEPTH
                          or (child is None and (child_id is None or child_id in failed
                                                 or fetches >= _MAX_STEP_FETCHES)))
            child_branches = _step_branches(child) if queued and child is not None else None
            reserve = queued and (child is None or bool(child_branches))
            if entries + 1 + reserve > _MAX_SCENARIO_ENTRIES:
                break
            entries += 1 + reserve
            admitted.add((id(step), index, "step"))
            if not queued:
                continue
            if child is None:
                fetches += 1
                child = fetch_step(child_id)
                if child is None:
                    failed.add(child_id)
                    entries -= 1                    # an unavailable step renders no cut marker
                    continue
                child_branches = _step_branches(child)
                entries -= not child_branches       # a fetched step without branches needs no reserve
            branches_of[id(child)] = child_branches
            if child_id is not None:
                known[child_id] = child
                failed.discard(child_id)
            queue.append((child, child_depth))
        else:
            entries -= bool(branches)               # every branch admitted: no cut marker
    return known, failed, branches_of, admitted, fetches


def _render_scenario(root: dict, fetch_step) -> list:
    """``data.steps`` as indented lines, depth first, in the order KMS gives.

    A step shows its ``stepName`` (else ``stepNodeText``, unless that only
    repeats the option label that leads to it), its ``text`` and
    ``stepRemark``, and ``Image: <url>`` with the token parameters removed.
    Each decision is ``- "<label>" ->`` with its child one level in; a
    continuation child stays at its parent's level. A child that is not
    inline comes from ``fetch_step(step id)`` (a record, or None when it is
    not available), fetched first, breadth first (``_prefetch_steps``). A
    step id seen before renders ``(see step "<name>" above)``; a step whose
    fetch failed renders ``(step not available)`` at each reference. Every
    entry (a step, an option line, a marker, a cut marker) counts toward
    ``_MAX_SCENARIO_ENTRIES``, admitted level first (``_prefetch_steps``);
    where an entry was not admitted, past ``_MAX_STEP_FETCHES`` fetches or
    past ``_MAX_SCENARIO_DEPTH`` levels, ``_STEP_CUT`` stands where the step
    (or a step's remaining options) would be and ``_TREE_TRUNCATED`` ends
    the tree. When steps were fetched and every one still failed, ``_STEPS_NOT_LOADED``
    comes first. Linear: each step is rendered once.
    """
    root_id = _step_key(root.get("stepId"))
    known, failed, branches_of, admitted, fetches = _prefetch_steps(root, root_id, fetch_step)
    lines, seen, names = [], set(), {}
    truncated = False
    # (kind, record or line text, step id, depth, label that leads to it); kind is "step", "line" or "cut".
    stack = [("step", root, root_id, 0, None)]
    while stack:
        kind, step, step_id, depth, label = stack.pop()
        indent = _STEP_INDENT * depth
        if kind == "line":
            lines.append(indent + step)
            continue
        if kind == "cut":
            lines.append(indent + _STEP_CUT)
            truncated = True
            continue
        if step_id is not None and step_id in seen:
            name = names.get(step_id)
            lines.append(indent + (f'(see step "{name}" above)' if name else "(see step above)"))
            continue
        if depth > _MAX_SCENARIO_DEPTH:
            lines.append(indent + _STEP_CUT)
            truncated = True
            continue
        if step_id is not None:
            step = known.get(step_id)
        if step is None:
            if step_id is None or step_id in failed:
                lines.append(indent + _STEP_UNAVAILABLE)
            else:                                   # the fetch budget was spent before this step
                lines.append(indent + _STEP_CUT)
                truncated = True
            continue
        name = _scenario_line(step.get("stepName")) or _scenario_line(step.get("stepNodeText"))
        if step_id is not None:
            seen.add(step_id)
            names[step_id] = name
        if name and name != label:
            lines.append(indent + name)
        for field in ("text", "stepRemark"):
            lines += [indent + line if line else "" for line in _scenario_text(step.get(field)).splitlines()]
        image = _line(step.get("image")) if isinstance(step.get("image"), str) else ""
        if image:
            lines.append(f"{indent}Image: {image}")
        # A step the prefetch queued but did not expand (past the entry budget) has no admitted branch.
        branches = branches_of[id(step)] if id(step) in branches_of else _step_branches(step)
        pending = []
        for index, (decision, raw_label, child_id, child) in enumerate(branches):
            option = None
            if decision:
                if (id(step), index, "line") not in admitted:
                    pending.append(("cut", None, None, depth, None))       # the remaining options
                    break
                option = _scenario_line(raw_label)
                pending.append(("line", f'- "{option}" ->', None, depth, None))
            child_depth = depth + 1 if decision else depth
            if (id(step), index, "step") not in admitted:
                pending.append(("cut", None, None, child_depth, None))
                break
            pending.append(("step", child, child_id, child_depth, option))
        stack += reversed(pending)
    if truncated:
        lines.append(_TREE_TRUNCATED)
    if lines and fetches and len(failed) == fetches:
        lines.insert(0, _STEPS_NOT_LOADED)
    return lines


def _optional_int(value) -> int | None:
    """A config number as the client reads it: None when missing or blank (the connect form stores "")."""
    return int(value) if value not in (None, "") else None


def _max_results_of(value) -> int:
    """The max_results config value as the client reads it (blank or 0 is the default)."""
    return int(value or _DEFAULT_MAX_RESULTS)


def _note_number(read, value, fallback):
    """``read(value)`` for the planner note, or ``fallback`` when the value is not a number."""
    try:
        return read(value)
    except (TypeError, ValueError, OverflowError):
        return fallback


def planner_scope_note(config: dict) -> str:
    """The planner-facing browse note for a KMS connection (pure, no I/O)."""
    cfg = config or {}
    # Read each number exactly as the constructor does, so only whole numbers reach the prompt.
    max_results = _note_number(_max_results_of, cfg.get("max_results"), _DEFAULT_MAX_RESULTS)
    root = _note_number(_optional_int, cfg.get("root_folder_id"), None)
    layout = _note_number(_optional_int, cfg.get("layout_id"), None)
    origin = f"the root folder (id {root})" if root is not None else "the top level"
    in_layout = f" in layout {layout}" if layout is not None else ""
    return (
        f"KMS Lighthouse: list_files with no folder_id returns at most {max_results} articles and scenarios (folders "
        f"do not count) from {origin}, and stops there without saying so (the listing can be incomplete). Each list_files "
        f"call also stops after {_MAX_FOLDER_CALLS} folder lookups, so a short listing can be incomplete too; "
        "open a specific folder by its path or use search_files. There is no catalog: the file count shown is "
        "always 0, and every listing and read is live. To open a folder, "
        "pass the folder part of an entry's path as folder_id (for example 'Folder/Subfolder') with "
        "recursive=true; paths start below the configured root folder when one is set, and folder ids are "
        "not shown. The root folder limits browsing only: search_files covers "
        f"everything this identity can see{in_layout}, and is the better first step. Read with read_file and "
        "the entry's id (item:<id> or scenario:<id>)."
    )


class KmsLighthouseClient(DataSourceClient):
    capabilities = {Capability.LIST_FILES, Capability.READ_FILE, Capability.SEARCH_FILES}
    # Always list live with the caller's identity: there is no catalog (ADR-1).
    cheap_live_listing = True
    is_document_based = True

    def __init__(self, base_url: str, api_prefix: str = _DEFAULT_PREFIX, layout_id: int | None = None,
                 root_folder_id: int | None = None, include_offline: bool = False, allow_http: bool = False,
                 max_results: int = _DEFAULT_MAX_RESULTS, index_mode: str = "none",
                 username: str | None = None, password: str | None = None, api_token: str | None = None,
                 auth_type: str | None = None, **kwargs):
        prefix = (api_prefix or _DEFAULT_PREFIX).strip().rstrip("/") or _DEFAULT_PREFIX
        if not _PREFIX_RE.match(prefix) or any(seg in (".", "..") for seg in prefix.split("/")):
            raise KmsError("API path prefix must be a plain path such as /kms/lh/api.")
        raw_url = (base_url or "").strip() if isinstance(base_url, str) else ""
        parsed, bad_url = None, False
        try:
            parsed = urlsplit(raw_url)
            _ = parsed.port  # raises ValueError for a port that is not a number in 0-65535
        except ValueError:
            bad_url = True
        # Raised outside the except block so no parser error rides along as context.
        if (bad_url or parsed.scheme not in ("http", "https") or not parsed.hostname
                or "@" in parsed.netloc or any(ch.isspace() for ch in parsed.netloc)
                or parsed.query or parsed.fragment or "#" in raw_url or "?" in raw_url):
            raise KmsError(_URL_MESSAGE)
        allow_http = _flag(allow_http)
        if parsed.scheme == "http" and not allow_http:
            raise KmsError("HTTPS is required. Enable Allow HTTP only for an isolated test lab.")
        # The URL may carry the API prefix (it is stripped) but no other path,
        # so every request goes to exactly {scheme}://{host}{prefix}/...
        path = unquote(parsed.path).rstrip("/")
        if path not in ("", prefix):
            raise KmsError("KMS Lighthouse URL path must be empty or equal to the API path prefix.")
        self.host = parsed.netloc.lower()
        self._base = f"{parsed.scheme.lower()}://{self.host}"
        self.api_prefix = prefix
        self.layout_id = _optional_int(layout_id)
        self.root_folder_id = _optional_int(root_folder_id)
        self.include_offline = _flag(include_offline)
        self.max_results = _max_results_of(max_results)
        self.index_mode = "none"

        # A copied login name may carry stray spaces; a password is used exactly as typed.
        username, password = _text(username), _text(password)
        username = username.strip() if username else None
        api_token = _text(api_token)
        api_token = api_token.strip() if api_token else None
        has_userpass = bool(username) and bool(password)
        has_token = bool(api_token)
        if auth_type == "userpass" and has_userpass:
            self.auth_method = "userpass"
        elif auth_type == "token" and has_token:
            self.auth_method = "token"
        elif has_token:
            self.auth_method = "token"
        elif has_userpass:
            self.auth_method = "userpass"
        else:
            raise KmsError(_NO_CREDENTIAL)
        if self.auth_method == "token" and not KMS_TOKEN_RE.fullmatch(api_token):
            raise KmsError(KMS_TOKEN_FORMAT_MESSAGE)
        self._username = username if self.auth_method == "userpass" else None
        self._password = password if self.auth_method == "userpass" else None
        self._api_token = api_token if self.auth_method == "token" else None

        # Login state lives in the process pool entry for this identity (ADR-6),
        # shared with every other instance built for it. Each thread-local
        # session re-applies it whenever the entry's generation moves ahead.
        self._entry = (_pool_entry(_pool_key(self._base, prefix, username, password))
                       if self.auth_method == "userpass" else None)
        self._local = threading.local()
        # Tree levels seen by this instance (one tool call): parent id (None: top level) -> records.
        self._tree_cache: dict = {}

    # ------------------------------------------------------------------ meta
    @property
    def description(self):
        # One phrase for both variants: either may be a system or a per-user
        # connection, and the client is never told which (no auth_policy here).
        who = ("this connection's KMS identity (the configured account, or each person's own KMS login "
               "or token on per-user connections)")
        origin = "root folder" if self.root_folder_id is not None else "top level"
        shown = "Online and offline content is shown" if self.include_offline else "Only Online content is shown"
        layout = f" in layout {self.layout_id}" if self.layout_id is not None else ""
        return (
            f"Read-only KMS Lighthouse knowledge base at {self.host} (articles and decision-tree scenarios), "
            f"accessed as {who}; KMS permissions apply. Use search_files to find procedures. list_files with "
            f"no folder lists up to {self.max_results} articles and scenarios (folders do not count) from the "
            f"{origin}, each with a path of folder names; to open one folder, pass the folder part of a path as folder_id (for example "
            "folder_id='Folder/Subfolder'; paths start below the root folder when one is set) and set "
            "recursive=true to include its subfolders. Read with "
            f"read_file and the entry's id (item:<id> or scenario:<id>). {shown}. The root folder limits browsing only; search covers everything the identity can see"
            f"{layout}."
        )

    # ------------------------------------------------------------------ auth
    def _new_session(self) -> requests.Session:
        session = pinned_session()
        session.trust_env = False
        session.verify = os.environ.get("REQUESTS_CA_BUNDLE") or True
        session.headers["Accept"] = "application/json"
        return session

    def _session(self) -> requests.Session:
        # requests.Session is not thread-safe: one per worker thread.
        if not hasattr(self._local, "session"):
            self._local.session = self._new_session()
            self._local.generation = -1
            if self.auth_method == "token":
                self._local.session.headers["Authorization"] = "Bearer " + self._api_token
        session = self._local.session
        entry = self._entry
        if entry is not None and self._local.generation != entry.generation:
            with entry.state_lock:
                cookies, token, generation = dict(entry.cookies), entry.token, entry.generation
            session.cookies.clear()
            session.cookies.update(cookies)
            if token:
                session.headers["Authorization"] = "Bearer " + token
            else:
                session.headers.pop("Authorization", None)
            self._local.generation = generation
        return session

    def _check_hold(self, entry: _PoolEntry) -> None:
        """Refuse without contacting KMS while a rejected login of this identity is recent."""
        with entry.state_lock:
            held = _held(entry, _clock())
        if held:
            logger.debug("kms_lighthouse %s refused: %s", "login", "login hold")
            raise _LoginFailure(401, "auth", _HELD_AUTH)

    @staticmethod
    def _is_young(entry: _PoolEntry) -> bool:
        with entry.state_lock:
            return _young(entry, _clock())

    def _ensure_login(self) -> None:
        entry = self._entry
        if entry is None:
            return
        # A pooled session of this identity, still young enough to reuse: no
        # login_lock, so a login in flight (e.g. a health probe) does not block it.
        if self._is_young(entry):
            return
        _refuse_login_in_proof()
        with entry.login_lock:
            if self._is_young(entry):
                return   # another caller logged in while this one waited
            self._check_hold(entry)
            self._login(self._new_session())

    def _relogin(self, used_generation: int) -> None:
        """Log in again after the session of ``used_generation`` was refused.

        If another thread or instance already logged in again (the entry's
        generation moved on and its session is young), adopt that session
        instead, so one expiry causes one login. A generation that moved on
        without a young session (``clear_session_cache`` ran meanwhile) has
        nothing to adopt, so it logs in.

        KMS refused the pooled session of ``used_generation``, so it is
        forgotten first: when the re-login fails or is held, later calls stop
        at the hold instead of sending the dead session again. Only this path
        forgets it; a rejected ``test_connection`` login leaves a young session
        to the tool calls that hold it (ADR-6).
        """
        _refuse_login_in_proof()
        entry = self._entry
        with entry.login_lock:
            if entry.generation == used_generation or not self._is_young(entry):
                if entry.generation == used_generation:
                    with entry.state_lock:
                        entry.cookies, entry.token, entry.created_at = {}, None, None
                self._check_hold(entry)
                self._login(self._new_session())

    def _rejected(self) -> KmsHTTPError:
        """KMS refused this login: hold new attempts for this identity."""
        with self._entry.state_lock:
            self._entry.failed_at = _clock()
        logger.info("kms_lighthouse login rejected; holding new logins for %d s", _FAILED_LOGIN_HOLD_S)
        return _LoginFailure(401, "auth", _USERPASS_AUTH)

    def _login(self, session: requests.Session, deadline=None, observe: dict | None = None) -> None:
        """The only place that authenticates (a hash login would plug in here).

        Called with the pool entry's login_lock held. Every failure is a
        ``_LoginFailure`` with the same status, so a caller that tolerates one
        branch's or one item's failure never takes a login outage for one. A
        kind that would speak of the content asked for (e.g. a login 404 as
        "Not found") becomes ``server``; the others keep kind and message.
        ``deadline`` and ``observe`` are for ``diagnostics``: the first bounds
        the request (see ``_send``), the second receives the login answer.
        """
        failed = None
        try:
            self._send_login(session, deadline, observe)
        except _LoginFailure:
            raise
        except KmsHTTPError as exc:
            failed = exc
        # Raised outside the except block, so nothing rides along as context.
        if failed is not None:
            if failed.kind in _CONTENT_KINDS:
                raise _LoginFailure(failed.status, "server")
            login_failure = _LoginFailure(failed.status, failed.kind, str(failed))
            login_failure.transport, login_failure.answered = failed.transport, failed.answered
            login_failure.verify_code, login_failure.connect_timeout = failed.verify_code, failed.connect_timeout
            raise login_failure

    def _send_login(self, session: requests.Session, deadline=None, observe: dict | None = None) -> None:
        unsendable = False
        try:
            self._username.encode("utf-8")
            self._password.encode("utf-8")
        except UnicodeError:   # e.g. a lone surrogate from a JSON credential
            unsendable = True
        # Raised outside the except block: the UnicodeError's .object is the credential.
        if unsendable:
            logger.debug("kms_lighthouse %s refused: %s", "login", "credential cannot be encoded")
            raise KmsHTTPError(0, "server", _UNSENDABLE_LOGIN)
        status, body = self._send(session, "login", "POST", "/login", deadline=deadline,
                                  params={"username": self._username, "password": self._password})
        if observe is not None:
            observe["status"] = status
        if status in (400, 401, 403):
            raise self._rejected()
        if status != 200:
            raise KmsHTTPError(status, _kind_for_status(status))
        payload = self._decode(body, "login")
        if observe is not None:
            observe["payload"] = payload if isinstance(payload, dict) else {}
            observe["cookie_names"] = [cookie.name for cookie in session.cookies]
        if not isinstance(payload, dict) or str(payload.get("status", "")).upper() != "SUCCESS":
            raise self._rejected()
        data = payload.get("data")
        token = None
        if isinstance(data, dict) and isinstance(data.get("token"), str):
            token = data["token"]
        elif isinstance(data, str):
            token = data
        elif isinstance(payload.get("token"), str):
            token = payload["token"]
        if token and not KMS_TOKEN_RE.fullmatch(token):
            # It cannot go into an HTTP header as-is; refuse it before it becomes the session.
            logger.debug("kms_lighthouse %s refused: %s", "login", "unusable session token")
            raise KmsHTTPError(200, "server")
        # By name and value: dict(jar) raises CookieConflictError when the
        # server sets one name twice (for example on two paths).
        cookies = {cookie.name: cookie.value for cookie in session.cookies}
        if not cookies and not token:
            # Nothing to authenticate the next call with: do not count this as
            # a login, or the next 401 would wrongly blame the password.
            logger.debug("kms_lighthouse %s refused: %s", "login", "no session in the answer")
            raise KmsHTTPError(200, "server", _NO_SESSION)
        entry = self._entry
        with entry.state_lock:
            entry.cookies = cookies
            entry.token = token or None
            entry.created_at = _clock()
            entry.failed_at = None
            entry.generation += 1

    # ------------------------------------------------------------------ http
    def _send(self, session, op, method, path, *, params=None, headers=None, deadline=None, prefix=None):
        """One HTTP exchange. Returns (status, body bytes); never raises with a URL.

        With a ``deadline`` (a ``time.monotonic()`` value) the connect and the
        read timeout are each cut to the time left, but never below one second,
        so one exchange can take up to twice the time left: a caller that must
        fit connect plus read passes a deadline that allows for both. A body is
        also stopped at the first chunk read after the deadline.
        ``prefix`` replaces the configured API prefix for this one exchange
        (``diagnostics`` only: a GET, never a login).
        """
        take_call = getattr(_PROOF, "take_call", None)
        if take_call is not None:
            deadline = take_call()
        url = self._base + (prefix or self.api_prefix) + path
        started = time.monotonic()
        timeout = _DEFAULT_TIMEOUT
        if deadline is not None:
            left = max(_MIN_TIMEOUT_S, deadline - started)
            timeout = (min(_DEFAULT_TIMEOUT[0], left), min(_DEFAULT_TIMEOUT[1], left))
        status, body, failed, cause, timed_out, transport, verify_code = 0, b"", None, None, False, None, None
        connect_timeout, out_of_time = False, False
        _IN_KMS_REQUEST.active = True   # urllib3 would log the URL with its query (ADR-2)
        try:
            with session.request(method, url, params=params, headers=headers, timeout=timeout,
                                 allow_redirects=False, stream=True) as response:
                status = response.status_code
                if status == 200:
                    # iter_content maps urllib3 body errors to requests errors
                    # (raw.read does not) and decodes gzip; the cap counts decoded bytes.
                    chunks, size = [], 0
                    for chunk in response.iter_content(_CHUNK_BYTES):
                        size += len(chunk)
                        if size > _MAX_RESPONSE_BYTES:
                            failed = "too_large"
                            break
                        chunks.append(chunk)
                        # The socket timeout restarts with every byte, so a trickled body has only this bound.
                        if deadline is not None and _clock() >= deadline:
                            failed, cause, transport, out_of_time = "unreachable", "deadline", "timeout", True
                            break
                    body = b"".join(chunks)
        except _TRANSPORT_ERRORS as exc:
            failed, cause = "unreachable", type(exc).__name__
            timed_out = _is_timeout(exc)
            connect_timeout = isinstance(exc, requests.exceptions.ConnectTimeout)
            transport = _body_failure(exc) if status and not timed_out else _transport_failure(exc)
            verify_code = _verify_code(exc)
        finally:
            _IN_KMS_REQUEST.active = False
        # Raised outside the except block, so no exception chain carries the URL.
        if failed:
            # The class name tells SSL, DNS and timeouts apart; never the text (it holds the URL).
            logger.debug("kms_lighthouse %s failed: %s (%s)", op, failed, cause or "size cap")
            error = (_DeadlineTimeout()     # the deadline cut this timeout: the caller's time is up
                     if out_of_time or (timed_out and deadline is not None
                                        and deadline - time.monotonic() < _MIN_TIMEOUT_S)
                     else KmsHTTPError(status if failed == "too_large" else 0, failed))
            error.transport, error.answered, error.verify_code = transport, status, verify_code
            error.connect_timeout = connect_timeout
            raise error
        logger.debug("kms_lighthouse %s -> %s in %d ms", op, status, int((time.monotonic() - started) * 1000))
        return status, body

    @staticmethod
    def _decode(body: bytes, op: str):
        failed = False
        try:
            payload = json.loads(body)
        except (ValueError, RecursionError):  # RecursionError: absurdly deep nesting
            failed = True
        if failed:
            logger.debug("kms_lighthouse %s refused: %s", op, "body is not JSON")
            raise KmsHTTPError(0, "server")
        return payload

    def _request(self, op, path, *, params=None, headers=None, missing="rejected", deadline=None):
        """GET ``{base}{prefix}{path}`` as this client's identity and return the JSON body.

        ``deadline`` bounds the GET's timeouts (see ``_send``); a login keeps the default ones.
        """
        self._ensure_login()
        session = self._session()
        used_generation = self._local.generation
        status, body = self._send(session, op, "GET", path, params=params, headers=headers, deadline=deadline)
        if self.auth_method == "userpass" and _session_lost(status):
            # One re-login (shared with any thread that lost the same session), one retry.
            self._relogin(used_generation)
            status, body = self._send(self._session(), op, "GET", path, params=params, headers=headers,
                                      deadline=deadline)
            if _session_lost(status):
                # KMS has just accepted the password, so do not blame it.
                logger.warning("kms_lighthouse %s session refused after a fresh login", op)
                raise KmsHTTPError(401, "auth", _SESSION_REFUSED)
        elif status == 401:
            # A pasted token cannot be renewed here.
            raise KmsHTTPError(401, "auth", _TOKEN_AUTH)
        if status != 200:
            raise KmsHTTPError(status, _kind_for_status(status))
        if not body.strip():
            raise KmsHTTPError(200, "no_data")
        payload = self._decode(body, op)
        if isinstance(payload, dict) and str(payload.get("status", "")).upper() == "ERROR":
            logger.debug("kms_lighthouse %s refused: %s", op, "status ERROR")
            raise KmsHTTPError(200, missing)
        return payload

    def _tree_children(self, parent_id: int | str | None) -> list:
        params = {"parentId": str(parent_id)} if parent_id is not None else None
        payload = self._request("tree", "/cm/tree/branch/view", params=params)
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list):
            logger.debug("kms_lighthouse %s refused: %s", "tree", "data is not a list")
            raise KmsHTTPError(200, "server")
        for record in data:
            if not isinstance(record, dict) or not _DIGITS_RE.fullmatch(str(record.get("id", ""))):
                logger.debug("kms_lighthouse %s refused: %s", "tree", "record without a numeric id")
                raise KmsHTTPError(200, "server")
        return data

    # ------------------------------------------------------------------ api
    @_closed_errors
    def test_connection(self):
        try:
            entry = self._entry
            if entry is not None:
                # A health probe always logs in fresh (never a warm session),
                # but a recently rejected login is not sent again (ADR-6).
                with entry.login_lock:
                    self._check_hold(entry)
                    self._login(self._new_session())
            identity = None
            try:
                info = self._request("session_info", "/sessionInfo")
                data = info.get("data") if isinstance(info, dict) else None
                if isinstance(data, dict):
                    names = [data.get(key) for key in ("username", "userFullName")]
                    identity = next((_clean_text(n) for n in names if isinstance(n, str) and _clean_text(n)), None)
            except KmsHTTPError as exc:
                if exc.kind == "auth":
                    raise
                # The identity is optional; the tree call below decides the result.
                logger.debug("kms_lighthouse session_info ignored: %s", exc.kind)
            try:
                children = self._tree_children(self.root_folder_id)
            except KmsHTTPError as exc:
                if self.root_folder_id is None and exc.kind in ("bad_request", "no_data"):
                    return {"success": False, "message": _TOP_LEVEL_HINT}
                raise
            if self.root_folder_id is None and not children:
                return {"success": False, "message": _TOP_LEVEL_HINT}
            # ``reachable``: KMS answered, so these two keep the connection savable, for an admin to fix.
            if not children:
                return {"success": False, "reachable": True, "message": _EMPTY_ROOT.format(root=self.root_folder_id)}
            if self._layout_refused():
                return {"success": False, "reachable": True, "message": _BAD_LAYOUT.format(layout=self.layout_id)}
            # ``details.identity`` is a KMS account name or None, never a stand-in.
            identity = identity or (self._username if self.auth_method == "userpass" else None)
            named = identity or "the API token's account"
            return {
                "success": True,
                "message": f"Connected to KMS Lighthouse at {self.host} as {named}.",
                "details": {"host": self.host, "identity": identity, "auth_method": self.auth_method,
                            "root_folder_id": self.root_folder_id},
            }
        except (KmsError, GlobScopeError) as exc:
            logger.info("kms_lighthouse test_connection failed: %s", getattr(exc, "kind", "error"))
            result = {"success": False, "message": str(exc)}
            # The login cannot be used: a caller saving a member's login must not store it.
            # "rejected" is a wrong password or token; "session_refused" is KMS taking the
            # password but refusing (or never giving) a session; "held" is a recent rejection.
            if str(exc) in (_SESSION_REFUSED, _NO_SESSION):
                result["login_refused"] = "session_refused"
            elif getattr(exc, "kind", None) == "auth":
                result["login_refused"] = "held" if str(exc) == _HELD_AUTH else "rejected"
            return result
        except Exception as exc:
            # The class name only: the exception text may hold a URL or a secret.
            logger.warning("kms_lighthouse test_connection failed unexpectedly: %s", type(exc).__name__)
            return {"success": False, "message": "KMS connection test failed."}

    def _layout_refused(self) -> bool:
        """Whether KMS answers 400 for the configured layout (the API doc's answer to a layout that is not valid).

        One plain GET through ``_send``, with no re-login: a tenant that refuses
        this call for another reason must not cost a login on every status sweep,
        and the search itself is what matters, so any other outcome is ignored.
        """
        if self.layout_id is None:
            return False
        try:
            status, _ = self._send(self._session(), "layout_filters", "GET", "/search/global/filters",
                                   params={"layoutId": str(self.layout_id)})
        except KmsHTTPError as exc:
            logger.debug("kms_lighthouse layout_filters ignored: %s", exc.kind)
            return False
        return status == 400

    def diagnostics(self, deadline=None) -> dict:
        """The on-site diagnostics report: a plain dict that never holds content.

        ``deadline`` (a ``time.monotonic()`` value) may only shorten the run.
        ``kms_lighthouse_diagnostics`` holds the checks and the safety rules.
        """
        from app.data_sources.clients.kms_lighthouse_diagnostics import run_diagnostics
        return run_diagnostics(self, deadline)

    @_closed_errors
    def search_files(self, query, limit=100, deadline=None, cancel_check=None, **_):
        """Full-text search as this identity: typed, plain-text, scrubbed entries.

        Pages of 50 until ``min(limit, max_results)`` entries or a short page.
        Reaching that cap while more results exist (a usable record past it,
        or the cap filled on a full page) raises ``SearchStopped("max_results",
        files)``, and so do twenty full pages without reaching the cap (more
        results may exist).
        ``deadline`` (a ``time.monotonic()`` value) and ``cancel_check`` are
        checked before every page, and the deadline also bounds each request's
        timeouts; a stop, or a request that times out at the deadline, raises
        ``SearchStopped`` with the entries found so far. A record whose id is
        not plain digits is skipped (and counted in a debug log); an answer
        with records but none usable and none merely hidden is a ``server``
        error, not "no hits".
        """
        cap = self._search_cap(limit)
        text = _clean_query(query)
        if not text or cap <= 0:
            return []
        files, seen, skipped, hidden = [], set(), 0, 0
        try:
            for page in range(_MAX_SEARCH_PAGES):
                if cancel_check is not None and cancel_check():
                    raise SearchStopped("cancelled", files)
                if deadline is not None and time.monotonic() >= deadline:
                    raise SearchStopped("deadline", files)
                params = {"query": text, "start": str(page * _SEARCH_PAGE_ROWS), "rows": str(_SEARCH_PAGE_ROWS)}
                if self.layout_id is not None:
                    params["layoutId"] = str(self.layout_id)
                timed_out = False
                try:
                    results = self._search_page(params, deadline)
                except _DeadlineTimeout:
                    timed_out = True
                # Raised outside the except block, so no exception chain rides along.
                if timed_out:
                    raise SearchStopped("deadline", files)
                for record in results:
                    if not _DIGITS_RE.fullmatch(str(record.get("id", ""))):
                        skipped += 1
                        continue
                    entry = self._search_entry(record)
                    if entry is None or entry["id"] in seen:
                        hidden += 1
                        continue
                    if len(files) >= cap:
                        # A usable entry past the cap: more results exist.
                        raise SearchStopped("max_results", files)
                    seen.add(entry["id"])
                    files.append(entry)
                if len(files) >= cap:
                    if len(results) >= _SEARCH_PAGE_ROWS:
                        # The cap filled on a full page: more results may exist.
                        raise SearchStopped("max_results", files)
                    return files
                if len(results) < _SEARCH_PAGE_ROWS:
                    break
            else:
                # The page bound was hit on a full page, short of the cap.
                _refuse_if_none_usable(skipped, files, hidden)
                raise SearchStopped("max_results", files)
            _refuse_if_none_usable(skipped, files, hidden)
            return files
        finally:
            if skipped:
                logger.debug("kms_lighthouse search skipped %d records without a numeric id", skipped)

    def _search_cap(self, limit) -> int:
        """``min(limit, max_results)``; a limit that is not a whole number is a fixed error."""
        try:
            cap = int(100 if limit is None else limit)
        except (TypeError, ValueError, OverflowError):
            cap = None
        # Raised outside the except block, so no parser error rides along as context.
        if cap is None:
            raise KmsError(_BAD_LIMIT)
        return min(cap, self.max_results)

    def _search_page(self, params: dict, deadline=None) -> list:
        rejected = False
        try:
            payload = self._request("search", "/search/full", params=params, deadline=deadline)
        except KmsHTTPError as exc:
            if exc.kind != "bad_request" or isinstance(exc, _LoginFailure):
                raise
            rejected = True
        # Raised outside the except block. The PDF: a 400 with no body means the query is too long.
        if rejected:
            raise KmsHTTPError(400, "bad_request", _SEARCH_REJECTED)
        block = payload.get("regularSearch") if isinstance(payload, dict) else None
        results = block.get("results") if isinstance(block, dict) else None
        if isinstance(block, dict) and results is None and _zero_total(block):
            results = []    # an explicit "no hits"; a missing list alone is an unexpected answer
        if not isinstance(results, list) or not all(isinstance(r, dict) for r in results):
            logger.debug("kms_lighthouse %s refused: %s", "search", "results are not a list of records")
            raise KmsHTTPError(200, "server")
        return results

    def _search_entry(self, record: dict) -> dict | None:
        kind = _kind_of(record)
        entry_id = _make_id(kind, record.get("id", ""))
        online = _is_online(record)
        if kind == "folder" or (online is False and not self.include_offline):
            return None   # search lists no folders (path == name); offline content is hidden
        name = _entry_path((), record)[-1]
        return {
            "id": entry_id, "name": name, "path": name, "size": 0,
            "modified_at": _normalise_date(record.get("updateDate") or record.get("updatedDate")),
            "mime_type": "text/plain", "is_folder": False, "kind": kind,
            "status": None if online is None else ("online" if online else "offline"),
            "snippet": _without_controls(_plain_text(record.get("description")))[:_SNIPPET_CHARS],
            "web_url": None,
        }

    # ------------------------------------------------------------------ browse
    @_closed_errors
    def list_files(self, folder_id=None, recursive=None, limit=None, progress_callback=None, folder_calls=None):
        """Browse the folder tree as this identity, breadth first, from the root (or the top level).

        ``folder_id`` is a folder path (the folder part of an entry's path,
        ADR-7) or ``folder:<digits>``; with ``root_folder_id`` set, a folder id
        must be the root or lie inside it (``GlobScopeError``). Entry paths are
        folder segments from the root (or the top level) plus the name, also
        for a ``folder:<digits>`` listing, whose folder is found by a bounded
        walk; a folder id not found is ``not_found`` (also an offline folder,
        unless ``include_offline``), one the walk cannot reach within the call
        bound is ``not_found`` naming the browse limit, and one found with no
        call left to list it is ``not_found`` saying it was found.
        ``recursive`` defaults to True with no folder and to False with one. At
        most ``min(limit, max_results)`` articles and scenarios (None:
        ``max_results``; 0 or a limit that is not a whole number is refused,
        ``_list_cap``) and ``_MAX_FOLDER_CALLS`` tree calls; past either bound
        the listing just stops. Folders do not count against ``limit``, because
        the shared list_files tool drops them and a tree full of them would read
        as empty; the tree-call bound keeps them bounded, and ``folder_calls``
        may only lower it. Offline content is hidden unless ``include_offline``.
        """
        target = _folder_target(folder_id)       # refused before any request
        cap = self._list_cap(limit)
        # "/" names the origin, as a blank folder does; the shared tool sends recursive=False with any folder_id.
        slash = target is None and folder_id not in (None, "")
        deep = slash or (target is None if recursive is None else _flag(recursive))
        budget = _CallBudget(min(folder_calls or _MAX_FOLDER_CALLS, _MAX_FOLDER_CALLS))
        start, start_path = self._resolve_folder(target, budget)
        files, listed, content = [], set(), 0
        queue, queued = deque([(start, start_path)]), {start}
        while queue:
            parent, parent_path = queue.popleft()
            level = self._level(parent, budget)
            if level is None:
                if (parent, parent_path) == (start, start_path):
                    raise KmsHTTPError(404, "not_found", _FOUND_BEYOND_BROWSE_LIMIT)   # no call left to list it
                break                             # the folder-call cap
            records, segments = level
            for record in records:
                kind = _kind_of(record)
                entry_id = _make_id(kind, record["id"])
                if entry_id in listed:
                    continue
                if kind == "folder":
                    folder = str(record["id"])
                    path = (*parent_path, segments[folder])
                    if deep and folder not in queued:
                        queued.add(folder)
                        queue.append((folder, path))
                else:
                    path = _entry_path(parent_path, record)
                    content += 1
                listed.add(entry_id)
                files.append(self._list_entry(record, entry_id, kind, path))
                if content >= cap:
                    return files
        return files

    def _list_cap(self, limit) -> int:
        """``min(limit, max_results)``; None is ``max_results``.

        As the shared list_files tool (``limit >= 1``), 0 is refused, as an int
        and as text alike; so is anything that is not a whole number (2.5,
        "2.5", a bool): never truncated.
        """
        wanted = None
        if limit is None:
            wanted = self.max_results
        elif isinstance(limit, int) and not isinstance(limit, bool):
            wanted = limit
        elif isinstance(limit, float) and limit.is_integer():
            wanted = int(limit)
        elif isinstance(limit, str):
            try:
                wanted = int(limit)
            except ValueError:
                wanted = None
        # Raised outside the except block, so no parser error rides along as context.
        if wanted is None or wanted < 1:
            raise KmsError(_BAD_LIST_LIMIT)
        return min(wanted, self.max_results)

    def _hidden(self, record: dict) -> bool:
        return _is_online(record) is False and not self.include_offline

    def _level(self, parent: str | None, budget: _CallBudget):
        """``(shown records, folder id -> segment)`` under ``parent`` (None: the top level), or None past the budget.

        Levels are cached per instance; only a real tree call takes from the budget.
        """
        data = self._tree_cache.get(parent)
        if data is None:
            if not budget.take():
                return None
            data = self._tree_cache[parent] = self._tree_children(parent)
        shown = [record for record in data if not self._hidden(record)]
        return shown, _folder_segments([record for record in shown if _kind_of(record) == "folder"])

    def _resolve_folder(self, target, budget: _CallBudget) -> tuple[str | None, tuple[str, ...]]:
        """The folder to list (None: the top level) and its path segments from the root (or the top level)."""
        origin = None if self.root_folder_id is None else str(self.root_folder_id)
        if target is None:
            return origin, ()
        how, value = target
        if how == "id":
            if value == origin:
                return value, ()
            # Found from the root (or the top level), so its entries get paths that resolve back.
            path = self._find_inside_root(value, origin, budget)
            if path is None:
                if origin is None:
                    raise KmsHTTPError(404, "not_found", _FOLDER_NOT_FOUND)
                raise GlobScopeError(_OUTSIDE_ROOT)
            return value, path
        # A folder path: one level per segment from the root, so it is inside the root by construction.
        current = origin
        for segment in value:
            level = self._level(current, budget)
            if level is None:
                raise KmsHTTPError(404, "not_found", _BEYOND_BROWSE_LIMIT)
            match = next((folder for folder, seg in level[1].items() if seg == segment), None)
            if match is None:
                raise KmsHTTPError(404, "not_found", _FOLDER_NOT_FOUND)
            current = match
        return current, value

    def _find_inside_root(self, folder: str, origin: str | None, budget: _CallBudget) -> tuple[str, ...] | None:
        """The path of ``folder`` by a bounded breadth-first walk from ``origin`` (None: the top level).

        None when the whole walk ends without it. Shown folders are opened
        first; then the walk also opens hidden (offline) folders and the folders
        under them, within the same bound, so a folder under one is found: a
        hidden folder, or one under a hidden folder, is ``not_found``. A hidden
        folder the server refuses to list for its own sake (``_concerns_one_branch``)
        is left unseen, and a walk that skipped one ends ``not_found``, since the
        folder may be in it; any other failure (a refused session, a failed
        login, throttling, a gateway 502/503/504, no connection, a redirect)
        raises at once. A walk that runs out of tree
        calls is ``not_found`` naming the browse limit; never a false "outside root".
        """
        shown, hidden, seen, skipped = deque([(origin, ())]), deque(), {origin}, False
        while shown or hidden:
            under_hidden = not shown
            parent, parent_path = hidden.popleft() if under_hidden else shown.popleft()
            level, failed = None, None
            try:
                level = self._level(parent, budget)
            except KmsHTTPError as exc:
                failed = exc
            if failed is not None:
                # Raised outside the except block, so nothing rides along as context.
                if not under_hidden or not _concerns_one_branch(failed):
                    raise failed
                logger.debug("kms_lighthouse folder lookup skipped a hidden branch: %s", failed.kind)
                skipped = True
                continue
            if level is None:
                raise KmsHTTPError(404, "not_found", _BEYOND_BROWSE_LIMIT)
            segments = level[1]
            for record in self._tree_cache[parent]:
                child = str(record["id"])
                if _kind_of(record) != "folder" or child in seen:
                    continue
                child_hidden = under_hidden or child not in segments
                if child == folder:
                    if child_hidden:
                        raise KmsHTTPError(404, "not_found", _FOLDER_NOT_FOUND)
                    return (*parent_path, segments[child])
                seen.add(child)
                if child_hidden:
                    hidden.append((child, ()))
                else:
                    shown.append((child, (*parent_path, segments[child])))
        if skipped:
            raise KmsHTTPError(404, "not_found", _FOLDER_NOT_FOUND)
        return None

    @staticmethod
    def _list_entry(record: dict, entry_id: str, kind: str, path: tuple[str, ...]) -> dict:
        online = _is_online(record)
        is_folder = kind == "folder"
        return {
            "id": entry_id, "name": path[-1], "path": "/".join(path), "size": 0,
            "modified_at": _normalise_date(record.get("updateDate") or record.get("updatedDate")),
            "mime_type": None if is_folder else "text/plain", "is_folder": is_folder, "kind": kind,
            "status": None if online is None else ("online" if online else "offline"),
            "snippet": None, "web_url": None,
        }

    @discovery_progress
    def get_schemas(self, progress_callback=None):
        # No catalog (ADR-1): search and browse are always live.
        return []

    def get_schema(self, table_name):
        return None

    def prompt_schema(self):
        return ""

    # ------------------------------------------------------------------ read
    @_closed_errors
    def read_file(self, file_id, sheet=None, max_bytes=None, page_range=None, **_):
        """An article (``item:<digits>`` or bare digits) or a scenario as scrubbed plain text.

        The id is checked before any request; a ``folder:`` id is refused. An
        article comes from ``/public/item``: a hidden (offline) one is
        ``not_found``; its text is the ai-ready text when KMS has it (only the
        elements ``/public/item`` also returned), else the item HTML. Bare
        digits that ``/public/item`` does not know are tried once as a
        scenario, labelled ``scenario:<digits>`` (ADR-5); an explicit
        ``item:`` id never switches kind. Returns ``DocumentText`` named
        ``<title>.txt``; ``sheet``, ``max_bytes`` and ``page_range`` do not
        apply to KMS text.
        """
        text = str(file_id)
        kind, raw = _parse_id(text)
        if kind == "folder":
            raise KmsError(_FOLDER_READ)
        digits, bare = str(int(raw)), _KIND_PREFIX_RE.match(text) is None
        if kind == "scenario":
            return self._read_scenario(digits)
        item, unknown = None, False
        try:
            item = self._request("item", f"/public/item/{digits}", missing="not_found")
        except KmsHTTPError as exc:
            if not (bare and exc.kind == "not_found" and not isinstance(exc, _LoginFailure)):
                raise
            unknown = True
        if unknown:
            return self._read_scenario(digits)      # once, for bare digits only (ADR-5)
        return self._read_article(digits, item)

    @_closed_errors
    def read_raw_bytes(self, file_id, *, max_bytes=None):
        """``(utf-8 text, name, "text/plain")`` of ``read_file``; ``FileTooLargeError`` above ``max_bytes``."""
        text = self.read_file(file_id)
        data = text.encode("utf-8")
        if max_bytes and len(data) > max_bytes:
            raise FileTooLargeError(_TOO_LARGE_READ)
        return data, text.name, "text/plain"

    @_closed_errors
    def execute_query(self, query=None, table_name=None, **kwargs):
        """Read one entry: a JSON ``{"file_id": ...}`` (as Documentum accepts) or the id itself."""
        text = "" if query is None else str(query)
        if not text.lstrip().startswith("{"):
            return self.read_file(table_name or text or kwargs.get("file_id", ""))
        params = None
        try:
            params = json.loads(text)
        except (ValueError, RecursionError):
            params = None
        # Raised outside the except block, so no parser error (it quotes the query) rides along.
        if not isinstance(params, dict) or "file_id" not in params:
            raise KmsError(_BAD_QUERY)
        return self.read_file(params["file_id"])

    def _read_article(self, digits: str, item) -> DocumentText:
        if not isinstance(item, dict) or not _same_id(item.get("id"), digits):
            logger.debug("kms_lighthouse %s refused: %s", "item", "not the item asked for")
            raise KmsHTTPError(200, "server")
        if self._hidden(item):
            raise KmsHTTPError(404, "not_found")
        # No elements: absent, null, {} or []. Any other shape that is not a record map fails closed.
        elements = item.get("elements")
        if elements is not None and elements != [] and not isinstance(elements, dict):
            logger.debug("kms_lighthouse %s refused: %s", "item", "elements is not a record map")
            raise KmsHTTPError(200, "server")
        records = _elements(item)        # once per read, so a dropped element is logged once
        version, language = _version(item.get("version")), _language(item.get("language"))
        sections = self._ai_text(digits, version, language, records) if version and language else None
        if sections is None:
            sections = _item_sections(item, records)
        sub_type = _line(item.get("subType") or item.get("sub_type"))
        kind = f"article ({sub_type})" if sub_type else "article"
        lines = _header(item.get("title"), kind, item, version, language) + _render_sections(sections)
        return DocumentText("\n".join(lines), raw=None, name=_safe_title(item.get("title")) + ".txt",
                            mime="text/plain")

    def _ai_text(self, digits: str, version: str, language: str, records: dict) -> list | None:
        """The ai-ready sections of this read, or None to fall back to the item HTML.

        ``/public/item`` has already proved this identity may read the item,
        so a failure about this one item (``_concerns_one_item``: a 4xx, a
        plain 500, an empty, non-JSON or ERROR answer) falls back, for this
        read alone. Anything that concerns every call raises: a failed login,
        a refused session, throttling, a redirect, an unreachable KMS or a
        502/503/504 gateway answer is never hidden behind other text. An
        answer for another item, or with no element ``/public/item`` shows,
        falls back too.
        """
        payload, failed = None, None
        try:
            payload = self._request("ai_text", "/ai_answers_item", missing="not_found",
                                    params={"itemId": digits, "version": version, "language": language})
        except KmsHTTPError as exc:
            failed = exc
        # Raised outside the except block, so nothing rides along as context.
        if failed is not None:
            if not _concerns_one_item(failed):
                raise failed
            logger.debug("kms_lighthouse %s fell back: %s", "ai_text", failed.kind)
            return None
        if not isinstance(payload, dict) or ("item_id" in payload and not _same_id(payload["item_id"], digits)):
            logger.debug("kms_lighthouse %s fell back: %s", "ai_text", "not the item asked for")
            return None
        return _ai_sections(payload.get("elements"), records)

    def _read_scenario(self, digits: str) -> DocumentText:
        """A scenario: its header lines, then its decision tree (``_render_scenario``) under "## Steps".

        No steps (absent, null, ``{}`` or ``[]``), or steps that render no
        line, is the header alone; any other shape that is not a record
        fails closed.
        """
        payload = self._request("scenario", f"/public/scenario/{digits}", missing="not_found",
                                params=_SCENARIO_PARAMS, headers={"Accept": _SCENARIO_ACCEPT})
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict) or not _same_id(data.get("id"), digits):
            logger.debug("kms_lighthouse %s refused: %s", "scenario", "not the scenario asked for")
            raise KmsHTTPError(200, "server")
        if self._hidden(data):
            raise KmsHTTPError(404, "not_found")
        steps = data.get("steps")
        if steps is not None and steps != [] and not isinstance(steps, dict):
            logger.debug("kms_lighthouse %s refused: %s", "scenario", "steps is not a record")
            raise KmsHTTPError(200, "server")
        lines = _header(data.get("title"), f"scenario (scenario:{digits})", data)
        tree = _render_scenario(steps, functools.partial(self._fetch_step, digits)) if steps else []
        if tree:
            lines += ["", "## Steps"] + tree
        return DocumentText("\n".join(lines), raw=None, name=_safe_title(data.get("title")) + ".txt",
                            mime="text/plain")

    def _fetch_step(self, digits: str, step_id: str) -> dict | None:
        """One step of scenario ``digits`` from the step endpoint, or None when it is not available.

        The scenario itself was read, so a failure about this one step
        (``_concerns_one_item``) or an answer for another scenario or step is
        None, logged with its kind or reason only; anything that concerns
        every call raises.
        """
        payload, failed = None, None
        try:
            payload = self._request("step", f"/public/scenario/{digits}/step/{step_id}", missing="not_found",
                                    params=_SCENARIO_PARAMS, headers={"Accept": _SCENARIO_ACCEPT})
        except KmsHTTPError as exc:
            failed = exc
        # Raised outside the except block, so nothing rides along as context.
        if failed is not None:
            if not _concerns_one_item(failed):
                raise failed
            logger.debug("kms_lighthouse %s dropped: %s", "step", failed.kind)
            return None
        data = payload.get("data") if isinstance(payload, dict) else None
        step = data.get("steps") if isinstance(data, dict) else None
        if (not isinstance(step, dict) or not _same_id(data.get("id"), digits)
                or _step_key(step.get("stepId")) != step_id):
            logger.debug("kms_lighthouse %s dropped: %s", "step", "not the step asked for")
            return None
        return step
