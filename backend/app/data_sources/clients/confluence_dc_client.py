"""Read-only Confluence Data Center / Server connector over the Confluence REST API.

Talks to ``{base_url}/rest/api/...`` only, where ``base_url`` carries the
site's context path (``https://host/confluence`` on a typical on-prem
install). Every call is a stateless GET authenticated as one identity, so
Confluence space permissions and page restrictions decide what is seen:

  * ``token``    - a personal access token (Data Center 7.9+), sent as
    ``Authorization: Bearer``.
  * ``userpass`` - username and password, sent as HTTP Basic.

Like KMS Lighthouse there is no catalog: search (CQL), browse and read are
live, with the caller's identity. Unlike KMS, the optional space list is a hard
scope: search is limited to it in CQL, browsing never leaves it, and a read of
a page in another space is refused.

Messages leaving this module are fixed strings: they never carry a request
URL, a provider response body or a credential.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
import threading
import time
from datetime import datetime
from urllib.parse import parse_qs, quote, unquote, urlsplit

import requests
import urllib3
import http.client

from app.core.egress import pinned_session
from app.data_sources.clients._confluence_html import html_to_text
from app.data_sources.clients._file_source_common import DocumentText, FileTooLargeError, GlobScopeError
from app.data_sources.clients.base import Capability, DataSourceClient, SearchStopped
from app.data_sources.clients.progress import discovery_progress
from app.schemas.data_sources.configs import CONFLUENCE_TOKEN_FORMAT_MESSAGE, CONFLUENCE_TOKEN_RE

logger = logging.getLogger(__name__)

_DEFAULT_MAX_RESULTS = 200
_MAX_RESPONSE_BYTES = 16 * 1024 * 1024
_DEFAULT_TIMEOUT = (10, 60)          # (connect, read) seconds
_MIN_TIMEOUT_S = 1
_CHUNK_BYTES = 64 * 1024
_PAGE_ROWS = 50                      # requested per call; the server may cap it lower (25 on DC)
_MAX_SEARCH_CALLS = 20
_MAX_LIST_CALLS = 40
_MAX_SPACES = 100
_MAX_QUERY_CHARS = 200
_SNIPPET_CHARS = 300
_RATE_LIMIT_RETRIES = 2
_MAX_RETRY_AFTER_S = 10.0
_TRANSPORT_ERRORS = (requests.RequestException, urllib3.exceptions.HTTPError, http.client.HTTPException,
                     OSError, ValueError)

_CONTEXT_SEGMENT_RE = re.compile(r"^[A-Za-z0-9._~-]+$")
_SPACE_KEY_RE = re.compile(r"^~?[A-Za-z0-9_.@-]{1,255}$")
_DIGITS_RE = re.compile(r"[0-9]{1,19}")
_ID_RE = re.compile(r"^(page|blogpost|space):(.+)$", re.IGNORECASE)
_CQL_NOISE_RE = re.compile(r'[+\-&|!(){}\[\]^"~*?:\\/<>=;,]')
_HL_RE = re.compile(r"@@@(?:end)?hl@@@")
_SLASH = "/"
_SLASH_IN_TITLE = "∕"          # DIVISION SLASH: a "/" inside a title, so paths stay unambiguous

# --------------------------------------------------------------------- messages
_URL_MESSAGE = ("Confluence URL must be an HTTP(S) URL such as https://confluence.example.com or "
                "https://intranet.example.com/confluence (include the context path, if any).")
_HTTPS_REQUIRED = "HTTPS is required. Enable Allow HTTP only for an isolated test lab."
_NO_CREDENTIAL = "Provide a Confluence personal access token, or a username and password."
_BAD_SPACES = ("Spaces must be a comma-separated list of space keys such as ENG, HR or ~jdoe "
               "(the key from the space's URL, not its name).")
_TOKEN_REJECTED = ("Confluence did not accept the personal access token (HTTP 401). It may be wrong, expired or "
                   "revoked, or personal access tokens may be turned off by the Confluence administrator.")
_PASSWORD_REJECTED = ("Confluence did not accept the username and password (HTTP 401). Check them; if the site "
                      "signs in through SSO, use a personal access token instead.")
_CAPTCHA = ("Confluence requires a CAPTCHA for this account after failed logins, so API logins are refused "
            "(HTTP 401). Sign in to Confluence once in a browser to clear it, or use a personal access token.")
_ANONYMOUS = ("Confluence treated the request as anonymous, so the credentials were not used. Check the "
              "username and password or the personal access token.")
_REDIRECTED = ("Confluence redirected the request (often to a single sign-on or login page, or to another "
               "address). Check the URL and its context path; if the site signs in through SSO, use a "
               "personal access token.")
_NOT_API = ("The server answered, but not with the Confluence REST API. Check the URL, including the context "
            "path (for example https://intranet.example.com/confluence).")
_FORBIDDEN = "Confluence refused access to that content for this identity (HTTP 403)."
_NOT_FOUND = ("Not found, or this identity may not view it (Confluence hides restricted content). "
              "Use an id exactly as listed by search_files or list_files.")
_THROTTLED = ("Confluence is rate-limiting requests right now (HTTP 429). Do not retry in a loop; "
              "try again in a minute.")
_SERVER = "Confluence answered with an error (HTTP {status}). Try again later."
_UNREACHABLE = "Could not reach Confluence. Check the URL, the network path and the server's TLS certificate."
_TLS = ("Could not verify Confluence's TLS certificate. Add the issuing CA to the deployment trust store "
        "(REQUESTS_CA_BUNDLE).")
_TIMEOUT = "Confluence did not answer in time."
_TOO_LARGE = "Confluence's answer is larger than the 16 MB limit."
_BAD_QUERY_400 = "Confluence could not parse that search; try plain words."
_BAD_REQUEST = "Confluence refused the request (HTTP 400)."
_BAD_ID = ("That is not a Confluence id; use an id exactly as listed (page:<digits> or blogpost:<digits>), "
           "or a Confluence page URL.")
_SPACE_READ = "That id is a space; use list_files with the space key as folder_id to see its pages."
_OUTSIDE_SCOPE = "That content is outside the spaces this connection is limited to."
_UNSUPPORTED_TYPE = "That content is not a page or a blog post."
_BAD_LIMIT = "The limit must be a positive whole number."
_BAD_FOLDER = ("That folder is not valid; pass a space key (ENG), a path from an entry's path (ENG/Runbooks), "
               "or a page id (page:<digits>).")
_FOLDER_NOT_FOUND = "Folder not found; use a space key or the folder part of an entry's path exactly as listed."
_TOO_LARGE_READ = "Page exceeds the configured byte limit; increase it explicitly to read this page."
_BAD_QUERY_JSON = 'A Confluence query must be an id, or a JSON object with a file_id, e.g. {"file_id": "page:123"}.'


class ConfluenceError(ValueError):
    """A fixed, user-facing Confluence connector error."""


class ConfluenceHTTPError(ConfluenceError):
    """One failed Confluence exchange. ``kind`` is a stable machine word; the text is fixed."""

    def __init__(self, status: int, kind: str, message: str):
        super().__init__(message)
        self.status = status
        self.kind = kind


class _DeadlineTimeout(ConfluenceHTTPError):
    def __init__(self):
        super().__init__(0, "deadline", _TIMEOUT)


# Patched by tests so a 429 retry does not sleep.
_sleep = time.sleep


def _flag(value) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def _text(value) -> str | None:
    return value if isinstance(value, str) and value != "" else None


def _parse_spaces(value) -> tuple[str, ...]:
    """Space keys from the config (comma, space or newline separated); () means every space."""
    if value in (None, ""):
        return ()
    items = value if isinstance(value, (list, tuple)) else re.split(r"[,\s]+", str(value))
    keys, seen = [], set()
    for item in items:
        key = str(item).strip()
        if not key:
            continue
        if not _SPACE_KEY_RE.match(key):
            raise ConfluenceError(_BAD_SPACES)
        # Global space keys are upper case in Confluence; personal (~user) keys keep their case.
        key = key if key.startswith("~") else key.upper()
        if key.casefold() not in seen:
            seen.add(key.casefold())
            keys.append(key)
    return tuple(keys)


def _cql_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _clean_query(query) -> str:
    """Plain search words: CQL/Lucene operators are dropped so any text is a valid ``text ~`` phrase."""
    text = _CQL_NOISE_RE.sub(" ", str(query or ""))
    text = " ".join(w for w in text.split() if w.upper() not in ("AND", "OR", "NOT"))
    return text[:_MAX_QUERY_CHARS].strip()


def _segment(title) -> str:
    text = " ".join(str(title or "").split())[:255] or "(untitled)"
    return text.replace(_SLASH, _SLASH_IN_TITLE)


def _title_of(segment: str) -> str:
    return segment.replace(_SLASH_IN_TITLE, _SLASH)


def _normalise_date(value) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).isoformat()
    except ValueError:
        return None


def _status_message(status: int) -> tuple[str, str]:
    if status == 403:
        return "forbidden", _FORBIDDEN
    if status == 404:
        return "not_found", _NOT_FOUND
    if status == 400:
        return "bad_request", _BAD_REQUEST
    if status == 429:
        return "throttled", _THROTTLED
    return "server", _SERVER.format(status=status)


def planner_scope_note(config: dict) -> str:
    """The planner-facing note for a confluence_dc connection (pure, no I/O)."""
    cfg = config or {}
    try:
        spaces = _parse_spaces(cfg.get("spaces"))
    except ConfluenceError:
        spaces = ()
    scope = (f"limited to the spaces {', '.join(spaces)} (search, browse and reads outside them are refused)"
             if spaces else "every space this identity can see")
    return (
        f"Confluence Data Center: {scope}. There is no catalog: the file count shown is always 0, and every "
        "search, listing and read is live, as the connection's Confluence identity, so page restrictions apply. "
        "Start with search_files (Confluence full-text search over titles and page text). list_files with no "
        "folder_id lists pages space by space, each with a path SPACE/Parent/Page; pass a space key or the folder "
        "part of a path as folder_id (set recursive=true for the whole subtree). Read with read_file and the "
        "entry's id (page:<id> or blogpost:<id>); tables in pages come back as markdown tables. Each entry's "
        "web_url is the page link to cite."
    )


class ConfluenceDcClient(DataSourceClient):
    capabilities = {Capability.LIST_FILES, Capability.READ_FILE, Capability.SEARCH_FILES}
    # Always list live with the caller's identity: there is no catalog.
    cheap_live_listing = True
    is_document_based = True

    def __init__(self, base_url: str, spaces: str | None = None, include_blogposts: bool = True,
                 max_results: int = _DEFAULT_MAX_RESULTS, allow_http: bool = False, index_mode: str = "none",
                 username: str | None = None, password: str | None = None,
                 personal_access_token: str | None = None, auth_type: str | None = None, **kwargs):
        raw_url = base_url.strip() if isinstance(base_url, str) else ""
        parsed, bad_url = None, False
        try:
            parsed = urlsplit(raw_url)
            _ = parsed.port  # raises ValueError for a port outside 0-65535
        except ValueError:
            bad_url = True
        # Raised outside the except block so no parser error rides along as context.
        if (bad_url or parsed.scheme not in ("http", "https") or not parsed.hostname
                or "@" in parsed.netloc or any(ch.isspace() for ch in parsed.netloc)
                or parsed.query or parsed.fragment or "#" in raw_url or "?" in raw_url):
            raise ConfluenceError(_URL_MESSAGE)
        if parsed.scheme == "http" and not _flag(allow_http):
            raise ConfluenceError(_HTTPS_REQUIRED)
        # The path is the context path. A pasted ".../rest/api" is accepted and dropped.
        path = unquote(parsed.path).rstrip("/")
        if path.lower().endswith("/rest/api"):
            path = path[: -len("/rest/api")]
        segments = [s for s in path.split("/") if s]
        if any(seg in (".", "..") or not _CONTEXT_SEGMENT_RE.match(seg) for seg in segments):
            raise ConfluenceError(_URL_MESSAGE)
        self.host = parsed.netloc.lower()
        self.context_path = "".join("/" + s for s in segments)
        self._base = f"{parsed.scheme.lower()}://{self.host}{self.context_path}"
        self.spaces = _parse_spaces(spaces)
        self._space_set = {k.casefold() for k in self.spaces}
        self.include_blogposts = _flag(include_blogposts) if include_blogposts is not None else True
        try:
            self.max_results = max(1, min(1000, int(max_results or _DEFAULT_MAX_RESULTS)))
        except (TypeError, ValueError):
            self.max_results = _DEFAULT_MAX_RESULTS
        self.index_mode = "none"

        username, password = _text(username), _text(password)
        username = username.strip() if username else None
        token = _text(personal_access_token)
        token = token.strip() if token else None
        has_userpass, has_token = bool(username) and bool(password), bool(token)
        if auth_type == "token" and has_token:
            self.auth_method = "token"
        elif auth_type == "userpass" and has_userpass:
            self.auth_method = "userpass"
        elif has_token:
            self.auth_method = "token"
        elif has_userpass:
            self.auth_method = "userpass"
        else:
            raise ConfluenceError(_NO_CREDENTIAL)
        if self.auth_method == "token":
            if not CONFLUENCE_TOKEN_RE.fullmatch(token):
                raise ConfluenceError(CONFLUENCE_TOKEN_FORMAT_MESSAGE)
            self._auth_header = "Bearer " + token
        else:
            pair = f"{username}:{password}".encode("utf-8", "surrogatepass")
            self._auth_header = "Basic " + base64.b64encode(pair).decode("ascii")
        self._username = username if self.auth_method == "userpass" else None
        self._local = threading.local()

    # ------------------------------------------------------------------ meta
    @property
    def description(self):
        scope = (f"limited to the spaces {', '.join(self.spaces)}" if self.spaces
                 else "covering every space the identity can see")
        return (
            f"Read-only Confluence Data Center wiki at {self.host}{self.context_path}, {scope}. It is accessed as "
            "this connection's Confluence identity (the configured account, or each person's own token or login "
            "on per-user connections), so space permissions and page restrictions apply. Use search_files first "
            "(full-text search over page titles and text). list_files lists pages with a path SPACE/Parent/Page; "
            "pass a space key or a path as folder_id. Read with read_file and the entry's id (page:<id> or "
            "blogpost:<id>); tables come back as markdown tables. Cite the entry's web_url."
        )

    # ------------------------------------------------------------------ http
    def _session(self) -> requests.Session:
        # requests.Session is not thread-safe: one per worker thread.
        session = getattr(self._local, "session", None)
        if session is None:
            session = pinned_session()
            session.trust_env = False   # no ambient proxy or .netrc credentials
            session.verify = os.environ.get("REQUESTS_CA_BUNDLE") or True
            session.headers["Accept"] = "application/json"
            session.headers["Authorization"] = self._auth_header
            self._local.session = session
        return session

    def _send(self, op: str, path: str, params=None, deadline=None) -> tuple[int, bytes, dict]:
        """One GET. Returns (status, body, headers); transport failures raise a fixed error."""
        url = self._base + "/rest/api" + path
        started = time.monotonic()
        timeout = _DEFAULT_TIMEOUT
        if deadline is not None:
            left = deadline - started
            if left <= 0:
                raise _DeadlineTimeout()
            left = max(_MIN_TIMEOUT_S, left)
            timeout = (min(_DEFAULT_TIMEOUT[0], left), min(_DEFAULT_TIMEOUT[1], left))
        status, chunks, headers, failure = 0, [], {}, None
        try:
            with self._session().get(url, params=params, timeout=timeout, allow_redirects=False,
                                     stream=True) as response:
                status = response.status_code
                headers = {k.lower(): v for k, v in response.headers.items()}
                if status == 200:
                    size = 0
                    for chunk in response.iter_content(_CHUNK_BYTES):
                        size += len(chunk)
                        if size > _MAX_RESPONSE_BYTES:
                            failure = "too_large"
                            break
                        chunks.append(chunk)
                        if deadline is not None and time.monotonic() >= deadline:
                            failure = "deadline"
                            break
        except requests.exceptions.SSLError:
            failure = "tls"
        except (requests.exceptions.Timeout, TimeoutError):
            failure = "timeout"
        except _TRANSPORT_ERRORS as exc:
            failure = "unreachable"
            logger.debug("confluence_dc %s transport failure: %s", op, type(exc).__name__)
        # Raised outside the except blocks, so no exception chain carries the URL.
        if failure == "deadline" or (failure == "timeout" and deadline is not None
                                     and deadline - time.monotonic() < _MIN_TIMEOUT_S):
            raise _DeadlineTimeout()
        if failure:
            message = {"tls": _TLS, "timeout": _TIMEOUT, "too_large": _TOO_LARGE}.get(failure, _UNREACHABLE)
            raise ConfluenceHTTPError(status, failure, message)
        logger.debug("confluence_dc %s -> %s in %d ms", op, status, int((time.monotonic() - started) * 1000))
        return status, b"".join(chunks), headers

    def _get(self, op: str, path: str, params=None, deadline=None):
        """GET ``{base}/rest/api{path}`` and return its JSON; maps every failure to a fixed error."""
        for attempt in range(_RATE_LIMIT_RETRIES + 1):
            status, body, headers = self._send(op, path, params, deadline)
            if status != 429 or attempt == _RATE_LIMIT_RETRIES:
                break
            wait = self._retry_after(headers)
            if deadline is not None and time.monotonic() + wait >= deadline:
                break
            logger.info("confluence_dc %s rate-limited; retrying in %.1fs", op, wait)
            _sleep(wait)
        if status == 401:
            if "captcha" in (headers.get("x-authentication-denied-reason") or "").lower():
                raise ConfluenceHTTPError(401, "captcha", _CAPTCHA)
            raise ConfluenceHTTPError(401, "auth", _TOKEN_REJECTED if self.auth_method == "token" else _PASSWORD_REJECTED)
        if 300 <= status < 400:
            raise ConfluenceHTTPError(status, "redirect", _REDIRECTED)
        if status != 200:
            kind, message = _status_message(status)
            raise ConfluenceHTTPError(status, kind, message)
        if "json" not in (headers.get("content-type") or "application/json").lower():
            raise ConfluenceHTTPError(200, "not_api", _NOT_API)
        try:
            return json.loads(body)
        except (ValueError, RecursionError):
            pass
        raise ConfluenceHTTPError(200, "not_api", _NOT_API)

    @staticmethod
    def _retry_after(headers: dict) -> float:
        raw = headers.get("retry-after")
        try:
            return max(0.0, min(_MAX_RETRY_AFTER_S, float(raw)))
        except (TypeError, ValueError):
            return 2.0

    def _pages(self, op: str, path: str, params: dict, cap: int, budget: list, deadline=None, cancel_check=None):
        """Yield result records across start/limit pages until ``cap`` records, a short page or the call budget.

        ``budget`` is a one-item list of calls left (shared across one tool call).
        Stops quietly at the budget; the caller decides whether that is a stop.
        """
        start, served = 0, 0
        while served < cap:
            if cancel_check is not None and cancel_check():
                raise SearchStopped("cancelled", [])
            if budget[0] <= 0:
                self._budget_hit = True
                return
            budget[0] -= 1
            limit = min(_PAGE_ROWS, cap - served)
            payload = self._get(op, path, {**params, "start": str(start), "limit": str(limit)}, deadline)
            results = payload.get("results") if isinstance(payload, dict) else None
            if not isinstance(results, list):
                raise ConfluenceHTTPError(200, "not_api", _NOT_API)
            for record in results:
                if isinstance(record, dict):
                    served += 1
                    yield record
            size = len(results)
            has_next = isinstance(payload.get("_links"), dict) and bool(payload["_links"].get("next"))
            if size == 0 or not has_next:
                return
            start += size

    # ------------------------------------------------------------------ scope
    def _in_scope(self, space_key) -> bool:
        return not self._space_set or (isinstance(space_key, str) and space_key.casefold() in self._space_set)

    def _type_clause(self) -> str:
        return "type in (page, blogpost)" if self.include_blogposts else "type = page"

    def _space_clause(self) -> str:
        if not self.spaces:
            return ""
        return " AND space in (" + ", ".join(_cql_string(k) for k in self.spaces) + ")"

    def _web_url(self, links) -> str | None:
        webui = links.get("webui") if isinstance(links, dict) else None
        if isinstance(webui, str) and webui.startswith("/") and not webui.startswith("//"):
            return self._base + webui
        return None

    def _entry(self, record: dict, path_segments: list[str], snippet: str | None = None) -> dict | None:
        content_id, ctype = str(record.get("id") or ""), record.get("type")
        if not _DIGITS_RE.fullmatch(content_id) or ctype not in ("page", "blogpost"):
            return None
        version = record.get("version") if isinstance(record.get("version"), dict) else {}
        title = _segment(record.get("title"))
        return {
            "id": f"{ctype}:{content_id}", "name": title, "path": _SLASH.join([*path_segments, title]),
            "size": 0, "modified_at": _normalise_date(version.get("when") or record.get("lastModified")),
            "mime_type": "text/plain", "is_folder": False, "kind": ctype, "status": None,
            "snippet": snippet, "web_url": self._web_url(record.get("_links")),
        }

    # ------------------------------------------------------------------ test
    def test_connection(self):
        try:
            me = self._get("whoami", "/user/current")
            if not isinstance(me, dict) or me.get("type") == "anonymous" or not (me.get("username") or me.get("userKey")):
                return {"success": False, "message": _ANONYMOUS, "login_refused": "rejected"}
            username = str(me.get("username") or "") or None
            display = " ".join(str(me.get("displayName") or "").split())[:120] or None
            identity = username or display
            named = f"{display} ({username})" if display and username and display != username else (identity or "?")
            missing = []
            if self.spaces:
                for key in self.spaces[:20]:
                    try:
                        self._get("space", "/space/" + quote(key, safe="~"))
                    except ConfluenceHTTPError as exc:
                        if exc.kind not in ("not_found", "forbidden"):
                            raise
                        missing.append(key)
                if missing:
                    return {"success": False, "reachable": True,
                            "message": (f"Connected to Confluence as {named}, but these spaces are missing or not "
                                        f"visible to this identity: {', '.join(missing)}. Check the Spaces setting.")}
            else:
                listed = self._get("spaces", "/space", {"limit": "1"})
                if not (isinstance(listed, dict) and listed.get("results")):
                    return {"success": False, "reachable": True,
                            "message": f"Connected to Confluence as {named}, but this identity cannot see any space."}
            where = f"{self.host}{self.context_path}"
            return {
                "success": True,
                "message": f"Connected to Confluence at {where} as {named}.",
                "details": {"host": self.host, "context_path": self.context_path, "identity": identity,
                            "display_name": display, "auth_method": self.auth_method, "spaces": list(self.spaces)},
            }
        except ConfluenceHTTPError as exc:
            logger.info("confluence_dc test_connection failed: %s", exc.kind)
            result = {"success": False, "message": str(exc)}
            if exc.kind in ("auth", "captcha"):
                result["login_refused"] = exc.kind if exc.kind == "captcha" else "rejected"
            elif exc.kind == "not_found":
                # /user/current is on every Confluence: a 404 there means this is not its REST API.
                result["message"] = _NOT_API
            return result
        except ConfluenceError as exc:
            return {"success": False, "message": str(exc)}
        except Exception as exc:
            # The class name only: the exception text may hold a URL or a secret.
            logger.warning("confluence_dc test_connection failed unexpectedly: %s", type(exc).__name__)
            return {"success": False, "message": "Confluence connection test failed."}

    # ------------------------------------------------------------------ search
    def search_files(self, query, limit=100, deadline=None, cancel_check=None, **_):
        """Confluence full-text search (CQL ``text ~``) as this identity, limited to the configured spaces.

        Returns at most ``min(limit, max_results)`` entries; when more exist it
        raises ``SearchStopped("max_results", entries)``. ``deadline`` and
        ``cancel_check`` stop between pages with the entries found so far.
        """
        cap = self._cap(limit, 100)
        words = _clean_query(query)
        if not words or cap <= 0:
            return []
        cql = f"{self._type_clause()}{self._space_clause()} AND text ~ {_cql_string(words)}"
        files: list[dict] = []
        seen: set[str] = set()
        budget = [_MAX_SEARCH_CALLS]
        self._budget_hit = False
        try:
            for record in self._pages("search", "/search", {"cql": cql, "excerpt": "highlight"}, cap + 1,
                                      budget, deadline, cancel_check):
                content = record.get("content") if isinstance(record.get("content"), dict) else {}
                container = record.get("resultGlobalContainer") if isinstance(record.get("resultGlobalContainer"), dict) else {}
                display_url = str(container.get("displayUrl") or "")
                space_key = unquote(display_url.rsplit("/display/", 1)[1]).split("/")[0] if "/display/" in display_url else None
                if space_key is not None and not self._in_scope(space_key):
                    continue     # belt and braces: CQL already limits the spaces
                merged = {**content, "lastModified": record.get("lastModified")}
                if "_links" not in merged or not merged.get("_links"):
                    merged["_links"] = {"webui": record.get("url")}
                snippet = _HL_RE.sub("", " ".join(str(record.get("excerpt") or "").split()))[:_SNIPPET_CHARS]
                entry = self._entry(merged, [space_key] if space_key else [], snippet or None)
                if entry is None or entry["id"] in seen:
                    continue
                if len(files) >= cap:
                    raise SearchStopped("max_results", files)
                seen.add(entry["id"])
                files.append(entry)
        except SearchStopped as stop:
            if stop.stop_reason == "cancelled" and not stop.files:
                raise SearchStopped("cancelled", files)
            raise
        except _DeadlineTimeout:
            raise SearchStopped("deadline", files)
        except ConfluenceHTTPError as exc:
            if exc.kind == "bad_request":
                raise ConfluenceHTTPError(400, "bad_request", _BAD_QUERY_400)
            raise
        if self._budget_hit:
            raise SearchStopped("max_results", files)
        return files

    def _cap(self, limit, default: int) -> int:
        try:
            cap = int(default if limit is None else limit)
        except (TypeError, ValueError, OverflowError):
            cap = None
        if cap is None or cap < 0:
            raise ConfluenceError(_BAD_LIMIT)
        return min(cap, self.max_results)

    # ------------------------------------------------------------------ browse
    def list_files(self, folder_id=None, recursive=None, limit=None, progress_callback=None, **_):
        """Browse pages as this identity, space by space; each entry's path is SPACE/Ancestor/.../Title.

        ``folder_id``: None (or "/") lists every in-scope space; a space key
        lists that space; ``SPACE/Title/...`` or ``page:<id>`` lists that
        page's children. ``recursive`` defaults to True without a folder and
        False with one (a space then lists its root pages; a page its direct
        children). At most ``min(limit, max_results)`` entries and
        ``_MAX_LIST_CALLS`` API calls; past either the listing just stops.
        """
        cap = self._cap(limit, self.max_results) if limit is not None else self.max_results
        if cap == 0:
            raise ConfluenceError(_BAD_LIMIT)
        folder = str(folder_id or "").strip().strip("/")
        deep = (not folder) if recursive is None else _flag(recursive)
        budget = [_MAX_LIST_CALLS]
        self._budget_hit = False
        files: list[dict] = []
        expand = {"expand": "ancestors,version"}

        seen: set[str] = set()
        prefix_key = ""

        def add(records, prefix: list[str] | None = None):
            """Append entries; a record's path is ``prefix`` or SPACE plus its expanded ancestors."""
            for record in records:
                if len(files) >= cap:
                    return
                if prefix is not None:
                    segments = prefix
                else:
                    ancestors = record.get("ancestors") if isinstance(record.get("ancestors"), list) else []
                    segments = [prefix_key, *[_segment(a.get("title")) for a in ancestors if isinstance(a, dict)]]
                entry = self._entry(record, [s for s in segments if s])
                if entry is not None and entry["id"] not in seen:
                    seen.add(entry["id"])
                    files.append(entry)

        if not folder:
            for key in self._space_keys(budget):
                if len(files) >= cap or budget[0] <= 0:
                    break
                prefix_key = key
                add(self._pages("list", f"/space/{quote(key, safe='~')}/content/page",
                                {"depth": "all" if deep else "root", **expand}, cap - len(files), budget))
                if self.include_blogposts and deep and len(files) < cap:
                    add(self._pages("list", f"/space/{quote(key, safe='~')}/content/blogpost", expand,
                                    cap - len(files), budget), [key, "Blog"])
            return files

        page_id, space_key, path_prefix = self._resolve_folder(folder, budget)
        prefix_key = space_key
        if page_id is None:
            add(self._pages("list", f"/space/{quote(space_key, safe='~')}/content/page",
                            {"depth": "all" if deep else "root", **expand}, cap, budget))
            if self.include_blogposts and deep and len(files) < cap:
                add(self._pages("list", f"/space/{quote(space_key, safe='~')}/content/blogpost", expand,
                                cap - len(files), budget), [space_key, "Blog"])
            return files
        if deep:
            cql = f"ancestor = {page_id} AND type = page"
            add(self._pages("list", "/content/search", {"cql": cql, **expand}, cap, budget))
        else:
            add(self._pages("list", f"/content/{page_id}/child/page", expand, cap, budget), path_prefix)
        return files

    def _space_keys(self, budget: list) -> list[str]:
        if self.spaces:
            return list(self.spaces)
        keys = []
        for record in self._pages("spaces", "/space", {}, _MAX_SPACES, budget):
            key = record.get("key")
            if isinstance(key, str) and _SPACE_KEY_RE.match(key):
                keys.append(key)
        return keys

    def _resolve_folder(self, folder: str, budget: list) -> tuple[str | None, str, list[str]]:
        """(page id or None, space key, path segments of the page) for a folder_id; raises when unknown."""
        match = _ID_RE.match(folder)
        if match and match.group(1).lower() == "space":
            folder = match.group(2)
        elif match:
            raw = match.group(2)
            if not _DIGITS_RE.fullmatch(raw):
                raise ConfluenceError(_BAD_FOLDER)
            budget[0] -= 1
            record = self._get("content", f"/content/{raw}", {"expand": "space,ancestors"})
            key = (record.get("space") or {}).get("key") if isinstance(record, dict) else None
            if not self._in_scope(key):
                raise GlobScopeError(_OUTSIDE_SCOPE)
            ancestors = [_segment(a.get("title")) for a in (record.get("ancestors") or []) if isinstance(a, dict)]
            return raw, key, [key, *ancestors, _segment(record.get("title"))]
        segments = [s.strip() for s in folder.split(_SLASH) if s.strip()]
        if not segments or not _SPACE_KEY_RE.match(segments[0]):
            raise ConfluenceError(_BAD_FOLDER)
        key = segments[0] if segments[0].startswith("~") else segments[0].upper()
        if not self._in_scope(key):
            raise GlobScopeError(_OUTSIDE_SCOPE)
        if len(segments) == 1:
            budget[0] -= 1
            try:
                self._get("space", "/space/" + quote(key, safe="~"))
            except ConfluenceHTTPError as exc:
                if exc.kind in ("not_found", "forbidden"):
                    raise ConfluenceHTTPError(404, "not_found", _FOLDER_NOT_FOUND)
                raise
            return None, key, [key]
        # Page titles are unique within a space, so the last segment names the page.
        budget[0] -= 1
        found = self._get("content", "/content", {"spaceKey": key, "title": _title_of(segments[-1]),
                                                  "type": "page", "expand": "ancestors", "limit": "1"})
        results = found.get("results") if isinstance(found, dict) else None
        record = results[0] if isinstance(results, list) and results and isinstance(results[0], dict) else None
        if record is None or not _DIGITS_RE.fullmatch(str(record.get("id") or "")):
            raise ConfluenceHTTPError(404, "not_found", _FOLDER_NOT_FOUND)
        ancestors = [_segment(a.get("title")) for a in (record.get("ancestors") or []) if isinstance(a, dict)]
        # SPACE/Title is enough (titles are unique per space); a longer path must match the ancestors.
        middle = segments[1:-1]
        if middle and [s.casefold() for s in ancestors] != [s.casefold() for s in middle]:
            raise ConfluenceHTTPError(404, "not_found", _FOLDER_NOT_FOUND)
        return str(record["id"]), key, [key, *ancestors, _segment(record.get("title"))]

    # ------------------------------------------------------------------ schema (none)
    @discovery_progress
    def get_schemas(self, progress_callback=None):
        # No catalog: search and browse are always live.
        return []

    def get_schema(self, table_name):
        return None

    def prompt_schema(self):
        return ""

    # ------------------------------------------------------------------ read
    def _parse_read_id(self, file_id) -> tuple[str, str]:
        text = str(file_id or "").strip()
        if _DIGITS_RE.fullmatch(text):
            return "content", text
        match = _ID_RE.match(text)
        if match:
            kind, raw = match.group(1).lower(), match.group(2).strip()
            if kind == "space":
                raise ConfluenceError(_SPACE_READ)
            if _DIGITS_RE.fullmatch(raw):
                return kind, raw
            raise ConfluenceError(_BAD_ID)
        # A pasted page link on this site: .../pages/viewpage.action?pageId=123
        try:
            parsed = urlsplit(text)
        except ValueError:
            parsed = None
        if parsed is not None and parsed.scheme in ("http", "https") and parsed.netloc.lower() == self.host:
            page_ids = parse_qs(parsed.query).get("pageId") or []
            if page_ids and _DIGITS_RE.fullmatch(page_ids[0]):
                return "content", page_ids[0]
        raise ConfluenceError(_BAD_ID)

    def read_file(self, file_id, sheet=None, max_bytes=None, page_range=None, **_):
        """A page or blog post as markdown-like text with a short header (title, space, path, version, link).

        Ids: ``page:<digits>``, ``blogpost:<digits>``, bare digits, or a page
        URL on this site. A page outside the configured spaces is refused
        (``GlobScopeError``); a restricted one is ``not_found`` (Confluence
        does not reveal it). Returns ``DocumentText`` named ``<title>.txt``.
        """
        kind, content_id = self._parse_read_id(file_id)
        record = self._get("read", f"/content/{content_id}",
                           {"expand": "body.export_view,version,space,ancestors"})
        if not isinstance(record, dict) or str(record.get("id")) != content_id:
            raise ConfluenceHTTPError(200, "not_api", _NOT_API)
        ctype = record.get("type")
        if ctype not in ("page", "blogpost") or (kind in ("page", "blogpost") and kind != ctype):
            raise ConfluenceError(_UNSUPPORTED_TYPE)
        space = record.get("space") if isinstance(record.get("space"), dict) else {}
        key = space.get("key")
        if not self._in_scope(key):
            raise GlobScopeError(_OUTSIDE_SCOPE)
        title = " ".join(str(record.get("title") or "").split()) or "(untitled)"
        ancestors = [" ".join(str(a.get("title") or "").split()) for a in (record.get("ancestors") or [])
                     if isinstance(a, dict)]
        version = record.get("version") if isinstance(record.get("version"), dict) else {}
        by = version.get("by") if isinstance(version.get("by"), dict) else {}
        body = (record.get("body") or {}).get("export_view") if isinstance(record.get("body"), dict) else None
        text = html_to_text(body.get("value") if isinstance(body, dict) else None)
        header = [f"# {title}", ""]
        space_name = " ".join(str(space.get("name") or "").split())
        header.append(f"Space: {space_name} ({key})" if space_name else f"Space: {key}")
        if ancestors:
            header.append("Path: " + " / ".join([str(key), *ancestors, title]))
        updated = _normalise_date(version.get("when"))
        if updated or version.get("number"):
            who = " ".join(str(by.get("displayName") or "").split())
            parts = [p for p in (updated, f"by {who}" if who else "",
                                 f"(version {version['number']})" if version.get("number") else "") if p]
            header.append("Last updated: " + " ".join(parts))
        url = self._web_url(record.get("_links"))
        if url:
            header.append(f"URL: {url}")
        header += ["", "---", ""]
        document = "\n".join(header) + (text or "(This page has no text content.)")
        safe = re.sub(r"[\\/:*?\"<>|\x00-\x1f]+", " ", title).strip()[:120] or "page"
        return DocumentText(document, name=f"{safe}.txt", mime="text/plain")

    def read_raw_bytes(self, file_id, *, max_bytes=None):
        """``(utf-8 text, name, "text/plain")`` of ``read_file``; ``FileTooLargeError`` above ``max_bytes``."""
        text = self.read_file(file_id)
        data = text.encode("utf-8")
        if max_bytes and len(data) > max_bytes:
            raise FileTooLargeError(_TOO_LARGE_READ)
        return data, text.name, "text/plain"

    def execute_query(self, query=None, table_name=None, **kwargs):
        """Read one page: a JSON ``{"file_id": ...}`` or the id itself."""
        text = "" if query is None else str(query)
        if not text.lstrip().startswith("{"):
            return self.read_file(table_name or text or kwargs.get("file_id", ""))
        try:
            params = json.loads(text)
        except (ValueError, RecursionError):
            params = None
        if not isinstance(params, dict) or "file_id" not in params:
            raise ConfluenceError(_BAD_QUERY_JSON)
        return self.read_file(params["file_id"])
