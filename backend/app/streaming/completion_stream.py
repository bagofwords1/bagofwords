import asyncio
import logging
from typing import AsyncIterator, Optional
from app.schemas.sse_schema import SSEEvent

_SENTINEL = object()

# Marker objects returned by next_event() so stream generators can distinguish
# "no event within the timeout" (emit an SSE heartbeat comment) from
# "the producer finished" (close the stream).
HEARTBEAT = object()
STREAM_DONE = object()

_QUEUE_MAXSIZE = 512
_logger = logging.getLogger(__name__)


class CompletionEventQueue:
    """Fan-out broadcaster for streaming SSE events during a completion.

    The agent produces events with put(); any number of consumers each read
    from their own bounded queue:

    - The kickoff request's StreamingResponse consumes the primary queue via
      get_events() (created eagerly so events emitted before the response
      generator starts are never lost).
    - Reconnecting watchers (page refresh, network blip, second tab) call
      subscribe() and receive events from that point on; they recover earlier
      state from the DB snapshot, which every event-persisting write precedes.

    Every queue is bounded to _QUEUE_MAXSIZE so a slow or dead consumer cannot
    cause unbounded memory growth: when a subscriber's queue is full, put()
    drops the event for that subscriber and logs a warning rather than
    blocking the agent loop. Dropped block-level events are recovered by the
    1.2s full-text snapshots and idempotent block.upsert payloads.
    """

    def __init__(self):
        self._queues: list[asyncio.Queue] = []
        self._finished = False
        self._dropped: int = 0
        self._primary: asyncio.Queue = self._add_queue()

    def _add_queue(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=_QUEUE_MAXSIZE)
        self._queues.append(q)
        return q

    @property
    def finished(self) -> bool:
        return self._finished

    @property
    def primary(self) -> asyncio.Queue:
        return self._primary

    async def put(self, event: SSEEvent):
        """Broadcast a validated Pydantic event to every consumer queue.

        Non-blocking: drops the event for any full queue so the agent is never
        stalled waiting for a slow consumer.
        """
        for q in list(self._queues):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                self._dropped += 1
                _logger.warning(
                    f"[sse_queue] Queue full ({_QUEUE_MAXSIZE}), dropping event "
                    f"type={getattr(event, 'event', '?')} (total dropped: {self._dropped})"
                )

    def subscribe(self) -> asyncio.Queue:
        """Attach a new consumer; receives events from this moment on.

        If the producer already finished, the queue is pre-loaded with the
        sentinel so the consumer terminates immediately.
        """
        q = self._add_queue()
        if self._finished:
            q.put_nowait(_SENTINEL)
        return q

    def unsubscribe(self, q: asyncio.Queue):
        try:
            self._queues.remove(q)
        except ValueError:
            pass

    async def get_events(self) -> AsyncIterator[SSEEvent]:
        """Yield validated Pydantic events from the primary (kickoff) queue."""
        while True:
            event = await self._primary.get()
            if event is _SENTINEL:
                break
            yield event

    @staticmethod
    async def next_event(q: asyncio.Queue, timeout: Optional[float] = None):
        """Await the next event on a consumer queue.

        Returns HEARTBEAT on timeout, STREAM_DONE when the producer finished,
        or the SSEEvent itself.
        """
        try:
            if timeout is None:
                event = await q.get()
            else:
                event = await asyncio.wait_for(q.get(), timeout)
        except asyncio.TimeoutError:
            return HEARTBEAT
        return STREAM_DONE if event is _SENTINEL else event

    def finish(self):
        """Signal that no more events will be added (to all consumers)."""
        self._finished = True
        for q in list(self._queues):
            try:
                q.put_nowait(_SENTINEL)
            except asyncio.QueueFull:
                # Queue is full — drain one item to make room for the sentinel
                # so consumers can break out of their loops.
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                try:
                    q.put_nowait(_SENTINEL)
                except asyncio.QueueFull:
                    _logger.error("[sse_queue] Unable to enqueue sentinel after drain; stream may hang")


# ---------------------------------------------------------------------------
# Registry of live streams, keyed by system completion id.
#
# Registered by create_completion_stream when the agent task starts, removed
# when it finishes. Lets a reconnecting client (page refresh, dropped
# connection, second tab) re-attach to the live event stream with
# GET /reports/{report_id}/completions/{completion_id}/stream.
#
# In-process only: under multiple uvicorn workers a reconnect may land on a
# worker that doesn't own the run, in which case the watch endpoint falls back
# to tailing the DB (blocks are persisted incrementally), degrading only the
# token-level typing granularity.
# ---------------------------------------------------------------------------

_ACTIVE_STREAMS: dict[str, CompletionEventQueue] = {}


def register_stream(completion_id: str, queue: CompletionEventQueue) -> None:
    _ACTIVE_STREAMS[str(completion_id)] = queue


def unregister_stream(completion_id: str) -> None:
    _ACTIVE_STREAMS.pop(str(completion_id), None)


def get_active_stream(completion_id: str) -> Optional[CompletionEventQueue]:
    return _ACTIVE_STREAMS.get(str(completion_id))
