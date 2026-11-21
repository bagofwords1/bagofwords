"""Background visualization inference — the tracker's ordering guarantees.

``create_data`` hands its visualization inference to the agent as a task
(app/ai/viz_refinement.py). These tests pin the contract the agent relies on,
independent of any LLM or database:

* every refinement is announced once and applied once, whatever the timing;
* ``settle(wait=True)`` returns only when everything tracked is applied —
  a pass still running at the timeout is cancelled and applied as
  "no refinement", so nothing can change a chart after a later tool read it;
* ``settle(wait=False)`` never blocks on a running pass;
* failures, timeouts and cancellations clear the pending state instead of
  leaving it set, and teardown leaves no task running.
"""

import asyncio

import pytest

from app.ai.viz_refinement import (
    VizRefinement,
    VizRefinementTracker,
    apply_refinement_to_result_json,
    merge_refined_data_model,
)

REFINED = {"data_model": {"type": "bar_chart", "series": [{"key": "country", "value": "revenue"}]},
           "view": {"version": "v2", "view": {"type": "bar_chart"}}}


class Recorder:
    def __init__(self):
        self.announced: list = []
        self.applied: list = []
        self.order: list = []

    async def announce(self, rec):
        self.announced.append((rec.step_id, rec.refined))
        self.order.append(("announce", rec.step_id))

    async def apply(self, rec):
        self.applied.append((rec.step_id, rec.refined))
        self.order.append(("apply", rec.step_id))


async def _after(delay, value=None, exc=None):
    await asyncio.sleep(delay)
    if exc:
        raise exc
    return value


def _tracker(rec, **kw):
    return VizRefinementTracker(announce=rec.announce, apply=rec.apply, **kw)


def _track(tracker, coro, step_id):
    return tracker.track(VizRefinement(task=asyncio.ensure_future(coro), step_id=step_id))


@pytest.mark.asyncio
async def test_landed_refinement_is_announced_before_settle_and_applied_once():
    rec = Recorder()
    tracker = _tracker(rec)
    _track(tracker, _after(0.0, REFINED), "s1")
    await asyncio.sleep(0.01)
    # Announced by the watcher on its own, before any settle.
    assert rec.announced == [("s1", True)]
    assert rec.applied == []
    assert await tracker.settle(wait=False) == 1
    assert await tracker.settle(wait=True) == 0
    assert rec.applied == [("s1", True)]
    assert rec.announced == [("s1", True)]
    assert not tracker.pending


@pytest.mark.asyncio
async def test_settle_without_wait_never_blocks_on_a_running_pass():
    rec = Recorder()
    tracker = _tracker(rec)
    _track(tracker, _after(5.0, REFINED), "slow")
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    assert await tracker.settle(wait=False) == 0
    assert loop.time() - t0 < 0.5
    assert tracker.pending and rec.applied == []
    await tracker.cancel_all()


@pytest.mark.asyncio
async def test_settle_with_wait_waits_for_a_pass_that_finishes_in_time():
    rec = Recorder()
    tracker = _tracker(rec)
    _track(tracker, _after(0.05, REFINED), "s1")
    assert await tracker.settle(wait=True, timeout_s=2.0) == 1
    assert rec.applied == [("s1", True)]
    # Announce strictly precedes apply for the same refinement.
    assert rec.order == [("announce", "s1"), ("apply", "s1")]


@pytest.mark.asyncio
async def test_settle_timeout_cancels_and_applies_as_unrefined():
    rec = Recorder()
    tracker = _tracker(rec)
    record = _track(tracker, _after(10.0, REFINED), "stuck")
    assert await tracker.settle(wait=True, timeout_s=0.05) == 1
    # Applied with no refinement: the pending flag gets cleared, the
    # deterministic chart stands, and the inference can no longer land.
    assert rec.applied == [("stuck", False)]
    assert rec.announced == [("stuck", False)]
    assert record.task.cancelled()
    assert not tracker.pending
    await asyncio.sleep(0.01)
    assert rec.announced == [("stuck", False)]  # never re-announced


@pytest.mark.asyncio
async def test_zero_timeout_is_a_stop_that_still_clears_pending():
    rec = Recorder()
    tracker = _tracker(rec)
    _track(tracker, _after(0.0, REFINED), "done")
    await asyncio.sleep(0.01)
    _track(tracker, _after(10.0, REFINED), "running")
    assert await tracker.settle(wait=True, timeout_s=0.0) == 2
    assert sorted(rec.applied) == [("done", True), ("running", False)]


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", [
    {"value": None},                         # inference produced nothing usable
    {"exc": RuntimeError("provider down")},  # inference raised
])
async def test_failed_pass_is_applied_as_unrefined(outcome):
    rec = Recorder()
    tracker = _tracker(rec)
    _track(tracker, _after(0.0, **outcome), "s1")
    await tracker.settle(wait=True, timeout_s=1.0)
    assert rec.announced == [("s1", False)]
    assert rec.applied == [("s1", False)]


@pytest.mark.asyncio
async def test_pass_exceeding_its_own_timeout_is_unrefined():
    rec = Recorder()
    tracker = _tracker(rec, refine_timeout_s=0.05)
    record = _track(tracker, _after(10.0, REFINED), "s1")
    await asyncio.sleep(0.2)
    assert rec.announced == [("s1", False)]
    assert record.task.cancelled()
    await tracker.settle(wait=False)
    assert rec.applied == [("s1", False)]


@pytest.mark.asyncio
async def test_concurrent_settles_apply_each_refinement_exactly_once():
    rec = Recorder()
    tracker = _tracker(rec)
    for i in range(5):
        _track(tracker, _after(0.01 * i, REFINED), f"s{i}")
    await asyncio.gather(*(tracker.settle(wait=True, timeout_s=2.0) for _ in range(4)))
    assert sorted(s for s, _ in rec.applied) == [f"s{i}" for i in range(5)]
    assert len(rec.announced) == 5


@pytest.mark.asyncio
async def test_refinements_tracked_during_a_settle_are_not_lost():
    rec = Recorder()
    tracker = _tracker(rec)
    _track(tracker, _after(0.05, REFINED), "first")
    settling = asyncio.create_task(tracker.settle(wait=True, timeout_s=2.0))
    await asyncio.sleep(0.01)
    _track(tracker, _after(0.0, REFINED), "late")
    await settling
    await tracker.settle(wait=True, timeout_s=2.0)
    assert sorted(s for s, _ in rec.applied) == ["first", "late"]


@pytest.mark.asyncio
async def test_apply_failure_does_not_block_other_refinements():
    rec = Recorder()

    async def flaky_apply(r):
        if r.step_id == "bad":
            raise RuntimeError("db locked")
        await rec.apply(r)

    tracker = VizRefinementTracker(announce=rec.announce, apply=flaky_apply)
    _track(tracker, _after(0.0, REFINED), "bad")
    _track(tracker, _after(0.0, REFINED), "good")
    assert await tracker.settle(wait=True, timeout_s=1.0) == 2
    assert rec.applied == [("good", True)]
    assert not tracker.pending  # a failed write is not retried forever


@pytest.mark.asyncio
async def test_announce_failure_still_applies():
    rec = Recorder()

    async def broken_announce(r):
        raise RuntimeError("queue gone")

    tracker = VizRefinementTracker(announce=broken_announce, apply=rec.apply)
    _track(tracker, _after(0.0, REFINED), "s1")
    await tracker.settle(wait=True, timeout_s=1.0)
    assert rec.applied == [("s1", True)]


@pytest.mark.asyncio
async def test_cancel_all_leaves_no_running_task():
    rec = Recorder()
    tracker = _tracker(rec)
    records = [_track(tracker, _after(10.0, REFINED), f"s{i}") for i in range(3)]
    await tracker.cancel_all()
    assert all(r.task.done() and r.watch.done() for r in records)
    assert not tracker.pending
    assert rec.applied == []


def test_merge_replaces_the_refined_keys_as_a_set():
    existing = {"type": "bar_chart", "columns": [{"field": "country"}],
                "group_by": "year", "series": [{"key": "country", "value": "n"}]}
    refined = {"type": "bar_chart", "series": [{"key": "country", "value": "revenue"}], "group_by": None}
    merged = merge_refined_data_model(existing, refined)
    assert merged["series"] == [{"key": "country", "value": "revenue"}]
    assert "group_by" not in merged            # dropped by the refinement
    assert merged["columns"] == [{"field": "country"}]  # untouched
    # A refinement may change the type (a card the deterministic pass demoted).
    assert merge_refined_data_model({"type": "table"}, {"type": "metric_card"})["type"] == "metric_card"
    # ...but never blank it.
    assert merge_refined_data_model({"type": "table"}, {})["type"] == "table"


def test_result_json_always_loses_the_pending_flag():
    rj = {"viz_pending": True, "data_model": {"type": "bar_chart"}, "view": {"v": 1}, "code": "x"}
    unrefined = apply_refinement_to_result_json(rj, None)
    assert "viz_pending" not in unrefined and unrefined["data_model"] == {"type": "bar_chart"}
    refined = apply_refinement_to_result_json(rj, REFINED)
    assert "viz_pending" not in refined
    assert refined["data_model"] == REFINED["data_model"] and refined["view"] == REFINED["view"]
    assert refined["code"] == "x"
    assert rj["viz_pending"] is True  # input not mutated
