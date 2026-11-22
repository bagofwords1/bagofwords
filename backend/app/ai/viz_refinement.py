"""Background visualization inference for ``create_data`` in the agent loop.

``create_data`` used to await a small LLM pass (the visualization inference)
between running the query and ending the tool, so the planner waited for it on
every chart. Inside the agent v2 loop the tool now ends with the deterministic
chart spec (``app/ai/tools/chart_spec.py``), flags its result
``viz_pending: True`` and hands the inference over as an ``asyncio.Task``. This
module owns that task until its result is in the database.

Two halves, on purpose:

* **announce** — as soon as the inference lands (or fails, or times out), the
  agent emits ``visualization.refined`` over SSE so a figure waiting on the
  pending flag draws the refined chart while the planner is still writing.
  SSE emission is in-memory (``CompletionEventQueue.put`` never blocks), so it
  is safe from a background task.
* **apply** — the database writes (step ``data_model``, visualization ``view``,
  tool execution ``result_json``) happen only in :meth:`settle`, which the
  agent calls from its own loop at safe points: before a tool batch that may
  read visualizations, and before ``completion.finished``. In single-writer
  mode (always on SQLite) the agent's only write session is the loop's own
  session, which a background task must never touch; settling from the loop
  keeps every write on the loop's coroutine in both modes.

Race rules this module guarantees (tests/unit/test_viz_refinement.py):

* every tracked refinement is announced exactly once and applied exactly once
  (a failed write is retried with backoff, up to MAX_APPLY_ATTEMPTS, and only
  a committed write counts as applied);
* a refinement still running when a settle with ``wait=True`` times out is
  cancelled and applied as "no refinement" (the pending flag is cleared, the
  deterministic spec stands) — nothing can rewrite a visualization after a
  later tool has read it;
* ``settle(wait=False)`` applies only what has already landed and never blocks;
* concurrent settles are serialized; ``cancel_all`` leaves no task behind.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


# Upper bound on one inference pass. The pass is a bounded classification call
# (a few seconds); past this it is treated as failed and the deterministic
# spec stands.
VIZ_REFINE_TIMEOUT_S = _env_float("BOW_VIZ_REFINE_TIMEOUT_S", 45.0)
# How long a settle waits for refinements still in flight: before a tool batch
# that may read visualizations, and before the run reports it has finished.
VIZ_SETTLE_TIMEOUT_S = _env_float("BOW_VIZ_SETTLE_TIMEOUT_S", 20.0)

# Data-model keys the refinement owns. They are replaced (or removed) as a
# set, so a breakdown the deterministic pass guessed doesn't survive an
# inference that dropped it.
REFINED_DATA_MODEL_KEYS = ("type", "series", "group_by", "sort", "limit", "filters", "display")
# Keys the tool's own result writes into the step (agent_v2._handle_tool_output):
# if the stored step no longer holds the provisional value for one of them,
# someone changed the chart after the tool ended.
PROVISIONAL_STEP_KEYS = ("series", "group_by", "sort", "limit", "filters")
# A write that fails (transient lock, a tool row not inserted yet) is retried
# with a short backoff, at most this many attempts in all; only a committed
# write counts as applied.
MAX_APPLY_ATTEMPTS = 3
APPLY_RETRY_BASE_DELAY_S = 0.25


@dataclass
class VizRefinement:
    """One deferred inference and where its result goes."""

    task: asyncio.Task[dict[str, Any] | None]
    tool_execution_id: str | None = None
    step_id: str | None = None
    visualization_id: str | None = None
    # The background write that INSERTs the tool execution row (Postgres
    # mode). The apply UPDATEs that row, so it must land first.
    persist_task: asyncio.Task | None = None
    # What the tool returned before the refinement: the apply overwrites a
    # stored chart only while it still matches these, so an edit the user
    # saved while the refinement was pending is never replaced.
    provisional_view: dict[str, Any] | None = None
    provisional_data_model: dict[str, Any] | None = None
    # Filled by the watcher: {"data_model", "view", ...} or None.
    result: dict[str, Any] | None = None
    watch: asyncio.Task | None = None
    announced: bool = False
    applied: bool = False
    # Write attempts so far (retried within a settle up to MAX_APPLY_ATTEMPTS).
    apply_attempts: int = 0

    @property
    def refined(self) -> bool:
        return bool(self.result and isinstance(self.result.get("data_model"), dict))


def merge_refined_data_model(existing: dict[str, Any] | None, refined: dict[str, Any]) -> dict[str, Any]:
    """The step's data_model with the refinement's keys swapped in.

    Keeps whatever else the step carries (columns streamed during the run);
    replaces the keys the refinement owns, removing those it left unset.
    """
    merged = dict(existing or {})
    for key in REFINED_DATA_MODEL_KEYS:
        value = refined.get(key)
        if value is None:
            if key != "type":
                merged.pop(key, None)
        else:
            merged[key] = value
    return merged


def stored_chart_still_provisional(
    *,
    stored_view: Any,
    provisional_view: Any,
    stored_data_model: Any,
    provisional_data_model: Any,
) -> bool:
    """Whether the stored chart is still what the tool returned.

    False once anything else wrote it after the tool ended (the user saved a
    new axis or chart type): the refinement must then leave it alone. Views
    are compared as stored (normalized by the caller); the step is compared
    on the keys the tool itself wrote into it.
    """
    if provisional_view is not None and stored_view is not None and stored_view != provisional_view:
        return False
    if isinstance(provisional_data_model, dict) and isinstance(stored_data_model, dict):
        for key in PROVISIONAL_STEP_KEYS:
            want = provisional_data_model.get(key)
            if want is not None and stored_data_model.get(key) != want:
                return False
    return True


def apply_refinement_to_result_json(result_json: Any, refinement: dict[str, Any] | None) -> Any:
    """The tool result with the pending flag cleared and, when there is a
    refinement, its data_model / view in place of the deterministic ones."""
    if not isinstance(result_json, dict):
        return result_json
    updated = dict(result_json)
    updated.pop("viz_pending", None)
    if refinement and isinstance(refinement.get("data_model"), dict):
        updated["data_model"] = refinement["data_model"]
        if refinement.get("view"):
            updated["view"] = refinement["view"]
    return updated


class VizRefinementTracker:
    """Tracks deferred inferences for one agent run."""

    def __init__(
        self,
        *,
        announce: Callable[[VizRefinement], Awaitable[None]],
        apply: Callable[[VizRefinement], Awaitable[None]],
        refine_timeout_s: float = VIZ_REFINE_TIMEOUT_S,
    ):
        self._announce = announce
        self._apply = apply
        self._refine_timeout_s = refine_timeout_s
        self._records: list[VizRefinement] = []
        self._settle_lock = asyncio.Lock()

    @property
    def pending(self) -> bool:
        """True while any refinement is not yet applied."""
        return any(not r.applied for r in self._records)

    @property
    def in_flight(self) -> int:
        return sum(1 for r in self._records if r.watch is not None and not r.watch.done())

    def track(self, record: VizRefinement) -> VizRefinement:
        record.watch = asyncio.create_task(self._watch(record), name="agent.viz_refine.watch")
        self._records.append(record)
        return record

    async def _watch(self, record: VizRefinement) -> None:
        try:
            record.result = await asyncio.wait_for(record.task, timeout=self._refine_timeout_s)
        except asyncio.CancelledError:
            # Cancelled by settle/cancel_all; they announce on our behalf.
            record.task.cancel()
            raise
        except TimeoutError:
            logger.warning(
                "viz_refine timed out after %.0fs (step=%s); keeping the deterministic spec",
                self._refine_timeout_s, record.step_id,
            )
            record.result = None
        except Exception:
            logger.warning("viz_refine failed (step=%s)", record.step_id, exc_info=True)
            record.result = None
        await self._announce_once(record)

    async def _announce_once(self, record: VizRefinement) -> None:
        if record.announced:
            return
        record.announced = True
        try:
            await self._announce(record)
        except Exception:
            logger.warning("viz_refine announce failed (step=%s)", record.step_id, exc_info=True)

    async def settle(self, *, wait: bool, timeout_s: float = VIZ_SETTLE_TIMEOUT_S) -> int:
        """Apply landed refinements; with ``wait``, first wait for the rest.

        With ``wait=True`` everything tracked is applied before this returns:
        what is still running after ``timeout_s`` is cancelled and applied as
        "no refinement". With ``wait=False`` only what already landed is
        applied, and nothing is awaited except the writes themselves.

        Returns the number of refinements applied.
        """
        async with self._settle_lock:
            records = [r for r in self._records if not r.applied]
            if not records:
                return 0
            if wait:
                running = [r.watch for r in records if r.watch is not None and not r.watch.done()]
                if running:
                    await asyncio.wait(running, timeout=max(0.0, timeout_s))
            applied = 0
            for record in records:
                watch_done = record.watch is None or record.watch.done()
                if not watch_done:
                    if not wait:
                        continue
                    logger.warning(
                        "viz_refine still running after %.1fs settle (step=%s); cancelling",
                        timeout_s, record.step_id,
                    )
                    record.watch.cancel()
                    record.task.cancel()
                    await asyncio.gather(record.watch, record.task, return_exceptions=True)
                    record.result = None
                    await self._announce_once(record)
                elif not record.announced:
                    # Watch ended without announcing (cancelled from outside).
                    await self._announce_once(record)
                # Applied only once the write commits. A failed write (a lock,
                # a tool row not inserted yet) is retried here with a short
                # backoff; after MAX_APPLY_ATTEMPTS nothing was committed and
                # the stored chart stays the provisional one, consistently.
                if await self._apply_with_retry(record):
                    applied += 1
                elif record.refined:
                    # The browser was told the refined chart; tell it the
                    # provisional one the database kept, so a reload agrees.
                    await self._revert_announcement(record)
                record.applied = True
            self._records = [r for r in self._records if not r.applied]
            return applied

    async def _revert_announcement(self, record: VizRefinement) -> None:
        if not isinstance(record.provisional_data_model, dict):
            return
        record.result = {"data_model": record.provisional_data_model, "view": record.provisional_view}
        try:
            await self._announce(record)
        except Exception:
            logger.warning("viz_refine revert announce failed (step=%s)", record.step_id, exc_info=True)

    async def _apply_with_retry(self, record: VizRefinement) -> bool:
        delay = APPLY_RETRY_BASE_DELAY_S
        while True:
            record.apply_attempts += 1
            try:
                await self._apply(record)
                return True
            except Exception:
                if record.apply_attempts >= MAX_APPLY_ATTEMPTS:
                    logger.error(
                        "viz_refine apply failed %d times (step=%s); keeping the provisional chart",
                        record.apply_attempts, record.step_id, exc_info=True,
                    )
                    return False
                logger.warning(
                    "viz_refine apply failed (attempt %d, step=%s); retrying in %.2fs",
                    record.apply_attempts, record.step_id, delay, exc_info=True,
                )
                await asyncio.sleep(delay)
                delay *= 2

    async def cancel_all(self) -> None:
        """Stop every refinement without applying it (run teardown)."""
        records, self._records = self._records, []
        tasks = []
        for record in records:
            for t in (record.watch, record.task):
                if t is not None and not t.done():
                    t.cancel()
                    tasks.append(t)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
