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

import functools
import hashlib
import hmac
import http.client
import json
import logging
import os
import re
import threading
import time
import unicodedata
from collections import OrderedDict
from datetime import datetime
from urllib.parse import unquote, urlsplit

import requests
import urllib3

from app.data_sources.clients.base import Capability, DataSourceClient, SearchStopped
from app.data_sources.clients._file_source_common import FileTooLargeError, GlobScopeError
from app.data_sources.clients.mail_common import strip_html
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
# A JWT, also when whitespace (left by a <br> or <wbr>) sits next to a dot.
_JWT_RE = re.compile(r"eyJ[A-Za-z0-9_-]+\s{0,3}\.\s{0,3}[A-Za-z0-9_-]+\s{0,3}\.(?:\s{0,3}[A-Za-z0-9_-]+)?")
_BEARER_RE = re.compile(r"(?i)\b(bearer\s+)([A-Za-z0-9._~+/=-]{8,})")
# A parameter list: from any separator up to whitespace, a quote or a tag bracket.
_PARAMS_RE = re.compile(r"[?#;&][^\s\"'<>]*")
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
    "x-amz-security-token", "x-amz-signature", "x-amz-credential"))


def _without_controls(text: str) -> str:
    """Control, format and surrogate characters become spaces; whitespace is collapsed."""
    return " ".join("".join(" " if unicodedata.category(ch)[0] == "C" else ch for ch in text).split())


def _is_invisible(ch: str) -> bool:
    """A format character (soft hyphen, zero-width space, bidi control) or a non-whitespace control."""
    category = unicodedata.category(ch)
    return category == "Cf" or (category == "Cc" and not ch.isspace())


def _scrub_part(part: str, depth: int) -> str | None:
    """One ``name=value`` part: None when it is secret, else the part (scrubbed if it decodes)."""
    decoded = unquote(part)
    name, equals, _value = decoded.partition("=")
    if equals and _param_key(name) in _SECRET_PARAMS:
        return None
    if decoded == part or depth >= _MAX_SCRUB_DEPTH:
        return part
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


def _redact_bearer(match: re.Match) -> str:
    value = match.group(2)
    # A credential has a digit or punctuation; a plain word ("Bearer authentication") stays.
    if any(ch.isdigit() or ch in "._~+/=-" for ch in value):
        return match.group(1) + "[redacted]"
    return match.group(0)


def _scrub_at(text: str, depth: int) -> str:
    text = "".join(ch for ch in text if not _is_invisible(ch))
    text = _PARAMS_RE.sub(lambda m: _scrub_params(m.group(0), depth), text)
    text = _JWT_RE.sub("[redacted]", text)
    return _BEARER_RE.sub(_redact_bearer, text)


def _scrub(text: str) -> str:
    """Drop secret-named URL parameters, redact JWTs and Bearer credentials (pure).

    Run after ``strip_html``, so entity-encoded and plain-text URLs are caught
    too. Format characters and non-whitespace controls are dropped first, so
    an invisible character cannot split a secret; whitespace (newlines
    included) is kept. Query strings, fragments and ';' path parameters are
    split on '&' and ';'; each part is percent-decoded before its name is
    compared (lower case, without '_', '-' and '[]'), and a part that decodes
    to something else (a nested URL) is scrubbed again in decoded form.
    """
    return _scrub_at(text, 0)


def _safe_title(title) -> str:
    """A KMS title as one path segment the folder-path rules accept (Durable Decisions)."""
    text = _without_controls(_scrub(strip_html("" if title is None else str(title))).replace("/", "-"))
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
        status, body, failed, cause = 0, b"", None, None
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
        finally:
            _IN_KMS_REQUEST.active = False
        # Raised outside the except block, so no exception chain carries the URL.
        if failed:
            # The class name tells SSL, DNS and timeouts apart; never the text (it holds the URL).
            logger.debug("kms_lighthouse %s failed: %s (%s)", op, failed, cause or "size cap")
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
        ``SearchStopped("catalog_limit", files)`` (more results may exist).
        ``deadline`` (a ``time.monotonic()`` value) and ``cancel_check`` are
        checked before every page, and the deadline also bounds each request's
        timeouts; a stop raises ``SearchStopped`` with the entries found so
        far. A record whose id is not plain digits is skipped (and counted in
        a debug log).
        """
        cap = self._search_cap(limit)
        text = _clean_query(query)
        if not text or cap <= 0:
            return []
        files, seen, skipped = [], set(), 0
        try:
            for page in range(_MAX_SEARCH_PAGES):
                if cancel_check is not None and cancel_check():
                    raise SearchStopped("cancelled", files)
                if deadline is not None and time.monotonic() >= deadline:
                    raise SearchStopped("deadline", files)
                params = {"query": text, "start": str(page * _SEARCH_PAGE_ROWS), "rows": str(_SEARCH_PAGE_ROWS)}
                if self.layout_id is not None:
                    params["layoutId"] = str(self.layout_id)
                results = self._search_page(params, deadline)
                for record in results:
                    if not _DIGITS_RE.fullmatch(str(record.get("id", ""))):
                        skipped += 1
                        continue
                    entry = self._search_entry(record)
                    if entry is None or entry["id"] in seen:
                        continue
                    seen.add(entry["id"])
                    files.append(entry)
                    if len(files) >= cap:
                        return files
                if len(results) < _SEARCH_PAGE_ROWS:
                    return files
            # The page bound was hit on a full page, short of the cap.
            raise SearchStopped("catalog_limit", files)
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
        description = record.get("description")
        return {
            "id": entry_id, "name": name, "path": name, "size": 0,
            "modified_at": _normalise_date(record.get("updateDate") or record.get("updatedDate")),
            "mime_type": "text/plain", "is_folder": False, "kind": kind,
            "status": None if online is None else ("online" if online else "offline"),
            "snippet": _without_controls(_scrub(strip_html("" if description is None else str(description))))[
                :_SNIPPET_CHARS],
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
