import asyncio
import logging
from typing import AsyncIterator, Optional
from app.schemas.sse_schema import SSEEvent

_SENTINEL = object()
_QUEUE_MAXSIZE = 512
_logger = logging.getLogger(__name__)


class CompletionEventQueue:
    """Queue for streaming SSE events during completion.

    Bounded to _QUEUE_MAXSIZE so a slow consumer (network, slow client) cannot
    cause unbounded memory growth.  When the queue is full, put() drops the
    incoming event and logs a warning rather than blocking the agent loop.
    """

    def __init__(self):
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=_QUEUE_MAXSIZE)
        self._dropped: int = 0

    async def put(self, event: SSEEvent):
        """Add validated Pydantic event to queue.

        Falls back to a non-blocking put and drops the event on overflow so the
        agent is never stalled waiting for a slow consumer.
        """
        try:
            self.queue.put_nowait(event)
        except asyncio.QueueFull:
            self._dropped += 1
            _logger.warning(
                f"[sse_queue] Queue full ({_QUEUE_MAXSIZE}), dropping event "
                f"type={getattr(event, 'event', '?')} (total dropped: {self._dropped})"
            )

    async def get_events(self) -> AsyncIterator[SSEEvent]:
        """Yield validated Pydantic events."""
        while True:
            event = await self.queue.get()
            if event is _SENTINEL:
                break
            yield event

    def finish(self):
        """Signal that no more events will be added."""
        try:
            self.queue.put_nowait(_SENTINEL)
        except asyncio.QueueFull:
            # Queue is full — drain one item to make room for the sentinel
            # so get_events() can break out of its loop.
            try:
                self.queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            try:
                self.queue.put_nowait(_SENTINEL)
            except asyncio.QueueFull:
                _logger.error("[sse_queue] Unable to enqueue sentinel after drain; stream may hang")
