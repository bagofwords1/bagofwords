import asyncio
import time
from typing import Awaitable, Callable, Optional

from app.schemas.sse_schema import SSEEvent


class PlanningTextStreamer:
    """Throttled hybrid text streamer for planning blocks.

    - Emits small token deltas for typing effect (block.delta.token)
    - Periodically emits snapshots for robustness (block.delta.text)
    - Sends completion markers when finished (block.delta.text.complete)

    Streaming is optimized for smoothness:
    - Low time threshold (16ms = ~60fps) for responsive feel
    - Character threshold (5 chars) to emit on small batches regardless of time
    - Large chunk splitting for models that return text in bursts (e.g., GPT-5, o1)
    """

    def __init__(
        self,
        emit: Callable[[SSEEvent], Awaitable[None]],
        seq_fn: Callable[[], Awaitable[int]],
        completion_id: str,
        agent_execution_id: str,
        block_id: Optional[str],
        throttle_ms: int = 16,  # ~60fps for smooth streaming
        snapshot_every_ms: int = 1200,
        char_threshold: int = 30,  # Emit after N chars - batched for performance
        split_large_chunks: bool = False,  # Disabled - reduces event count significantly
        max_chunk_size: int = 100,  # Larger chunks when splitting is enabled
        split_delay_ms: int = 8,  # Delay between split emissions
    ):
        self.emit = emit
        self.seq_fn = seq_fn
        self.completion_id = completion_id
        self.agent_execution_id = agent_execution_id
        self.block_id = block_id

        self.prev_reasoning = ""
        self.prev_content = ""
        self.last_emit = {"reasoning": 0.0, "content": 0.0}
        self.last_snapshot = 0.0
        self.throttle_ms = throttle_ms
        self.snapshot_every_ms = snapshot_every_ms
        self.char_threshold = char_threshold
        self.split_large_chunks = split_large_chunks
        self.max_chunk_size = max_chunk_size
        self.split_delay_ms = split_delay_ms

    def set_block(self, block_id: str):
        self.block_id = block_id

    def _now_ms(self) -> float:
        return time.time() * 1000.0

    @staticmethod
    def _delta(prev: str, new: str) -> str:
        # Compute delta via common prefix
        i = 0
        limit = min(len(prev), len(new))
        while i < limit and prev[i] == new[i]:
            i += 1
        return new[i:]

    async def _emit_field_delta(self, field: str, delta: str):
        """Emit a single token delta for a field."""
        seq = await self.seq_fn()
        await self.emit(SSEEvent(
            event="block.delta.token",
            completion_id=self.completion_id,
            agent_execution_id=self.agent_execution_id,
            seq=seq,
            data={
                "block_id": self.block_id,
                "field": field,
                "token": delta,
            }
        ))

    async def _emit_chunked(self, field: str, delta: str):
        """Emit a delta, splitting into smaller chunks if needed for typing effect."""
        if not self.split_large_chunks or len(delta) <= self.max_chunk_size:
            # Small enough, emit directly
            await self._emit_field_delta(field, delta)
        else:
            # Split large chunk into smaller pieces with delays
            pos = 0
            while pos < len(delta):
                chunk = delta[pos:pos + self.max_chunk_size]
                await self._emit_field_delta(field, chunk)
                pos += self.max_chunk_size
                # Small delay between chunks for typing effect (skip delay on last chunk)
                if pos < len(delta):
                    await asyncio.sleep(self.split_delay_ms / 1000.0)

    async def update(self, reasoning: Optional[str], content: Optional[str]):
        if not self.block_id:
            return

        reasoning = reasoning or ""
        content = content or ""
        now = self._now_ms()

        # Emit reasoning delta - time OR char threshold triggers emission
        if reasoning != self.prev_reasoning:
            # Calculate pending chars as difference from last emitted state
            pending_chars = len(reasoning) - len(self.prev_reasoning)
            time_ready = (now - self.last_emit["reasoning"]) >= self.throttle_ms
            char_ready = pending_chars >= self.char_threshold

            if time_ready or char_ready:
                rdelta = self._delta(self.prev_reasoning, reasoning)
                if rdelta:
                    await self._emit_chunked("reasoning", rdelta)
                    self.prev_reasoning = reasoning
                    self.last_emit["reasoning"] = self._now_ms()

        # Emit content delta - time OR char threshold triggers emission
        if content != self.prev_content:
            # Calculate pending chars as difference from last emitted state
            pending_chars = len(content) - len(self.prev_content)
            time_ready = (now - self.last_emit["content"]) >= self.throttle_ms
            char_ready = pending_chars >= self.char_threshold

            if time_ready or char_ready:
                cdelta = self._delta(self.prev_content, content)
                if cdelta:
                    await self._emit_chunked("content", cdelta)
                    self.prev_content = content
                    self.last_emit["content"] = self._now_ms()

        # Periodic full snapshot for robustness
        if (now - self.last_snapshot) >= self.snapshot_every_ms:
            self.last_snapshot = now
            if self.prev_reasoning:
                seq = await self.seq_fn()
                await self.emit(SSEEvent(
                    event="block.delta.text",
                    completion_id=self.completion_id,
                    agent_execution_id=self.agent_execution_id,
                    seq=seq,
                    data={
                        "block_id": self.block_id,
                        "field": "reasoning",
                        "text": self.prev_reasoning,
                    }
                ))
            if self.prev_content:
                seq = await self.seq_fn()
                await self.emit(SSEEvent(
                    event="block.delta.text",
                    completion_id=self.completion_id,
                    agent_execution_id=self.agent_execution_id,
                    seq=seq,
                    data={
                        "block_id": self.block_id,
                        "field": "content",
                        "text": self.prev_content,
                    }
                ))

    async def complete(self):
        if not self.block_id:
            return
        # Final snapshots
        if self.prev_reasoning:
            seq = await self.seq_fn()
            await self.emit(SSEEvent(
                event="block.delta.text",
                completion_id=self.completion_id,
                agent_execution_id=self.agent_execution_id,
                seq=seq,
                data={
                    "block_id": self.block_id,
                    "field": "reasoning",
                    "text": self.prev_reasoning,
                }
            ))
        if self.prev_content:
            seq = await self.seq_fn()
            await self.emit(SSEEvent(
                event="block.delta.text",
                completion_id=self.completion_id,
                agent_execution_id=self.agent_execution_id,
                seq=seq,
                data={
                    "block_id": self.block_id,
                    "field": "content",
                    "text": self.prev_content,
                }
            ))
        # Completion markers
        for field in ("reasoning", "content"):
            seq = await self.seq_fn()
            await self.emit(SSEEvent(
                event="block.delta.text.complete",
                completion_id=self.completion_id,
                agent_execution_id=self.agent_execution_id,
                seq=seq,
                data={
                    "block_id": self.block_id,
                    "field": field,
                    "is_final": True,
                }
            ))
