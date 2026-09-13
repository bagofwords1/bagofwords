"""Progress reporting plumbing for DataSourceClient.get_schemas.

Clients accept an optional `progress_callback`. When set, they invoke it from
inside their existing iteration loops — never from new round-trips. When unset
(the default), the callback is a no-op and behavior is unchanged.

The callback signature is:

    callback(phase: str | None, current_item: str | None, done: int, total: int) -> None | Awaitable[None]

Callbacks may be sync or coroutine functions; `ProgressReporter.emit` handles
both. Emissions are debounced at the source (per-call) — runners are expected
to rate-limit DB writes separately.
"""
from __future__ import annotations

import asyncio
import inspect
import time
from contextvars import ContextVar
from functools import wraps
from collections.abc import Sized
from typing import Any, Awaitable, Callable, Optional, Union


# A callback may be sync or async. Both return None in practice.
ProgressCallback = Callable[
    [Optional[str], Optional[str], int, int],
    Union[None, Awaitable[None]],
]

# A zero-arg predicate a client polls during long work (schema discovery, cache
# warming). Returns True once the caller wants the work aborted. Clients that
# accept it must check it at coarse checkpoints (per file, per chunk) and raise
# `IndexingCancelled` promptly so a stuck long-running convert can be stopped.
CancelCheck = Callable[[], bool]


class IndexingCancelled(Exception):
    """Raised from inside a client (schema discovery or `awarm_all`) when a
    cancel has been requested via a `CancelCheck`. The indexing runner catches
    it and marks the run `cancelled` rather than `failed`.
    """


class ProgressReporter:
    """Cheap no-op when no callback is set; else forwards emissions."""

    __slots__ = ("_cb", "_phase", "_done", "_total", "_last_emit", "_last_bucket")

    def __init__(self, callback: Optional[ProgressCallback] = None) -> None:
        self._cb = callback
        self._phase: Optional[str] = None
        self._done = 0
        self._total = 0
        self._last_emit = float("-inf")
        self._last_bucket = -1

    @property
    def enabled(self) -> bool:
        return self._cb is not None

    def phase(self, phase: str, total: int = 0) -> None:
        """Begin a new phase. Resets done counter; sets total if provided."""
        self._phase = phase
        self._done = 0
        self._total = total
        self._emit(None, force=True)

    def set_total(self, total: int) -> None:
        self._total = total
        self._emit(None, force=True)

    def item(self, current_item: Optional[str], done: Optional[int] = None) -> None:
        """Report progress on a single item within the current phase."""
        if done is not None:
            self._done = done
        else:
            self._done += 1
        self._emit(current_item)

    def tick(self, current_item: Optional[str] = None) -> None:
        """Alias for `item()` with auto-increment."""
        self.item(current_item)

    def done(self, total: Optional[int] = None) -> None:
        """Mark the current phase done. Sets done == total."""
        if total is not None:
            self._total = total
        self._done = self._total
        self._emit(None, force=True)

    def _emit(self, current_item: Optional[str], *, force: bool = False) -> None:
        if self._cb is None:
            return
        now = time.monotonic()
        bucket = int(100 * self._done / self._total) if self._total > 0 else -1
        # Keep small catalogs detailed. Large/unknown crawls emit percentage
        # milestones or at most ten updates/second, plus every stage boundary.
        if not force and max(self._total, self._done) > 1000 and bucket == self._last_bucket and now - self._last_emit < 0.1:
            return
        self._last_emit = now
        self._last_bucket = bucket
        try:
            result = self._cb(self._phase, current_item, self._done, self._total)
            if inspect.isawaitable(result):
                # Best-effort fire-and-forget when called from sync code. If the
                # loop isn't running, run it to completion synchronously.
                try:
                    loop = asyncio.get_running_loop()
                    loop.create_task(result)  # type: ignore[arg-type]
                except RuntimeError:
                    asyncio.run(result)  # type: ignore[arg-type]
        except IndexingCancelled:
            # Cancellation is control flow, not a reporting error — let it
            # unwind the client's discovery/warm loop so the run stops promptly.
            raise
        except Exception:
            # Progress reporting must never break schema discovery.
            pass


def noop_reporter() -> ProgressReporter:
    return ProgressReporter(None)


def make_reporter(callback: Optional[ProgressCallback]) -> ProgressReporter:
    return ProgressReporter(callback)

# Discovery helpers share a reporter within one synchronous crawl. ContextVar
# keeps concurrent connections isolated and avoids mutable client-level state.

_discovery_reporter: ContextVar[Optional[ProgressReporter]] = ContextVar('discovery_reporter', default=None)


def discovery_progress(fn):
    """Give a discovery entry point a start/end lifecycle without extra I/O.

    The decorated method explicitly declares progress_callback. Helpers called
    by that method can report existing work through discovery_items/phase.
    Executor tasks should report completion from their submitting thread.
    """
    signature = inspect.signature(fn)
    @wraps(fn)
    def run(*args, **kwargs):
        callback = signature.bind_partial(*args, **kwargs).arguments.get('progress_callback')
        if callback is None:
            return fn(*args, **kwargs)
        reporter = make_reporter(callback)
        token = _discovery_reporter.set(reporter)
        try:
            reporter.phase('discovering_schema')
            result = fn(*args, **kwargs)
            reporter.phase('catalog_ready', total=len(result) if isinstance(result, Sized) else 0)
            reporter.done()
            return result
        finally:
            _discovery_reporter.reset(token)
    return run


def discovery_phase(phase: str) -> None:
    reporter = _discovery_reporter.get()
    if reporter is not None:
        reporter.phase(phase)


def discovery_items(items, phase: str, *, label=None, total=None):
    """Report one existing iterable without materializing it or extra queries.

    Counts are work items within this stage, not a global catalog percentage.
    Labels must be metadata identifiers, never rows of customer data or secrets.
    Unknown totals remain indeterminate until the iterable is exhausted.
    """
    reporter = _discovery_reporter.get()
    if reporter is None:
        return items

    def iterate():
        count = len(items) if total is None and isinstance(items, Sized) else (total or 0)
        reporter.phase(phase, total=count)
        done = 0
        for item in items:
            # Nested stages can change the shared reporter; restore this stage
            # before reporting its next work item.
            if reporter._phase != phase:
                reporter.phase(phase, total=count)
            try:
                name = label(item) if label is not None else None
            except Exception:
                name = None
            reporter.item(name, done=done)
            yield item
            done += 1
        if reporter._phase != phase:
            reporter.phase(phase, total=count)
        reporter.done(total=done)
    return iterate()


def discovery_summary(phase: str, count: int) -> None:
    """Report an observed outcome after that work has finished."""
    reporter = _discovery_reporter.get()
    if reporter is not None:
        reporter.phase(phase, total=count)
        reporter.done()
