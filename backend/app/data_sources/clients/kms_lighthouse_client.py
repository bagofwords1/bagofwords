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

import json
import logging
import os
import re
import threading
import time
from urllib.parse import unquote, urlsplit

import requests

from app.data_sources.clients.base import Capability, DataSourceClient
from app.data_sources.clients._file_source_common import GlobScopeError
from app.data_sources.clients.progress import discovery_progress

logger = logging.getLogger(__name__)

_PREFIX_RE = re.compile(r"^(/[A-Za-z0-9._~-]+)+$")
_DIGITS_RE = re.compile(r"^[0-9]{1,12}$")
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
_MESSAGES = {
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
_TOP_LEVEL_HINT = (
    "Connected to KMS Lighthouse, but the top level of the folder tree came back empty. Set Root folder ID "
    "to a folder this account can open, or check the account's KMS permissions."
)
_MAX_RESPONSE_BYTES = 16 * 1024 * 1024


class KmsError(ValueError):
    """A KMS Lighthouse failure with a fixed, secret-free message."""


class KmsHTTPError(KmsError):
    def __init__(self, status: int, kind: str, message: str | None = None):
        self.status = status
        self.kind = kind
        super().__init__(message or _MESSAGES.get(kind) or _MESSAGES["server"])


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


def _clean_text(value, limit: int = 200) -> str:
    text = "".join(ch for ch in str(value) if ch.isprintable())
    return " ".join(text.split())[:limit]


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
        parsed = urlsplit((base_url or "").strip())
        if (parsed.scheme not in ("http", "https") or not parsed.hostname
                or parsed.username or parsed.password or parsed.query or parsed.fragment
                or "#" in (base_url or "") or "?" in (base_url or "")):
            raise KmsError("KMS Lighthouse URL must be an HTTP(S) URL such as https://example.lighthouse-cloud.com, "
                           "without credentials, query, or fragment.")
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
        self.include_offline = bool(include_offline)
        self.max_results = int(max_results or _DEFAULT_MAX_RESULTS)
        self.index_mode = "none"

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
        self._username = username if self.auth_method == "userpass" else None
        self._password = password if self.auth_method == "userpass" else None
        self._api_token = api_token if self.auth_method == "token" else None

        # Login state, shared by this instance's threads. Each thread-local
        # session re-applies it whenever the generation moves ahead.
        self._auth_lock = threading.Lock()
        self._generation = 0
        self._cookies: dict = {}
        self._session_token: str | None = None
        self._local = threading.local()

    # ------------------------------------------------------------------ meta
    @property
    def description(self):
        who = ("the configured account" if self.auth_method == "userpass"
               else "the signed-in user's own KMS login or token")
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
        if self.auth_method == "userpass" and self._local.generation != self._generation:
            with self._auth_lock:
                cookies, token, generation = dict(self._cookies), self._session_token, self._generation
            session.cookies.clear()
            session.cookies.update(cookies)
            if token:
                session.headers["Authorization"] = "Bearer " + token
            else:
                session.headers.pop("Authorization", None)
            self._local.generation = generation
        return session

    def _ensure_login(self) -> None:
        if self.auth_method != "userpass":
            return
        with self._auth_lock:
            if self._generation:
                return
        self._login(self._new_session())

    def _login(self, session: requests.Session) -> None:
        """The only place that authenticates (a hash login would plug in here)."""
        status, body = self._send(session, "login", "POST", "/login",
                                  params={"username": self._username, "password": self._password})
        if status in (400, 401, 403):
            raise KmsHTTPError(401, "auth", _USERPASS_AUTH)
        if status != 200:
            raise KmsHTTPError(status, _kind_for_status(status))
        payload = self._decode(body)
        if not isinstance(payload, dict) or str(payload.get("status", "")).upper() != "SUCCESS":
            raise KmsHTTPError(401, "auth", _USERPASS_AUTH)
        data = payload.get("data")
        token = None
        if isinstance(data, dict) and isinstance(data.get("token"), str):
            token = data["token"]
        elif isinstance(data, str):
            token = data
        elif isinstance(payload.get("token"), str):
            token = payload["token"]
        with self._auth_lock:
            self._cookies = dict(session.cookies)
            self._session_token = token or None
            self._generation += 1

    # ------------------------------------------------------------------ http
    def _send(self, session, op, method, path, *, params=None, headers=None):
        """One HTTP exchange. Returns (status, body bytes); never raises with a URL."""
        url = self._base + self.api_prefix + path
        started = time.monotonic()
        failed = None
        try:
            with session.request(method, url, params=params, headers=headers, timeout=(10, 60),
                                 allow_redirects=False, stream=True) as response:
                status = response.status_code
                body = response.raw.read(_MAX_RESPONSE_BYTES + 1, decode_content=True) if status == 200 else b""
        except requests.RequestException:
            failed = "unreachable"
        if failed:
            raise KmsHTTPError(0, failed)
        logger.debug("kms_lighthouse %s -> %s in %d ms", op, status, int((time.monotonic() - started) * 1000))
        return status, body

    @staticmethod
    def _decode(body: bytes):
        failed = False
        try:
            payload = json.loads(body)
        except ValueError:
            failed = True
        if failed:
            raise KmsHTTPError(0, "server")
        return payload

    def _request(self, op, path, *, params=None, headers=None, missing="rejected"):
        """GET ``{base}{prefix}{path}`` as this client's identity and return the JSON body."""
        self._ensure_login()
        status, body = self._send(self._session(), op, "GET", path, params=params, headers=headers)
        if status == 401:
            raise KmsHTTPError(401, "auth", _USERPASS_AUTH if self.auth_method == "userpass" else _TOKEN_AUTH)
        if status != 200:
            raise KmsHTTPError(status, _kind_for_status(status))
        payload = self._decode(body)
        if isinstance(payload, dict) and str(payload.get("status", "")).upper() == "ERROR":
            raise KmsHTTPError(200, missing)
        return payload

    def _tree_children(self, parent_id: int | None) -> list:
        params = {"parentId": str(parent_id)} if parent_id is not None else None
        payload = self._request("tree", "/cm/tree/branch/view", params=params)
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list):
            raise KmsHTTPError(200, "server")
        for record in data:
            if not isinstance(record, dict) or not _DIGITS_RE.match(str(record.get("id", ""))):
                raise KmsHTTPError(200, "server")
        return data

    # ------------------------------------------------------------------ api
    def test_connection(self):
        try:
            if self.auth_method == "userpass":
                # A health probe always logs in fresh (never a warm session).
                self._login(self._new_session())
            identity = None
            try:
                info = self._request("session_info", "/sessionInfo")
                data = info.get("data") if isinstance(info, dict) else None
                if isinstance(data, dict):
                    identity = _clean_text(data.get("username") or data.get("userFullName") or "") or None
            except KmsHTTPError as exc:
                if exc.kind == "auth":
                    raise
            try:
                children = self._tree_children(self.root_folder_id)
            except KmsHTTPError as exc:
                if self.root_folder_id is None and exc.kind in ("bad_request", "no_data"):
                    return {"success": False, "message": _TOP_LEVEL_HINT}
                raise
            if self.root_folder_id is None and not children:
                return {"success": False, "message": _TOP_LEVEL_HINT}
            identity = identity or (self._username if self.auth_method == "userpass" else "the API token's account")
            return {
                "success": True,
                "message": f"Connected to KMS Lighthouse at {self.host} as {identity}.",
                "details": {"host": self.host, "identity": identity, "auth_method": self.auth_method,
                            "root_folder_id": self.root_folder_id},
            }
        except (KmsError, GlobScopeError) as exc:
            return {"success": False, "message": str(exc)}
        except Exception:
            return {"success": False, "message": "KMS connection test failed."}

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
