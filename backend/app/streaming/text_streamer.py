import time
from typing import Awaitable, Callable, Optional

from app.schemas.sse_schema import SSEEvent


class PlanningTextStreamer:
    """Throttled hybrid text streamer for planning blocks.

    - Emits small token deltas for typing effect (block.delta.token)
    - Periodically emits snapshots for robustness (block.delta.text)
    - Sends completion markers when finished (block.delta.text.complete)
    """

    def __init__(
        self,
        emit: Callable[[SSEEvent], Awaitable[None]],
        seq_fn: Callable[[], Awaitable[int]],
        completion_id: str,
        agent_execution_id: str,
        block_id: Optional[str],
        throttle_ms: int = 60,
        snapshot_every_ms: int = 1200,
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

    async def update(self, reasoning: Optional[str], content: Optional[str]):
        if not self.block_id:
            return

        reasoning = reasoning or ""
        content = content or ""
        now = self._now_ms()

        # Emit reasoning delta
        if reasoning != self.prev_reasoning:
            if (now - self.last_emit["reasoning"]) >= self.throttle_ms:
                rdelta = self._delta(self.prev_reasoning, reasoning)
                if rdelta:
                    seq = await self.seq_fn()
                    await self.emit(SSEEvent(
                        event="block.delta.token",
                        completion_id=self.completion_id,
                        agent_execution_id=self.agent_execution_id,
                        seq=seq,
                        data={
                            "block_id": self.block_id,
                            "field": "reasoning",
                            "token": rdelta,
                        }
                    ))
                    self.prev_reasoning = reasoning
                    self.last_emit["reasoning"] = now

        # Emit content delta
        if content != self.prev_content:
            if (now - self.last_emit["content"]) >= self.throttle_ms:
                cdelta = self._delta(self.prev_content, content)
                if cdelta:
                    seq = await self.seq_fn()
                    await self.emit(SSEEvent(
                        event="block.delta.token",
                        completion_id=self.completion_id,
                        agent_execution_id=self.agent_execution_id,
                        seq=seq,
                        data={
                            "block_id": self.block_id,
                            "field": "content",
                            "token": cdelta,
                        }
                    ))
                    self.prev_content = content
                    self.last_emit["content"] = now

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


