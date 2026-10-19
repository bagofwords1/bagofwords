"""Keep live streams flowing through proxies, and make it visible when they don't.

Live responses (chat SSE, the report activity stream, MCP tool streams) are
only useful if every hop forwards bytes as they are written. Two things here:

``NO_BUFFER_HEADERS``
    Response headers for every live stream:

    - ``Cache-Control: no-transform`` tells proxies, TLS-inspection gateways
      and AV/DLP scanners not to modify (recompress, rewrite) the response.
    - ``Content-Encoding: identity`` declares the body unencoded. Reverse
      proxies with response compression turned on (Caddy's ``encode``, nginx
      ``gzip``) skip a response that already names its encoding. A gzipped
      event stream is what makes content-inspecting middleboxes hold the whole
      answer: they must decode the full body before they can scan it.
    - ``X-Accel-Buffering: no`` turns off nginx proxy buffering.

``StreamStats``
    One INFO log line per stream (time to first byte, events, bytes,
    duration, how it ended), plus the request headers that reveal which
    proxies sit in front of the app.

``analyze_client_delivery``
    Turns what the browser measured about a stream it received into a
    verdict. The browser compares each token event's server timestamp with
    when it arrived and reports the longest hold-back; clock skew between
    server and browser cancels out because only differences are compared.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import AsyncIterator, Optional

from starlette.requests import Request

logger = logging.getLogger(__name__)

NO_BUFFER_HEADERS: dict[str, str] = {
    "Cache-Control": "no-cache, no-transform",
    "Content-Encoding": "identity",
    "X-Accel-Buffering": "no",
}

# A stream counts as buffered when a token was held back at least this long
# AND for at least this share of the time the tokens took to produce. Short
# turns (a few hundred ms of tokens) never qualify — there is nothing to see.
BUFFERED_MIN_HOLDBACK_MS = 1500
BUFFERED_MIN_SHARE = 0.25

_SYSTEM_COMPLETION_ID = re.compile(r'"system_completion_id":\s*"([0-9a-fA-F-]{8,64})"')

# Request headers that reveal intermediaries between the browser and the app.
_PROXY_HINT_HEADERS = ("via", "x-forwarded-for", "forwarded", "x-bluecoat-via", "x-zscaler-via")


def _proxy_hints(request: Optional[Request]) -> str:
    if request is None:
        return "-"
    hints = []
    for name in _PROXY_HINT_HEADERS:
        value = request.headers.get(name)
        if value:
            hints.append(f"{name}={value[:120]}")
    return " ".join(hints) or "-"


class StreamStats:
    """Count what a streaming generator writes and log it once at the end."""

    def __init__(self, kind: str, completion_id: Optional[str], request: Optional[Request] = None):
        self.kind = kind
        self.completion_id = completion_id
        self.request = request
        self.started = time.monotonic()
        self.first_byte_ms: Optional[float] = None
        self.events = 0
        self.bytes = 0

    def record(self, chunk: str) -> None:
        if self.first_byte_ms is None:
            self.first_byte_ms = (time.monotonic() - self.started) * 1000.0
        self.bytes += len(chunk.encode("utf-8"))
        if self.completion_id is None and self.events == 0:
            # The kickoff stream's first event names the run it belongs to.
            match = _SYSTEM_COMPLETION_ID.search(chunk)
            if match:
                self.completion_id = match.group(1)
        if not chunk.startswith(":"):  # SSE comments are heartbeats, not events
            self.events += 1

    def log(self, outcome: str) -> None:
        logger.info(
            "sse.stream kind=%s completion=%s outcome=%s first_byte_ms=%s events=%d bytes=%d "
            "duration_ms=%d accept_encoding=%s proxies=%s",
            self.kind, self.completion_id or "-", outcome,
            "-" if self.first_byte_ms is None else round(self.first_byte_ms),
            self.events, self.bytes, round((time.monotonic() - self.started) * 1000.0),
            (self.request.headers.get("accept-encoding", "-") if self.request is not None else "-"),
            _proxy_hints(self.request),
        )


async def instrument_stream(
    source: AsyncIterator[str],
    kind: str,
    completion_id: Optional[str] = None,
    request: Optional[Request] = None,
) -> AsyncIterator[str]:
    """Yield ``source`` unchanged while recording StreamStats; logs on exit."""
    stats = StreamStats(kind, completion_id, request)
    outcome = "finished"
    try:
        async for chunk in source:
            stats.record(chunk)
            yield chunk
    except (GeneratorExit, asyncio.CancelledError):
        # The client (or a proxy in front of it) went away mid-stream.
        outcome = "disconnected"
        raise
    except Exception:
        outcome = "error"
        raise
    finally:
        # Close the wrapped generator now, so its own cleanup (unsubscribing
        # from the live queue, …) runs on disconnect exactly as it did before
        # it was wrapped, instead of whenever it is garbage-collected.
        aclose = getattr(source, "aclose", None)
        if aclose is not None:
            await aclose()
        stats.log(outcome)


def is_buffered(holdback_ms: float, server_span_ms: float) -> bool:
    return holdback_ms >= BUFFERED_MIN_HOLDBACK_MS and holdback_ms >= BUFFERED_MIN_SHARE * server_span_ms


def analyze_client_delivery(report, *, report_id: str, completion_id: str, request: Optional[Request] = None) -> bool:
    """Log a browser-measured delivery report. Returns True if it shows buffering.

    ``report`` is a ``StreamDeliveryReport``. Only buffered streams are worth
    a warning: they mean something between the browser and the app holds the
    response back, and the user sees the answer appear all at once.
    """
    buffered = is_buffered(report.holdback_ms, report.server_span_ms)
    if buffered:
        cause = (
            f"the response reached the browser compressed (Content-Encoding={report.content_encoding}); "
            "check for a reverse proxy compressing text/event-stream"
            if report.content_encoding and report.content_encoding.lower() not in ("identity", "")
            else "an intermediary (corporate proxy, TLS inspection, antivirus/DLP, load balancer) is buffering the response"
        )
        logger.warning(
            "sse.delivery_buffered kind=%s report=%s completion=%s holdback_ms=%d server_span_ms=%d "
            "token_events=%d network_reads=%d response_via=%s response_content_encoding=%s proxies=%s — "
            "live tokens were held back on the way to the browser and appeared all at once: %s",
            report.kind, report_id, completion_id, report.holdback_ms, report.server_span_ms,
            report.token_events, report.network_reads, report.via or "-",
            report.content_encoding or "-", _proxy_hints(request), cause,
        )
    return buffered
