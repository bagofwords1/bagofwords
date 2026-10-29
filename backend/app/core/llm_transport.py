"""Connection pools shared by every LLM provider client in the process.

The agent builds new SDK clients for every message (planner, reporter, side
calls). Each used to carry its own httpx pool, so the first model call of every
message opened a new TCP + TLS connection to the provider. Clients now stay
per message but route through these shared transports, so a message reuses a
warm connection from the previous one.

* Async pools are kept per event loop: an asyncio connection belongs to the
  loop that opened it, and some LLM calls run on worker-thread loops.
* Closing one SDK client never closes the shared pool (``aclose``/``close``
  are no-ops here); pools live for the process.
* The pinned variant (admin-configured base URLs, app.core.egress) still
  validates and pins every request; only its connections are shared.
* Clients built with their own TLS settings (``verify=False``) or extra
  transport options keep a private pool, as before.
"""
from __future__ import annotations

import asyncio
import os
import threading
import weakref
from typing import Callable, Dict, Optional, Tuple

import httpx

from app.core.tls import shared_ssl_context


def _limits() -> httpx.Limits:
    try:
        idle = float(os.environ.get("BOW_LLM_KEEPALIVE_S", "60"))
    except ValueError:
        idle = 60.0
    return httpx.Limits(max_connections=200, max_keepalive_connections=40, keepalive_expiry=idle)


class _SharedSync(httpx.BaseTransport):
    def __init__(self, factory: Callable[[], httpx.BaseTransport], pinned: bool = False):
        self._inner = factory()
        self.pinned = pinned

    def transport(self) -> httpx.BaseTransport:
        """The underlying (pooled) transport."""
        return self._inner

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        return self._inner.handle_request(request)

    def close(self) -> None:  # shared: one client closing must not close it
        pass


class _SharedAsync(httpx.AsyncBaseTransport):
    def __init__(self, factory: Callable[[], httpx.AsyncBaseTransport], pinned: bool = False):
        self._factory = factory
        self.pinned = pinned
        self._by_loop: Dict[int, Tuple[weakref.ref, httpx.AsyncBaseTransport]] = {}
        self._lock = threading.Lock()

    def _for_running_loop(self) -> httpx.AsyncBaseTransport:
        loop = asyncio.get_running_loop()
        entry = self._by_loop.get(id(loop))
        if entry is not None and entry[0]() is loop:
            return entry[1]
        with self._lock:
            entry = self._by_loop.get(id(loop))
            if entry is not None and entry[0]() is loop:
                return entry[1]
            # Drop pools of loops that are gone (worker threads come and go).
            for key, (ref, _) in list(self._by_loop.items()):
                dead = ref()
                if dead is None or dead.is_closed():
                    self._by_loop.pop(key, None)
            transport = self._factory()
            self._by_loop[id(loop)] = (weakref.ref(loop), transport)
            return transport

    def transport(self) -> httpx.AsyncBaseTransport:
        """The running loop's underlying (pooled) transport."""
        return self._for_running_loop()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return await self._for_running_loop().handle_async_request(request)

    async def aclose(self) -> None:  # shared: one client closing must not close it
        pass


_shared: Dict[bool, Tuple[_SharedSync, _SharedAsync]] = {}
_shared_lock = threading.Lock()


def shared_transports(pinned: bool = False) -> Tuple[_SharedSync, _SharedAsync]:
    """(sync, async) transports shared process-wide, on the shared TLS context.

    ``pinned``: validate and pin every request (admin-configured URLs).
    """
    got = _shared.get(pinned)
    if got is not None:
        return got
    with _shared_lock:
        got = _shared.get(pinned)
        if got is None:
            tls = shared_ssl_context()
            if pinned:
                from app.core.egress import AsyncPinnedTransport, PinnedTransport, Policy

                got = (
                    _SharedSync(lambda: PinnedTransport(Policy.ADMIN, verify=tls, limits=_limits()), pinned=True),
                    _SharedAsync(lambda: AsyncPinnedTransport(Policy.ADMIN, verify=tls, limits=_limits()), pinned=True),
                )
            else:
                got = (
                    _SharedSync(lambda: httpx.HTTPTransport(verify=tls, limits=_limits())),
                    _SharedAsync(lambda: httpx.AsyncHTTPTransport(verify=tls, limits=_limits())),
                )
            _shared[pinned] = got
    return got


async def preconnect(async_http_client: Optional[httpx.AsyncClient], base_url: str, timeout: float = 5.0) -> bool:
    """Open a connection to ``base_url`` in the shared pool, ahead of the
    first model call. Sends an unauthenticated HEAD (no SDK, so no API key);
    the response is ignored. Returns True when a request was sent."""
    if async_http_client is None or not base_url:
        return False
    transport = getattr(async_http_client, "_transport", None)
    if not isinstance(transport, _SharedAsync):
        return False
    try:
        await async_http_client.request("HEAD", base_url, timeout=timeout)
        return True
    except Exception:
        return False
