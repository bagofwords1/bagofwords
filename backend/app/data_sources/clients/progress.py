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

    __slots__ = ("_cb", "_phase", "_done", "_total")

    def __init__(self, callback: Optional[ProgressCallback] = None) -> None:
        self._cb = callback
        self._phase: Optional[str] = None
        self._done = 0
        self._total = 0

    @property
    def enabled(self) -> bool:
        return self._cb is not None

    def phase(self, phase: str, total: int = 0) -> None:
        """Begin a new phase. Resets done counter; sets total if provided."""
        self._phase = phase
        self._done = 0
        self._total = total
        self._emit(None)

    def set_total(self, total: int) -> None:
        self._total = total
        self._emit(None)

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
        self._emit(None)

    def _emit(self, current_item: Optional[str]) -> None:
        if self._cb is None:
            return
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
