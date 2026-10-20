"""Pinned outbound HTTP (SSRF guard that also closes DNS rebinding).

``app.core.url_safety`` decides *which* addresses are allowed. This module makes
the HTTP client actually connect to an address that was checked: it resolves
the hostname once, validates every address it got back, and then connects to
one of those addresses while keeping the hostname for the ``Host`` header, TLS
SNI and certificate verification. A DNS server that answers "public" for the
check and "169.254.169.254" for the connect therefore has nothing to switch.

The check runs inside the transport, so it runs again for every request the
client makes — including each redirect hop httpx follows.

Two policies, matching url_safety's two tiers (unchanged):

* ``Policy.PUBLIC`` — every address must be globally routable (agent-chosen
  URLs: web_fetch, the sandbox ``http`` helper).
* ``Policy.ADMIN`` — cloud metadata and link-local are always refused; private,
  LAN and loopback are refused only when
  ``security.block_private_outbound: true`` (admin-configured URLs: LLM
  ``base_url``, MCP / custom API servers, OAuth discovery, audit streams).

Entry points:

* ``check_host(host, policy) -> list[ip]`` / ``check_url(url, policy)``
* ``pinned_client(policy, **httpx_kwargs) -> httpx.Client``
* ``pinned_async_client(policy, **httpx_kwargs) -> httpx.AsyncClient``
* ``PinnedTransport`` / ``AsyncPinnedTransport`` (pass as ``transport=``)
* ``mcp_client_factory(policy)`` — ``httpx_client_factory`` for the MCP SDK
* ``curl_pinned_request(url, policy) -> (canonical_url, curl_options)`` for
  curl_cffi (``CURLOPT_RESOLVE`` pin)
* ``check_google_token_uri(service_account_info)``
* ``connect_pinned_socket`` / ``open_pinned_socket`` — validated TCP socket for
  non-HTTP protocols (SMTP, IMAP, syslog); the caller keeps the hostname for TLS
* ``check_connection_params`` / ``acheck_connection_params`` — every host a
  data-source connector config names, checked right before a client is built

Known limit: when an environment proxy (``HTTPS_PROXY`` …) applies to a URL the
proxy does the DNS lookup, so the hostname is checked here but the connection
cannot be pinned; the proxy is the egress control point in that deployment.
"""

from __future__ import annotations

import enum
import ipaddress
import socket
import urllib.request
from typing import Any, Optional
from urllib.parse import urlsplit, urlunsplit

import httpx

from app.core.url_safety import (
    IPAddress,
    UnsafeURLError,
    _METADATA_HOSTNAMES,
    ip_is_metadata,
    ip_is_public,
)


class Policy(str, enum.Enum):
    PUBLIC = "public"
    ADMIN = "admin"


class EgressBlockedError(UnsafeURLError, httpx.ConnectError):
    """Raised from inside a pinned transport when a destination is refused.

    Subclasses ``httpx.ConnectError`` so existing ``except httpx.HTTPError``
    handlers treat it like any unreachable host, and ``UnsafeURLError`` so
    callers that care can tell "blocked" from "down".
    """

    def __init__(self, message: str, *, request: Optional[httpx.Request] = None):
        UnsafeURLError.__init__(self, message)
        httpx.ConnectError.__init__(self, message, request=request)


# ── resolution ──────────────────────────────────────────────────────────────

_NUMERIC_CHARS = set("0123456789abcdefABCDEFxX.")


def _getaddrinfo(host: str):  # indirection so tests can simulate DNS
    return socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)


def _literal_ip(host: str) -> Optional[IPAddress]:
    """An IP literal in any form a client might accept: dotted, bracketed v6,
    or the legacy inet_aton forms (``2130706433``, ``0x7f.1``, ``0177.0.0.1``)."""
    h = host.strip("[]")
    try:
        return ipaddress.ip_address(h.split("%", 1)[0])
    except ValueError:
        pass
    if h and set(h) <= _NUMERIC_CHARS and any(c.isdigit() for c in h):
        try:
            return ipaddress.IPv4Address(socket.inet_aton(h.rstrip(".")))
        except OSError:
            return None
    return None


def resolve_host(host: str) -> list[IPAddress]:
    """Every address ``host`` resolves to (deduplicated, order kept)."""
    lit = _literal_ip(host)
    if lit is not None:
        return [lit]
    try:
        infos = _getaddrinfo(host)
    except (socket.gaierror, UnicodeError, OSError):
        return []
    out: list[IPAddress] = []
    for info in infos:
        try:
            ip = ipaddress.ip_address(str(info[4][0]).split("%", 1)[0])
        except ValueError:
            continue
        if ip not in out:
            out.append(ip)
    return out


# ── policy ──────────────────────────────────────────────────────────────────


def block_private_outbound() -> bool:
    try:
        from app.settings.config import settings

        return bool(settings.bow_config.security.block_private_outbound)
    except Exception:
        return False


def _embedded_v4(ip: IPAddress) -> list[IPAddress]:
    """IPv4 addresses an IPv6 address carries and may be routed to."""
    if not isinstance(ip, ipaddress.IPv6Address):
        return []
    out: list[IPAddress] = []
    if ip.ipv4_mapped is not None:
        out.append(ip.ipv4_mapped)
    if ip.sixtofour is not None:
        out.append(ip.sixtofour)
    if ip.teredo is not None:
        out.extend(ip.teredo)
    packed = int(ip)
    if ip in ipaddress.ip_network("64:ff9b::/96") or ip in ipaddress.ip_network("64:ff9b:1::/48"):
        out.append(ipaddress.IPv4Address(packed & 0xFFFFFFFF))
    # Deprecated IPv4-compatible ::a.b.c.d (but not :: or ::1).
    if packed >> 32 == 0 and packed > 1:
        out.append(ipaddress.IPv4Address(packed & 0xFFFFFFFF))
    return out


def ip_violation(ip: IPAddress, policy: Policy, block_private: Optional[bool] = None) -> Optional[str]:
    """Why ``ip`` is refused under ``policy``, or None when it is allowed."""
    candidates = [ip, *_embedded_v4(ip)]
    if any(ip_is_metadata(c) for c in candidates):
        return "link-local or cloud metadata address"
    if block_private is None:
        block_private = block_private_outbound()
    if policy == Policy.PUBLIC or block_private:
        if not all(ip_is_public(c) for c in candidates):
            return "non-public address"
    return None


def _normalize_host(host: Optional[str]) -> str:
    return (host or "").strip().strip("[]").rstrip(".").lower()


def check_host(
    host: Optional[str], policy: Policy | str = Policy.ADMIN, *, require_resolution: bool = True
) -> list[IPAddress]:
    """Resolve ``host`` once and validate every address.

    Returns the validated addresses (connect to one of these). Raises
    ``UnsafeURLError`` when any address is refused, or when nothing resolves
    and ``require_resolution`` is set (a pinned connect needs an address; a
    save-time check can let an unresolvable host fail later at connect).
    """
    policy = Policy(policy)
    name = _normalize_host(host)
    if not name:
        raise UnsafeURLError("URL must include a hostname.")
    if name in _METADATA_HOSTNAMES:
        raise UnsafeURLError("URL points at a cloud metadata service.")
    block_private = block_private_outbound()
    if (policy == Policy.PUBLIC or block_private) and (name == "localhost" or name.endswith(".localhost")):
        raise UnsafeURLError("URL must point at a public address.")
    addrs = resolve_host(name)
    if not addrs:
        if require_resolution:
            raise UnsafeURLError(f"Could not resolve host {name!r}.")
        return []
    for ip in addrs:
        reason = ip_violation(ip, policy, block_private)
        if reason:
            raise UnsafeURLError(f"Refusing to connect to {name!r}: {reason}.")
    return addrs


def check_url(
    url: str,
    policy: Policy | str = Policy.ADMIN,
    schemes=("http", "https"),
    *,
    require_resolution: Optional[bool] = None,
) -> list[IPAddress]:
    """Scheme allowlist + ``check_host`` for a full URL (save-time guard).

    By default an unresolvable host passes the admin tier (it cannot reach
    anything; the pinned connect fails it later) and fails the public tier —
    the same semantics as ``url_safety``."""
    policy = Policy(policy)
    parts = _split_strict(url, schemes)
    if require_resolution is None:
        require_resolution = policy == Policy.PUBLIC
    return check_host(parts.hostname, policy, require_resolution=require_resolution)


def _split_strict(url: str, schemes=("http", "https")):
    raw = url or ""
    if any(c in raw for c in "\\\t\r\n ") or any(ord(c) < 0x20 or ord(c) == 0x7F for c in raw):
        raise UnsafeURLError("URL contains characters that are not allowed.")
    parts = urlsplit(raw)
    if parts.scheme.lower() not in set(schemes):
        raise UnsafeURLError(f"URL scheme must be one of: {', '.join(schemes)}.")
    if not parts.hostname:
        raise UnsafeURLError("URL must include a hostname.")
    try:
        parts.port
    except ValueError:
        raise UnsafeURLError("URL has an invalid port.")
    return parts


# ── proxies ─────────────────────────────────────────────────────────────────


def env_proxy_for(url: str) -> Optional[str]:
    """The environment proxy httpx/curl would use for ``url`` (None if none)."""
    parts = urlsplit(url)
    try:
        proxies = urllib.request.getproxies()
    except Exception:
        return None
    proxy = proxies.get(parts.scheme.lower()) or proxies.get("all")
    if not proxy:
        return None
    try:
        if urllib.request.proxy_bypass(parts.hostname or ""):
            return None
    except Exception:
        pass
    return proxy


# ── httpx transports ────────────────────────────────────────────────────────

_TRANSPORT_KWARGS = ("verify", "cert", "trust_env", "http1", "http2", "limits", "retries", "local_address", "socket_options")


def _fmt_ip(ip: IPAddress) -> str:
    return str(ip)


class _PinCore:
    def __init__(self, policy: Policy | str = Policy.ADMIN, **transport_kwargs: Any):
        self._policy = Policy(policy)
        self._trust_env = transport_kwargs.pop("trust_env", True)
        self._kwargs = transport_kwargs
        self._pools: dict[tuple, Any] = {}

    def _plan(self, request: httpx.Request) -> tuple[tuple, httpx.Request]:
        url = request.url
        if url.scheme not in ("http", "https"):
            raise EgressBlockedError(f"Scheme {url.scheme!r} is not allowed.", request=request)
        host = url.host
        proxy = env_proxy_for(str(url)) if self._trust_env else None
        try:
            # Behind a proxy the server may have no DNS for the target at all
            # (the proxy resolves it), so an unresolvable name is not an error
            # there; a name that does resolve is still validated.
            addrs = check_host(host, self._policy, require_resolution=not proxy)
        except UnsafeURLError as e:
            raise EgressBlockedError(str(e), request=request) from None
        if proxy:
            # The proxy resolves; the hostname check above is all we can do.
            return ("proxy", proxy), request
        ip = addrs[0]
        port = url.port or (443 if url.scheme == "https" else 80)
        extensions = dict(request.extensions)
        if url.scheme == "https":
            extensions["sni_hostname"] = host
        pinned = httpx.Request(
            request.method,
            url.copy_with(host=_fmt_ip(ip)),
            headers=request.headers,  # keeps the original Host header
            stream=request.stream,
            extensions=extensions,
        )
        # One pool per (hostname, ip): a TLS connection verified for one
        # hostname is never reused for another that shares the address.
        return (url.scheme, host, port, ip), pinned


class PinnedTransport(_PinCore, httpx.BaseTransport):
    """Sync httpx transport that validates and pins every request."""

    def _pool(self, key) -> httpx.HTTPTransport:
        pool = self._pools.get(key)
        if pool is None:
            kw = dict(self._kwargs)
            if key[0] == "proxy":
                kw["proxy"] = key[1]
            pool = self._pools[key] = httpx.HTTPTransport(**kw)
        return pool

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        key, pinned = self._plan(request)
        return self._pool(key).handle_request(pinned)

    def close(self) -> None:
        for pool in list(self._pools.values()):
            pool.close()
        self._pools.clear()


class AsyncPinnedTransport(_PinCore, httpx.AsyncBaseTransport):
    """Async httpx transport that validates and pins every request."""

    def _pool(self, key) -> httpx.AsyncHTTPTransport:
        pool = self._pools.get(key)
        if pool is None:
            kw = dict(self._kwargs)
            if key[0] == "proxy":
                kw["proxy"] = key[1]
            pool = self._pools[key] = httpx.AsyncHTTPTransport(**kw)
        return pool

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        key, pinned = self._plan(request)
        return await self._pool(key).handle_async_request(pinned)

    async def aclose(self) -> None:
        for pool in list(self._pools.values()):
            await pool.aclose()
        self._pools.clear()


def _split_kwargs(kwargs: dict) -> tuple[dict, dict]:
    transport_kw = {k: kwargs.pop(k) for k in list(kwargs) if k in _TRANSPORT_KWARGS}
    if "trust_env" in transport_kw:
        kwargs["trust_env"] = transport_kw["trust_env"]
    return transport_kw, kwargs


def pinned_client(policy: Policy | str = Policy.ADMIN, **kwargs: Any) -> httpx.Client:
    """``httpx.Client(**kwargs)`` whose every request (and redirect hop) is
    validated under ``policy`` and pinned to the validated address.
    TLS options (``verify``, ``cert``, ``http2`` …) are applied to the pinned
    connections; certificate checks still use the hostname."""
    transport_kw, client_kw = _split_kwargs(dict(kwargs))
    return httpx.Client(transport=PinnedTransport(policy, **transport_kw), **client_kw)


def pinned_async_client(policy: Policy | str = Policy.ADMIN, **kwargs: Any) -> httpx.AsyncClient:
    """Async counterpart of ``pinned_client``."""
    transport_kw, client_kw = _split_kwargs(dict(kwargs))
    return httpx.AsyncClient(transport=AsyncPinnedTransport(policy, **transport_kw), **client_kw)


def mcp_client_factory(policy: Policy | str = Policy.ADMIN):
    """An ``httpx_client_factory`` for ``mcp.client.{sse,streamable_http}``
    with the SDK's own defaults (30s / 300s read, no auto-redirects)."""

    def factory(headers=None, timeout=None, auth=None) -> httpx.AsyncClient:
        if timeout is None:
            timeout = httpx.Timeout(30.0, read=300.0)
        kw: dict[str, Any] = {"timeout": timeout}
        if headers is not None:
            kw["headers"] = headers
        if auth is not None:
            kw["auth"] = auth
        return pinned_async_client(policy, **kw)

    return factory


# ── curl_cffi ───────────────────────────────────────────────────────────────


def curl_pinned_request(url: str, policy: Policy | str = Policy.PUBLIC) -> tuple[str, dict]:
    """Validate ``url`` and return ``(canonical_url, curl_options)``.

    ``canonical_url`` is rebuilt from the parsed parts (no userinfo, IDNA host,
    no trailing dot, numeric IPs in dotted form) so curl's URL parser sees
    exactly the host that was checked; ``curl_options`` pins that host to the
    validated address with ``CURLOPT_RESOLVE``. Raises ``UnsafeURLError``.
    """
    from curl_cffi.const import CurlOpt

    parts = _split_strict(url)
    raw_host = parts.hostname.rstrip(".")
    lit = _literal_ip(raw_host)
    if lit is not None:
        host = str(lit)
    else:
        try:
            host = raw_host.encode("idna").decode("ascii").lower()
        except UnicodeError:
            raise UnsafeURLError("URL host is not a valid hostname.")
    addrs = check_host(host, policy)
    scheme = parts.scheme.lower()
    port = parts.port or (443 if scheme == "https" else 80)
    host_in_url = f"[{host}]" if ":" in host else host
    netloc = host_in_url if parts.port is None else f"{host_in_url}:{parts.port}"
    canonical = urlunsplit((scheme, netloc, parts.path, parts.query, ""))
    ip = addrs[0]
    ip_txt = f"[{ip}]" if ip.version == 6 else str(ip)
    return canonical, {CurlOpt.RESOLVE: [f"{host}:{port}:{ip_txt}"]}


def check_connected_ip(primary_ip: Optional[str], url: str, policy: Policy | str) -> None:
    """Defense in depth after a curl request: the address curl actually
    connected to must pass the policy (skipped behind an env proxy)."""
    if not primary_ip or env_proxy_for(url):
        return
    try:
        ip = ipaddress.ip_address(primary_ip.strip("[]"))
    except ValueError:
        return
    reason = ip_violation(ip, Policy(policy))
    if reason:
        raise UnsafeURLError(f"Connected to a refused address: {reason}.")


# ── Google service-account keys ─────────────────────────────────────────────

GOOGLE_TOKEN_URIS = frozenset({
    "https://oauth2.googleapis.com/token",
    "https://accounts.google.com/o/oauth2/token",
})


def check_google_token_uri(info: Any) -> None:
    """A service-account key's ``token_uri`` is where google-auth POSTs a
    signed assertion; it must be Google's token endpoint. Raises
    ``UnsafeURLError``."""
    uri = (info or {}).get("token_uri") if isinstance(info, dict) else None
    if uri not in GOOGLE_TOKEN_URIS:
        raise UnsafeURLError(
            "service account token_uri must be https://oauth2.googleapis.com/token"
        )


# ── non-HTTP sockets (SMTP, IMAP, syslog) ───────────────────────────────────
#
# Same idea as the transports above for protocols that take a socket: resolve
# once, validate every address, connect to a validated one. The caller keeps
# the hostname for TLS (SNI + certificate verification).


def _sock_family(ip: IPAddress) -> int:
    return socket.AF_INET6 if ip.version == 6 else socket.AF_INET


def connect_pinned_socket(
    host: str, port: int, policy: Policy | str = Policy.ADMIN, *, timeout: Optional[float] = None
) -> socket.socket:
    """Blocking TCP connect to a validated address of ``host``.
    Raises ``UnsafeURLError`` when refused, ``OSError`` when unreachable."""
    last: Optional[OSError] = None
    for ip in check_host(host, policy):
        s = socket.socket(_sock_family(ip), socket.SOCK_STREAM)
        s.settimeout(timeout)
        try:
            s.connect((str(ip), int(port)))
            return s
        except OSError as e:
            s.close()
            last = e
    raise last or OSError(f"Could not connect to {host!r}")


async def open_pinned_socket(
    host: str, port: int, policy: Policy | str = Policy.ADMIN, *, timeout: Optional[float] = None
) -> socket.socket:
    """Async counterpart of ``connect_pinned_socket``: a connected,
    non-blocking socket to hand to ``asyncio.open_connection(sock=...)`` or
    ``aiosmtplib.SMTP(sock=...)``."""
    import asyncio

    addrs = await asyncio.to_thread(check_host, host, policy)
    loop = asyncio.get_running_loop()
    last: Optional[BaseException] = None
    for ip in addrs:
        s = socket.socket(_sock_family(ip), socket.SOCK_STREAM)
        s.setblocking(False)
        try:
            await asyncio.wait_for(loop.sock_connect(s, (str(ip), int(port))), timeout)
            return s
        except (OSError, asyncio.TimeoutError) as e:
            s.close()
            last = e
    if last is not None:
        raise last
    raise OSError(f"Could not connect to {host!r}")


# ── connection (data source) configs ────────────────────────────────────────
#
# Database / API connectors take their destination from admin-entered config.
# Drivers (libpq, ODBC, vendor SDKs) do their own DNS and cannot be handed a
# pre-validated socket, so the hosts are checked right before a client is
# built — at every create / update / test / index / query — rather than
# pinned. See docs/feedback-loops/ssrf-egress-pinning.md (package B).

CONNECTION_HOST_KEYS = ("host", "hostname", "server", "server_hostname")
CONNECTION_URL_KEYS = (
    "url", "base_url", "server_url", "site_url", "instance_url", "api_url",
    "cluster_url", "endpoint_url", "sts_endpoint_url",
)
# Keys that hold a host only in some forms (Salesforce ``domain`` is
# "login" / "acme" or a full host).
_TYPE_HOST_KEYS = {"salesforce": ("domain",)}


def hosts_in(value: Any) -> list[str]:
    """Hostnames a host-ish config value names: ``db.internal``,
    ``db:5432``, ``[::1]:5432``, ``h1:27017,h2:27017``, ``https://h/x``,
    ``mongodb+srv://user:pw@h1,h2/db``."""
    import re

    v = str(value or "").strip()
    if not v:
        return []
    rest = v.split("://", 1)[1] if "://" in v else v
    netloc = re.split(r"[/?#]", rest, maxsplit=1)[0].rsplit("@", 1)[-1]
    out: list[str] = []
    for part in netloc.split(","):
        part = part.strip()
        if not part:
            continue
        if part.startswith("["):
            h = part[1:part.index("]")] if "]" in part else part[1:]
        elif part.count(":") == 1:
            h = part.split(":", 1)[0]
        else:
            h = part
        if h and h not in out:
            out.append(h)
    return out


def connection_hosts(conn_type: Optional[str], params: Optional[dict]) -> list[str]:
    """Every destination host a connector config names."""
    out: list[str] = []
    params = params or {}
    keys = [(k, False) for k in CONNECTION_HOST_KEYS + CONNECTION_URL_KEYS]
    keys += [(k, True) for k in _TYPE_HOST_KEYS.get(conn_type or "", ())]
    for key, only_full in keys:
        v = params.get(key)
        if not isinstance(v, str) or (only_full and "." not in v and "://" not in v):
            continue
        out.extend(h for h in hosts_in(v) if h not in out)
    return out


def check_connection_params(
    conn_type: Optional[str], params: Optional[dict], policy: Policy | str = Policy.ADMIN
) -> None:
    """Admin-tier check of every host a connector config names. Unresolvable
    hosts pass (the driver fails them itself). Raises ``UnsafeURLError``."""
    for host in connection_hosts(conn_type, params):
        check_host(host, policy, require_resolution=False)


async def acheck_connection_params(
    conn_type: Optional[str], params: Optional[dict], policy: Policy | str = Policy.ADMIN
) -> None:
    """``check_connection_params`` off the event loop (DNS can block)."""
    import asyncio

    await asyncio.to_thread(check_connection_params, conn_type, params, policy)
