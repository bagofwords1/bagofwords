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
import json
import logging
import os
import re
import threading
import time
import unicodedata
from collections import OrderedDict
from html.entities import html5 as _HTML5_ENTITIES
from datetime import datetime
from urllib.parse import unquote, urlsplit

import requests
import urllib3

from app.data_sources.clients.base import Capability, DataSourceClient, SearchStopped
from app.data_sources.clients._file_source_common import FileTooLargeError, GlobScopeError
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
_TOP_LEVEL_HINT = (
    "Connected to KMS Lighthouse, but the top level of the folder tree came back empty. Set Root folder ID "
    "to a folder this account can open, or check the account's KMS permissions."
)
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
_URL_MESSAGE = ("KMS Lighthouse URL must be an HTTP(S) URL such as https://example.lighthouse-cloud.com, "
                "without a user name or password in the URL, a query, or a fragment.")


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


def _is_timeout(exc: BaseException) -> bool:
    """A connect or read timeout, also when requests wraps urllib3's (a read timeout while streaming)."""
    timeouts = (requests.Timeout, urllib3.exceptions.TimeoutError, TimeoutError)
    return isinstance(exc, timeouts) or any(isinstance(arg, timeouts) for arg in exc.args)


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
_KIND_PREFIX_RE = re.compile(r"^(item|scenario|folder):", re.IGNORECASE)
# Every pattern below is linear: possessive runs never backtrack, and every
# Python scan is bounded. Titles and descriptions are scrubbed at full length
# (a cut before the scrub could split a secret), and so are article bodies.
# A JWT is any base64url run that starts like a JWT header ("eyJ"), with up to
# two dotted parts; whitespace (left by a <br>) may sit next to a dot.
_JWT_RE = re.compile(r"eyJ[A-Za-z0-9_-]{4,}+(?:\s{0,3}\.\s{0,3}[A-Za-z0-9_-]++){0,2}")
# A quote may open the token ('--oauth2-bearer "ya29.X"'); it stays with the scheme word.
_BEARER_RE = re.compile(r"(?i)\b(bearer\s++[\"']?+)([A-Za-z0-9._~+/=-]{8,}+)")
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
_MAX_SCRUB_DEPTH = 4          # rounds of percent-decoding a nested value gets
_DAY_FIRST_RE = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})$")


def _param_key(name: str) -> str:
    """A parameter name as compared: lower case, no "amp;" left from unescaping, no '_', '-' or '[]'."""
    key = name.strip().lower()
    while key.startswith("amp;"):
        key = key[len("amp;"):]
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
# "MY_SECRET", "TOKEN_HERE", "your_api_key_here", and a variable: "${DB_PASS}",
# "$DB_PASSWORD", "%API_KEY%"): kept as written.
_BRACKETED_PLACEHOLDER = r"<[a-z][a-z _-]{0,63}+>|\{[a-z][a-z _-]{0,63}+\}|\[[a-z][a-z _-]{0,63}+\]"
_BRACKETED_PLACEHOLDER_RE = re.compile("(?i)" + _BRACKETED_PLACEHOLDER)
_PLACEHOLDER_RE = re.compile(
    "(?i)" + _BRACKETED_PLACEHOLDER + r"|\*{3,}+|x{3,}+|\.{3,}+"
    r"|(?:your_?)?password|pass|passwd|secret|token|changeme"
    r"|(?:(?:your|my)_)?(?:api_)?(?:key|token|secret|password|pass|pwd)(?:_here)?"
    r"|\$\{[a-z_][a-z0-9_]{0,61}+\}|\$[a-z_][a-z0-9_]{0,63}+|%[a-z_][a-z0-9_]{0,63}+%")
_MAX_PLACEHOLDER_CHARS = 66
# A value that is a URL ("proxy_pass: http://backend:8080" names an upstream, not a password).
_URL_VALUE_RE = re.compile(r"(?i)(?:https?|ftp|wss?)://")


def _is_placeholder(value: str) -> bool:
    """A placeholder; an earlier "[redacted]" is none (a scheme word before it still goes with it)."""
    return value != _REDACTED and _PLACEHOLDER_RE.fullmatch(value) is not None


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
        credential_at = start
        if not quoted:
            # A bracketed placeholder may hold blanks ("api_key: <your api key>"); the match is bounded.
            bracketed = None if text.startswith(_REDACTED, start) else _BRACKETED_PLACEHOLDER_RE.match(text, start)
            if bracketed and _STRUCTURED_VALUE_RE.match(text, bracketed.end()).end() == bracketed.end():
                continue
            end = _unquoted_value_end(text, start)
            if text[start:end].lower() in _SCHEME_WORDS:
                credential_at = _VALUE_GAP_RE.match(text, end).end()
                credential_end = _unquoted_value_end(text, credential_at)
                if credential_end == credential_at:
                    continue          # a scheme word with no credential after it is text ("X-Auth-Token: Bearer")
                end = credential_end
        # An unquoted "{placeholder" or "${VAR" stops before its '}'; a placeholder is short, so the check
        # reads a bounded run.
        braced = text.startswith(("{", "${"), credential_at)
        closer = "}" if not quoted and braced and text.startswith("}", end) else ""
        if end - credential_at <= _MAX_PLACEHOLDER_CHARS and _is_placeholder(text[credential_at:end] + closer):
            continue
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


def _redact_bearer(match: re.Match) -> str:
    value = match.group(2)
    # A token has a digit or one of "_~+/="; a word ("Bearer authentication", "token-based") stays.
    if any(ch.isdigit() or ch in "_~+/=" for ch in value):
        return match.group(1) + "[redacted]"
    return match.group(0)


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
#   * a token-only URL userinfo ("https://TOKEN@host", no ':');
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
#     "DB_PASSWORD=$DB_PASSWORD", "set API_KEY=%API_KEY%"); a real value of the
#     same shape is shown too ("db_password: $ecret1");
#   * shell quoting after an unquoted structured value: "curl -H 'X-Api-Key: S'"
#     loses the closing quote with the value.
# Over-redaction accepted: a camel-case word starting "eyJ" ("eyJumpsMonkey"), an
# identifier-shaped quoted key in prose ('the "api_key": field'), parameter names
# that end with a secret word ("nextPageToken", "showPassword"), and these
# crafted shapes: a dotted identifier-shaped key in prose ("etc.password: see
# below" loses "see"); a word after a scheme-word value ("x_token: Token based
# login" loses "based"; a scheme word with nothing after it on its line stays);
# bracket debris inside a value ("(api_key=S)" loses the ')', "api_key:
# Sv1[x9Qk2]abc" leaves "]abc" after the redaction); a stray ']' after a YAML flow
# list value; "Subscription-Key: found on your account page" loses "found".
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
_INLINE_TAG_RUN_RE = re.compile(
    r"(?i)(?:</?(?:wbr|span|b|i|u|em|strong|font|a|small|sup|sub|mark|code|s|del|ins|kbd|abbr|q|cite"
    r"|var|samp|tt)\b[^<>]*+>)++")
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
    text = _ENTITY_RE.sub(_unescape_entity, text).replace("\xa0", " ")
    lines = [_BLANKS_RE.sub(" ", line).strip() for line in text.splitlines()]
    return _BLANK_LINES_RE.sub("\n\n", "\n".join(lines)).strip()


def _plain_text(value) -> str:
    """A KMS title or description as plain, scrubbed text, at full length (callers cut the result)."""
    return _scrub(_html_to_text("" if value is None else str(value)))


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


def planner_scope_note(config: dict) -> str:
    """The planner-facing browse note for a KMS connection (pure, no I/O)."""
    cfg = config or {}
    max_results = cfg.get("max_results") or _DEFAULT_MAX_RESULTS
    root = cfg.get("root_folder_id")
    layout = cfg.get("layout_id")
    origin = f"the root folder (id {root})" if root is not None else "the top level"
    in_layout = f" in layout {layout}" if layout is not None else ""
    return (
        f"KMS Lighthouse: list_files with no folder_id returns at most {max_results} articles and scenarios "
        f"from {origin}, and stops there without saying so (the listing can be incomplete). To open a folder, "
        "pass the folder part of an entry's path as folder_id (for example 'Procedures/Billing') with "
        "recursive=true; folder ids are not shown. The root folder limits browsing only: search_files covers "
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
        self.layout_id = int(layout_id) if layout_id not in (None, "") else None
        self.root_folder_id = int(root_folder_id) if root_folder_id not in (None, "") else None
        self.include_offline = _flag(include_offline)
        self.max_results = int(max_results or _DEFAULT_MAX_RESULTS)
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
            raise KmsError("Provide a KMS username and password or an API token.")
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
            f"no folder lists up to {self.max_results} articles and scenarios from the {origin}, each with a "
            "path of folder names; to open one folder, pass the folder part of a path as folder_id (for example "
            "folder_id='Procedures/Billing') and set recursive=true to include its subfolders. Read with "
            f"read_file and the entry's id (item:<id> or scenario:<id>). {shown}. The root folder limits browsing only; search covers everything the identity can see"
            f"{layout}."
        )

    # ------------------------------------------------------------------ auth
    def _new_session(self) -> requests.Session:
        session = requests.Session()
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
            raise KmsHTTPError(401, "auth", _HELD_AUTH)

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
        """
        entry = self._entry
        with entry.login_lock:
            if entry.generation == used_generation or not self._is_young(entry):
                self._check_hold(entry)
                self._login(self._new_session())

    def _rejected(self) -> KmsHTTPError:
        """KMS refused this login: hold new attempts for this identity."""
        with self._entry.state_lock:
            self._entry.failed_at = _clock()
        logger.info("kms_lighthouse login rejected; holding new logins for %d s", _FAILED_LOGIN_HOLD_S)
        return KmsHTTPError(401, "auth", _USERPASS_AUTH)

    def _login(self, session: requests.Session) -> None:
        """The only place that authenticates (a hash login would plug in here).

        Called with the pool entry's login_lock held.
        """
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
        status, body = self._send(session, "login", "POST", "/login",
                                  params={"username": self._username, "password": self._password})
        if status in (400, 401, 403):
            raise self._rejected()
        if status != 200:
            raise KmsHTTPError(status, _kind_for_status(status))
        payload = self._decode(body, "login")
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
    def _send(self, session, op, method, path, *, params=None, headers=None, deadline=None):
        """One HTTP exchange. Returns (status, body bytes); never raises with a URL.

        With a ``deadline`` (a ``time.monotonic()`` value) the connect and read
        timeouts are cut to the time left, but never below one second.
        """
        url = self._base + self.api_prefix + path
        started = time.monotonic()
        timeout = _DEFAULT_TIMEOUT
        if deadline is not None:
            left = max(_MIN_TIMEOUT_S, deadline - started)
            timeout = (min(_DEFAULT_TIMEOUT[0], left), min(_DEFAULT_TIMEOUT[1], left))
        status, body, failed, cause, timed_out = 0, b"", None, None, False
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
                    body = b"".join(chunks)
        except _TRANSPORT_ERRORS as exc:
            failed, cause = "unreachable", type(exc).__name__
            timed_out = _is_timeout(exc)
        finally:
            _IN_KMS_REQUEST.active = False
        # Raised outside the except block, so no exception chain carries the URL.
        if failed:
            # The class name tells SSL, DNS and timeouts apart; never the text (it holds the URL).
            logger.debug("kms_lighthouse %s failed: %s (%s)", op, failed, cause or "size cap")
            if timed_out and deadline is not None and deadline - time.monotonic() < _MIN_TIMEOUT_S:
                raise _DeadlineTimeout()    # the deadline cut this timeout: the caller's time is up
            raise KmsHTTPError(status if failed == "too_large" else 0, failed)
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

    def _tree_children(self, parent_id: int | None) -> list:
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
            identity = identity or (self._username if self.auth_method == "userpass" else "the API token's account")
            note = " The root folder came back empty." if not children else ""
            return {
                "success": True,
                "message": f"Connected to KMS Lighthouse at {self.host} as {identity}.{note}",
                "details": {"host": self.host, "identity": identity, "auth_method": self.auth_method,
                            "root_folder_id": self.root_folder_id},
            }
        except (KmsError, GlobScopeError) as exc:
            logger.info("kms_lighthouse test_connection failed: %s", getattr(exc, "kind", "error"))
            return {"success": False, "message": str(exc)}
        except Exception as exc:
            # The class name only: the exception text may hold a URL or a secret.
            logger.warning("kms_lighthouse test_connection failed unexpectedly: %s", type(exc).__name__)
            return {"success": False, "message": "KMS connection test failed."}

    @_closed_errors
    def search_files(self, query, limit=100, deadline=None, cancel_check=None, **_):
        """Full-text search as this identity: typed, plain-text, scrubbed entries.

        Pages of 50 until ``min(limit, max_results)`` entries or a short page.
        Twenty full pages without reaching the cap raise
        ``SearchStopped("max_results", files)`` (more results may exist).
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
                    seen.add(entry["id"])
                    files.append(entry)
                    if len(files) >= cap:
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
            if exc.kind != "bad_request":
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
        name = _safe_title(record.get("title")) + ".txt"
        return {
            "id": entry_id, "name": name, "path": name, "size": 0,
            "modified_at": _normalise_date(record.get("updateDate") or record.get("updatedDate")),
            "mime_type": "text/plain", "is_folder": False, "kind": kind,
            "status": None if online is None else ("online" if online else "offline"),
            "snippet": _without_controls(_plain_text(record.get("description")))[:_SNIPPET_CHARS],
            "web_url": None,
        }

    @discovery_progress
    def get_schemas(self, progress_callback=None):
        # No catalog (ADR-1): search and browse are always live.
        return []

    def get_schema(self, table_name):
        return None

    def prompt_schema(self):
        return ""

    def execute_query(self, query=None, table_name=None, **kwargs):
        raise KmsError("Reading by query is not available yet on this connection.")
