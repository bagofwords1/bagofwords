"""Shared HTTP layer for the Atlassian Data Center / Server connectors (Confluence, Jira).

Both products run on-prem behind a context path (``https://host/confluence``,
``https://host/jira``), take the same credentials (a personal access token sent
as ``Authorization: Bearer``, or HTTP Basic) and fail in the same on-prem ways
(CAPTCHA after failed logins, an SSO redirect, a proxy login page, 429 from the
admin's rate limiter). This module holds that behaviour once:

  * ``parse_base_url``  - scheme/host/context-path validation (no userinfo,
    query or fragment; HTTPS unless the lab switch is on).
  * ``resolve_auth``    - picks the token or the login and builds the header.
  * ``AtlassianDcHttp`` - a mixin with one thread-local egress-pinned session
    per client, ``_send`` (one GET, 16 MB cap, deadline-aware timeouts, never
    follows redirects) and ``_get`` (429 retry honouring ``Retry-After``, then
    status/kind mapping to fixed messages).

Messages leaving this module are fixed strings: they never carry a request URL,
a provider response body or a credential.
"""
from __future__ import annotations

import base64
import http.client
import json
import logging
import os
import re
import threading
import time
from urllib.parse import unquote, urlsplit

import requests
import urllib3

from app.core.egress import pinned_session

logger = logging.getLogger(__name__)

MAX_RESPONSE_BYTES = 16 * 1024 * 1024
DEFAULT_TIMEOUT = (10, 60)          # (connect, read) seconds
MIN_TIMEOUT_S = 1
CHUNK_BYTES = 64 * 1024
RATE_LIMIT_RETRIES = 2
MAX_RETRY_AFTER_S = 10.0
TRANSPORT_ERRORS = (requests.RequestException, urllib3.exceptions.HTTPError, http.client.HTTPException,
                    OSError, ValueError)
CONTEXT_SEGMENT_RE = re.compile(r"^[A-Za-z0-9._~-]+$")
# Visible ASCII only: a token goes into an HTTP header as-is.
TOKEN_RE = re.compile(r"\A[\x21-\x7e]+\Z")

HTTPS_REQUIRED = "HTTPS is required. Enable Allow HTTP only for an isolated test lab."


def _sleep(seconds: float) -> None:
    """Patched by tests so a 429 retry does not sleep."""
    time.sleep(seconds)


class AtlassianError(ValueError):
    """A fixed, user-facing Atlassian Data Center connector error."""


class AtlassianHTTPError(AtlassianError):
    """One failed exchange. ``kind`` is a stable machine word; the text is fixed."""

    def __init__(self, status: int, kind: str, message: str):
        super().__init__(message)
        self.status = status
        self.kind = kind


def flag(value) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def text_or_none(value) -> str | None:
    return value if isinstance(value, str) and value != "" else None


def parse_base_url(base_url, allow_http, *, error_class, url_message: str,
                   api_suffixes: tuple[str, ...] = ()) -> tuple[str, str, str]:
    """``(host, context_path, base)`` for a site URL; raises ``error_class`` with a fixed message.

    A pasted API suffix (e.g. ``/rest/api`` or ``/rest/api/2``) is accepted and dropped,
    so ``base`` is always ``scheme://host[:port]/<context path>`` with no trailing slash.
    """
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
        raise error_class(url_message)
    if parsed.scheme == "http" and not flag(allow_http):
        raise error_class(HTTPS_REQUIRED)
    path = unquote(parsed.path).rstrip("/")
    for suffix in sorted(api_suffixes, key=len, reverse=True):
        if path.lower().endswith(suffix):
            path = path[: -len(suffix)]
            break
    segments = [s for s in path.split("/") if s]
    if any(seg in (".", "..") or not CONTEXT_SEGMENT_RE.match(seg) for seg in segments):
        raise error_class(url_message)
    host = parsed.netloc.lower()
    context_path = "".join("/" + s for s in segments)
    return host, context_path, f"{parsed.scheme.lower()}://{host}{context_path}"


def resolve_auth(username, password, token, auth_type, *, error_class, no_credential: str,
                 token_format_message: str) -> tuple[str, str, str | None]:
    """``(auth_method, Authorization header, username or None)``.

    An explicit ``auth_type`` wins when its fields are present; otherwise a token
    wins over a login (the stored credentials blob may carry both after a switch).
    """
    username, password = text_or_none(username), text_or_none(password)
    username = username.strip() if username else None
    token = text_or_none(token)
    token = token.strip() if token else None
    has_userpass, has_token = bool(username) and bool(password), bool(token)
    if auth_type == "token" and has_token:
        method = "token"
    elif auth_type == "userpass" and has_userpass:
        method = "userpass"
    elif has_token:
        method = "token"
    elif has_userpass:
        method = "userpass"
    else:
        raise error_class(no_credential)
    if method == "token":
        if not TOKEN_RE.fullmatch(token):
            raise error_class(token_format_message)
        return method, "Bearer " + token, None
    pair = f"{username}:{password}".encode("utf-8", "surrogatepass")
    return method, "Basic " + base64.b64encode(pair).decode("ascii"), username


class AtlassianDcHttp:
    """Mixin: thread-local session, one GET with a size cap, retries and fixed error mapping.

    The host class sets ``product`` (e.g. "Confluence"), ``api_root`` (prefixed to
    every path, may be ""), ``log_name``, ``http_error_class`` (a subclass of
    ``AtlassianHTTPError``), ``_base``, ``_auth_header`` and ``auth_method``.
    """

    product = "Atlassian"
    api_root = ""
    log_name = "atlassian_dc"
    http_error_class = AtlassianHTTPError
    not_found_message = "Not found, or this identity may not view it."

    # ------------------------------------------------------------- messages
    def _msg(self, key: str, **kw) -> str:
        p = self.product
        texts = {
            "token_rejected": (f"{p} did not accept the personal access token (HTTP 401). It may be wrong, expired "
                               f"or revoked, or personal access tokens may be turned off by the {p} administrator."),
            "password_rejected": (f"{p} did not accept the username and password (HTTP 401). Check them; if the "
                                  "site signs in through SSO, use a personal access token instead."),
            "captcha": (f"{p} requires a CAPTCHA for this account after failed logins, so API logins are refused "
                        f"(HTTP 401). Sign in to {p} once in a browser to clear it, or use a personal access token."),
            "redirected": (f"{p} redirected the request (often to a single sign-on or login page, or to another "
                           "address). Check the URL and its context path; if the site signs in through SSO, use a "
                           "personal access token."),
            "not_api": (f"The server answered, but not with the {p} REST API. Check the URL, including the context "
                        f"path (for example https://intranet.example.com/{p.lower()})."),
            "forbidden": f"{p} refused access to that content for this identity (HTTP 403).",
            "throttled": (f"{p} is rate-limiting requests right now (HTTP 429). Do not retry in a loop; "
                          "try again in a minute."),
            "server": f"{p} answered with an error (HTTP {kw.get('status')}). Try again later.",
            "unreachable": f"Could not reach {p}. Check the URL, the network path and the server's TLS certificate.",
            "tls": (f"Could not verify {p}'s TLS certificate. Add the issuing CA to the deployment trust store "
                    "(REQUESTS_CA_BUNDLE)."),
            "timeout": f"{p} did not answer in time.",
            "too_large": f"{p}'s answer is larger than the 16 MB limit.",
            "bad_request": f"{p} refused the request (HTTP 400).",
        }
        return texts[key]

    def _error(self, status: int, kind: str, key: str | None = None, **kw) -> AtlassianHTTPError:
        if kind == "not_found" and key is None:
            return self.http_error_class(status, kind, self.not_found_message)
        return self.http_error_class(status, kind, self._msg(key or kind, **kw))

    def _deadline_error(self) -> AtlassianHTTPError:
        return self.http_error_class(0, "deadline", self._msg("timeout"))

    # ------------------------------------------------------------- session
    def _session(self) -> requests.Session:
        local = self.__dict__.setdefault("_local", threading.local())
        session = getattr(local, "session", None)
        if session is None:
            session = pinned_session()
            session.trust_env = False   # no ambient proxy or .netrc credentials
            session.verify = os.environ.get("REQUESTS_CA_BUNDLE") or True
            session.headers["Accept"] = "application/json"
            session.headers["Authorization"] = self._auth_header
            local.session = session
        return session

    def _send(self, op: str, path: str, params=None, deadline=None) -> tuple[int, bytes, dict]:
        """One GET. Returns (status, body, headers); transport failures raise a fixed error."""
        url = self._base + self.api_root + path
        started = time.monotonic()
        timeout = DEFAULT_TIMEOUT
        if deadline is not None:
            left = deadline - started
            if left <= 0:
                raise self._deadline_error()
            left = max(MIN_TIMEOUT_S, left)
            timeout = (min(DEFAULT_TIMEOUT[0], left), min(DEFAULT_TIMEOUT[1], left))
        status, chunks, headers, failure = 0, [], {}, None
        try:
            with self._session().get(url, params=params, timeout=timeout, allow_redirects=False,
                                     stream=True) as response:
                status = response.status_code
                headers = {k.lower(): v for k, v in response.headers.items()}
                if status == 200:
                    size = 0
                    for chunk in response.iter_content(CHUNK_BYTES):
                        size += len(chunk)
                        if size > MAX_RESPONSE_BYTES:
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
        except TRANSPORT_ERRORS as exc:
            failure = "unreachable"
            logger.debug("%s %s transport failure: %s", self.log_name, op, type(exc).__name__)
        # Raised outside the except blocks, so no exception chain carries the URL.
        if failure == "deadline" or (failure == "timeout" and deadline is not None
                                     and deadline - time.monotonic() < MIN_TIMEOUT_S):
            raise self._deadline_error()
        if failure:
            raise self._error(status, failure)
        logger.debug("%s %s -> %s in %d ms", self.log_name, op, status, int((time.monotonic() - started) * 1000))
        return status, b"".join(chunks), headers

    def _get(self, op: str, path: str, params=None, deadline=None):
        """GET ``{base}{api_root}{path}`` and return its JSON; maps every failure to a fixed error."""
        for attempt in range(RATE_LIMIT_RETRIES + 1):
            status, body, headers = self._send(op, path, params, deadline)
            if status != 429 or attempt == RATE_LIMIT_RETRIES:
                break
            wait = self._retry_after(headers)
            if deadline is not None and time.monotonic() + wait >= deadline:
                break
            logger.info("%s %s rate-limited; retrying in %.1fs", self.log_name, op, wait)
            _sleep(wait)
        if status == 401:
            if "captcha" in (headers.get("x-authentication-denied-reason") or "").lower():
                raise self._error(401, "captcha")
            raise self._error(401, "auth", "token_rejected" if self.auth_method == "token" else "password_rejected")
        if 300 <= status < 400:
            raise self._error(status, "redirect", "redirected")
        if status != 200:
            if status == 403:
                raise self._error(403, "forbidden")
            if status == 404:
                raise self._error(404, "not_found")
            if status == 400:
                raise self._error(400, "bad_request")
            if status == 429:
                raise self._error(429, "throttled")
            raise self._error(status, "server", status=status)
        if "json" not in (headers.get("content-type") or "application/json").lower():
            raise self._error(200, "not_api")
        try:
            return json.loads(body)
        except (ValueError, RecursionError):
            pass
        raise self._error(200, "not_api")

    @staticmethod
    def _retry_after(headers: dict) -> float:
        raw = headers.get("retry-after")
        try:
            return max(0.0, min(MAX_RETRY_AFTER_S, float(raw)))
        except (TypeError, ValueError):
            return 2.0
