import asyncio
from typing import AsyncIterator, Optional
from app.schemas.sse_schema import SSEEvent

_SENTINEL = object()


class CompletionEventQueue:
    """Queue for streaming SSE events during completion."""

    def __init__(self):
        self.queue: asyncio.Queue = asyncio.Queue()

    async def put(self, event: SSEEvent):
        """Add validated Pydantic event to queue."""
        await self.queue.put(event)

    async def get_events(self) -> AsyncIterator[SSEEvent]:
        """Yield validated Pydantic events."""
        while True:
            event = await self.queue.get()
            if event is _SENTINEL:
                break
            yield event

    def finish(self):
        """Signal that no more events will be added."""
        self.queue.put_nowait(_SENTINEL)
