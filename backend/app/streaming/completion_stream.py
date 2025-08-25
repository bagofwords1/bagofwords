import asyncio
from typing import AsyncIterator
from app.schemas.sse_schema import SSEEvent


class CompletionEventQueue:
    """Queue for streaming SSE events during completion."""
    
    def __init__(self):
        self.queue: asyncio.Queue[SSEEvent] = asyncio.Queue()
        self.finished = False
    
    async def put(self, event: SSEEvent):
        """Add validated Pydantic event to queue."""
        await self.queue.put(event)
    
    async def get_events(self) -> AsyncIterator[SSEEvent]:
        """Yield validated Pydantic events."""
        while not self.finished or not self.queue.empty():
            try:
                event = await asyncio.wait_for(self.queue.get(), timeout=0.1)
                yield event
            except asyncio.TimeoutError:
                continue
    
    def finish(self):
        """Mark the queue as finished (no more events will be added)."""
        self.finished = True
