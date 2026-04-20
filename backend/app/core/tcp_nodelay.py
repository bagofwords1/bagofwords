"""Disable Nagle's algorithm on uvicorn's accepted sockets.

Streaming endpoints (SSE via POST /api/completions with
Accept: text/event-stream, and the /ws/api/reports/{id} WebSocket) emit
many tiny writes. Without TCP_NODELAY, the OS batches them — noticeably
on macOS, where small writes can sit in the send buffer for ~200 ms
before Nagle flushes, producing visibly chunky streaming in the browser.

The previous Nuxt dev/prod proxy happened to set TCP_NODELAY because
Node's HTTP server does so by default. Now that FastAPI serves the
frontend directly, we need to replicate that behaviour on uvicorn's
side.

Toggle with env var UVICORN_TCP_NODELAY (default on). Disable it to A/B
compare streaming smoothness.
"""

from __future__ import annotations

import logging
import os
import socket

log = logging.getLogger(__name__)


def _wrap_connection_made(cls) -> None:
    original = cls.connection_made

    def connection_made(self, transport):  # type: ignore[no-untyped-def]
        sock = transport.get_extra_info("socket")
        if sock is not None:
            try:
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except OSError:
                pass
        return original(self, transport)

    cls.connection_made = connection_made


def enable_tcp_nodelay() -> None:
    """Patch uvicorn's HTTP/WS protocols to set TCP_NODELAY on every
    accepted connection. Idempotent; no-ops if uvicorn isn't importable.
    Must be called before uvicorn starts (i.e. at module import time)."""
    if os.environ.get("UVICORN_TCP_NODELAY", "1").lower() in ("0", "false", "no"):
        return

    patched: list[str] = []

    try:
        from uvicorn.protocols.http.httptools_impl import HttpToolsProtocol
        _wrap_connection_made(HttpToolsProtocol)
        patched.append("httptools")
    except Exception as e:  # pragma: no cover
        log.debug("skip httptools TCP_NODELAY patch: %s", e)

    try:
        from uvicorn.protocols.http.h11_impl import H11Protocol
        _wrap_connection_made(H11Protocol)
        patched.append("h11")
    except Exception as e:  # pragma: no cover
        log.debug("skip h11 TCP_NODELAY patch: %s", e)

    # WebSocket protocols reuse the underlying socket, so TCP_NODELAY
    # already persists from the HTTP upgrade handshake — but patch these
    # too in case uvicorn ever constructs them standalone.
    try:
        from uvicorn.protocols.websockets.websockets_impl import WebSocketProtocol
        _wrap_connection_made(WebSocketProtocol)
        patched.append("ws-websockets")
    except Exception as e:  # pragma: no cover
        log.debug("skip websockets TCP_NODELAY patch: %s", e)

    try:
        from uvicorn.protocols.websockets.wsproto_impl import WSProtocol
        _wrap_connection_made(WSProtocol)
        patched.append("ws-wsproto")
    except Exception as e:  # pragma: no cover
        log.debug("skip wsproto TCP_NODELAY patch: %s", e)

    if patched:
        log.info("TCP_NODELAY enabled for uvicorn protocols: %s", ", ".join(patched))
