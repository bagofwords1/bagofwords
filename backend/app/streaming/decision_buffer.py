"""Debounced decision persistence for streaming performance.

This module provides a buffer that accumulates decision updates in memory
and flushes them to the database periodically, rather than on every token.
This dramatically reduces DB load during streaming while maintaining data
integrity on completion or stop.
"""

import asyncio
from datetime import datetime
from typing import Optional, Callable, Awaitable, Any


class DecisionBuffer:
    """Buffers decision updates and flushes to DB periodically.
    
    - SSE events stream to client immediately (no delay)
    - DB writes are batched every `flush_interval_ms`
    - Always flushes on completion, stop, or explicit request
    
    Usage:
        buffer = DecisionBuffer(flush_interval_ms=500)
        
        # During streaming loop:
        buffer.set_pending(decision_model, plan_decision_orm)
        buffer.mark_dirty()  # Starts background flush timer
        
        # At end of streaming:
        await buffer.flush_now(db, save_fn, upsert_fn)
    """
    
    def __init__(self, flush_interval_ms: int = 500):
        self.flush_interval_ms = flush_interval_ms
        self._dirty = False
        self._flush_task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()
        self._stopped = False
        
        # Pending state to flush
        self._pending_decision_model: Any = None
        self._pending_plan_decision: Any = None
        self._pending_block: Any = None
        
        # Callbacks for persistence (set via set_callbacks)
        self._save_decision_fn: Optional[Callable[[], Awaitable[Any]]] = None
        self._upsert_block_fn: Optional[Callable[[Any], Awaitable[Any]]] = None
        self._rebuild_fn: Optional[Callable[[], Awaitable[None]]] = None
    
    def set_callbacks(
        self,
        save_decision_fn: Callable[[], Awaitable[Any]],
        upsert_block_fn: Callable[[Any], Awaitable[Any]],
        rebuild_fn: Callable[[], Awaitable[None]],
    ):
        """Set the persistence callbacks."""
        self._save_decision_fn = save_decision_fn
        self._upsert_block_fn = upsert_block_fn
        self._rebuild_fn = rebuild_fn
    
    def set_pending(self, decision_model: Any, plan_decision: Any = None):
        """Store the latest decision state to be flushed."""
        self._pending_decision_model = decision_model
        if plan_decision is not None:
            self._pending_plan_decision = plan_decision
    
    def get_pending_decision(self) -> Any:
        """Get the pending plan decision ORM object."""
        return self._pending_plan_decision
    
    def mark_dirty(self):
        """Mark that there's pending data to flush."""
        self._dirty = True
        # Start background flush task if not running
        if self._flush_task is None or self._flush_task.done():
            self._flush_task = asyncio.create_task(self._flush_loop())
    
    async def _flush_loop(self):
        """Background task that flushes periodically."""
        while not self._stopped and self._dirty:
            await asyncio.sleep(self.flush_interval_ms / 1000.0)
            if self._dirty and not self._stopped:
                await self._do_flush()
    
    async def _do_flush(self):
        """Execute the flush if dirty."""
        async with self._lock:
            if not self._dirty:
                return
            
            try:
                # Save decision to DB
                if self._save_decision_fn and self._pending_decision_model:
                    self._pending_plan_decision = await self._save_decision_fn()
                
                # Upsert block
                if self._upsert_block_fn and self._pending_plan_decision:
                    self._pending_block = await self._upsert_block_fn(self._pending_plan_decision)
                
                self._dirty = False
            except Exception:
                # Best-effort persistence - don't crash streaming
                pass
    
    async def flush_now(self):
        """Force an immediate flush (call on completion/stop)."""
        self._stopped = True
        
        # Cancel background task
        if self._flush_task and not self._flush_task.done():
            self._flush_task.cancel()
            try:
                await self._flush_task
            except asyncio.CancelledError:
                pass
        
        # Final flush
        await self._do_flush()
        
        # Rebuild completion if needed
        if self._rebuild_fn:
            try:
                await self._rebuild_fn()
            except Exception:
                pass
    
    async def stop(self):
        """Stop the buffer and flush any pending data."""
        await self.flush_now()
    
    def get_pending_block(self) -> Any:
        """Get the last flushed block (for block ID tracking)."""
        return self._pending_block
    
    def reset(self):
        """Reset buffer state for a new planning loop."""
        self._dirty = False
        self._stopped = False
        self._pending_decision_model = None
        self._pending_plan_decision = None
        self._pending_block = None
        self._save_decision_fn = None
        self._upsert_block_fn = None
        self._rebuild_fn = None

