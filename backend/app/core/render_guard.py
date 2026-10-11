"""Outbound-request guard for server-side headless Chromium (SSRF).

Artifact code is model-authored and runs inside a headless browser on the
server for PDF export and thumbnails (and the agent browser tool drives a
browser on the server too). Anything that page fetches is fetched from inside
the deployment — ``<img src="http://169.254.169.254/...">`` reaches the cloud
metadata service.

Two layers, shared by every server-side renderer:

1. ``guard_page_requests`` — a Playwright ``route`` handler that aborts every
   request the policy does not allow (cheap, early, logged).
2. ``RenderEgressProxy`` — a local forward proxy Chromium is launched behind.
   Route handlers never see redirect hops (Chromium follows a 3xx without
   re-entering the handler), so an allowed URL that answers
   ``302 Location: http://169.254.169.254/`` would otherwise go straight
   through. Every connection, including each redirect hop, goes through the
   proxy, which decides on ``host:port``, resolves the name once, validates the
   address (``app.core.egress``) and connects to *that* address — so DNS
   rebinding has nothing to switch either.

Renderer policy (PDF export, thumbnails): only the app's own render origin and
``RENDER_ASSET_HOSTS``. Everything a page artifact needs (React, ECharts,
Tailwind, lucide, fonts) is vendored and inlined by ``app.services.artifact_libs``
— the renderer loads nothing from a CDN, and the in-app artifact iframe's CSP
(``frontend/utils/artifactIframe.ts``) already refuses external scripts, styles,
fonts and ``connect-src``. So the asset allowlist is empty: a page that pulls
other external assets renders without them in exports.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, Iterable, List, Optional, Union
from urllib.parse import urlsplit

from app.core.egress import IPAddress, Policy, check_host, resolve_host
from app.core.url_safety import UnsafeURLError

logger = logging.getLogger(__name__)

# Hosts (https, port 443) a renderer may load assets from besides the app's own
# origin. Derived from the code: none — see the module docstring.
RENDER_ASSET_HOSTS: frozenset[str] = frozenset()

# Decide where (host, port) may go: return the validated addresses to connect
# to (tried in order), or raise UnsafeURLError. Runs in a worker thread (it may
# resolve DNS).
Decider = Callable[[str, int], List[IPAddress]]

_INLINE_SCHEMES = ("data", "blob", "about")


def _origin_key(url: str) -> Optional[tuple[str, str, int]]:
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    scheme = (parts.scheme or "").lower()
    host = (parts.hostname or "").lower().rstrip(".")
    if scheme not in ("http", "https") or not host:
        return None
    return scheme, host, port or (443 if scheme == "https" else 80)


class RendererPolicy:
    """Allow the app's own origin(s) and ``RENDER_ASSET_HOSTS``; nothing else.

    The own origin is trusted deployment config (often loopback), so it is
    connected to without the private-address policy; asset hosts get the
    admin tier (never metadata / link-local)."""

    def __init__(self, allowed_origins: Iterable[str] = (), asset_hosts: Iterable[str] = RENDER_ASSET_HOSTS):
        self.origins = {k for k in (_origin_key(o) for o in allowed_origins if o) if k}
        self.asset_hosts = {h.lower().rstrip(".") for h in asset_hosts}

    def allows_url(self, url: str) -> bool:
        key = _origin_key(url)
        if key is None:
            return False
        if key in self.origins:
            return True
        return key[0] == "https" and key[2] == 443 and key[1] in self.asset_hosts

    def decide(self, host: str, port: int) -> List[IPAddress]:
        name = host.lower().rstrip(".").strip("[]")
        if any(o[1] == name and o[2] == port for o in self.origins):
            addrs = resolve_host(name)
            if not addrs:
                raise UnsafeURLError(f"Could not resolve host {name!r}.")
            return addrs
        if name in self.asset_hosts and port == 443:
            return check_host(name, Policy.ADMIN)
        raise UnsafeURLError(f"{name}:{port} is not an allowed render destination.")


async def guard_page_requests(target, allows_url: Callable[[str], Union[bool, Awaitable[bool]]], *, label: str = "render") -> None:
    """Route every request of ``target`` (a Page or BrowserContext) through
    ``allows_url``; inline ``data:`` / ``blob:`` / ``about:`` always load."""

    async def handler(route, request):
        url = request.url
        scheme = url.split(":", 1)[0].lower()
        try:
            if scheme in _INLINE_SCHEMES:
                return await route.continue_()
            ok = allows_url(url)
            if asyncio.iscoroutine(ok):
                ok = await ok
            if ok:
                return await route.continue_()
        except Exception:  # noqa: BLE001 — a guard error must fail closed
            logger.exception("%s request guard failed for %s", label, url[:200])
        logger.warning("%s blocked request to %s", label, url[:200])
        try:
            await route.abort("blockedbyclient")
        except Exception:  # noqa: BLE001
            pass

    await target.route("**/*", handler)


class RenderEgressProxy:
    """In-process HTTP forward proxy enforcing ``decide(host, port)``.

    ``async with RenderEgressProxy(policy.decide) as proxy:`` then launch
    Chromium with ``**proxy.launch_kwargs()``. CONNECT (https / wss) is
    tunnelled to the decided address; plain-http requests are forwarded with
    ``Connection: close`` so every request re-enters the decision.
    """

    _MAX_HEAD = 64 * 1024

    def __init__(self, decide: Decider, *, label: str = "render"):
        self._decide = decide
        self._label = label
        self._server: Optional[asyncio.base_events.Server] = None
        self.port: Optional[int] = None
        self.refused: list[str] = []
        self._tasks: set[asyncio.Task] = set()

    async def __aenter__(self) -> "RenderEgressProxy":
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc) -> None:
        if self._server is not None:
            self._server.close()
            for t in list(self._tasks):
                t.cancel()
            try:
                await asyncio.wait_for(self._server.wait_closed(), timeout=2)
            except Exception:  # noqa: BLE001
                pass
            self._server = None

    def launch_kwargs(self) -> dict:
        """Playwright ``chromium.launch`` kwargs. ``<-loopback>`` removes
        Chromium's implicit proxy bypass for localhost / 127.0.0.1, so loopback
        targets go through the decision too."""
        return {
            "proxy": {"server": f"http://127.0.0.1:{self.port}", "bypass": "<-loopback>"},
        }

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._tasks.add(task)
        upstream_writer = None
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=30)
            if len(head) > self._MAX_HEAD:
                return
            lines = head.decode("latin-1").split("\r\n")
            method, target, version = (lines[0].split(" ") + ["", "", ""])[:3]
            if method.upper() == "CONNECT":
                host, _, port_s = target.rpartition(":")
                port = int(port_s)
                rest = b""
            else:
                parts = urlsplit(target)
                if parts.scheme.lower() != "http" or not parts.hostname:
                    return await self._refuse(writer, target)
                host, port = parts.hostname, parts.port or 80
                path = parts.path or "/"
                if parts.query:
                    path += "?" + parts.query
                headers = [
                    ln for ln in lines[1:]
                    if ln and ln.split(":", 1)[0].strip().lower()
                    not in ("connection", "proxy-connection", "keep-alive", "proxy-authorization")
                ]
                rest = (f"{method} {path} {version}\r\n" + "\r\n".join(headers + ["Connection: close"]) + "\r\n\r\n").encode("latin-1")
            try:
                addrs = await asyncio.to_thread(self._decide, host.strip("[]"), port)
            except (UnsafeURLError, ValueError) as e:
                return await self._refuse(writer, f"{host}:{port}", str(e))
            up_reader = None
            for ip in addrs:  # e.g. localhost -> ::1, 127.0.0.1
                try:
                    up_reader, upstream_writer = await asyncio.wait_for(
                        asyncio.open_connection(str(ip), port), timeout=15
                    )
                    break
                except (OSError, asyncio.TimeoutError):
                    continue
            if up_reader is None:
                writer.write(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                return
            if method.upper() == "CONNECT":
                writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                await writer.drain()
            else:
                upstream_writer.write(rest)
                await upstream_writer.drain()
            await asyncio.gather(
                self._pipe(reader, upstream_writer), self._pipe(up_reader, writer), return_exceptions=True
            )
        except Exception:  # noqa: BLE001 — a bad client request just closes
            pass
        finally:
            for w in (upstream_writer, writer):
                if w is not None:
                    try:
                        w.close()
                    except Exception:  # noqa: BLE001
                        pass
            if task is not None:
                self._tasks.discard(task)

    async def _refuse(self, writer: asyncio.StreamWriter, target: str, why: str = "") -> None:
        self.refused.append(target)
        logger.warning("%s egress proxy refused %s %s", self._label, target[:200], why)
        writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
        try:
            await writer.drain()
        except Exception:  # noqa: BLE001
            pass

    @staticmethod
    async def _pipe(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
        try:
            while True:
                chunk = await src.read(65536)
                if not chunk:
                    break
                dst.write(chunk)
                await dst.drain()
        finally:
            try:
                if dst.can_write_eof():
                    dst.write_eof()
            except Exception:  # noqa: BLE001
                pass

