"""Regression: context builders must not collide on the hub's shared session.

Production failure class (reproduced against Postgres in a live sandbox):
``ContextHub.refresh_warm`` and ``prime_static`` ran their builders under
``asyncio.gather(..., return_exceptions=True)``. Every builder is constructed
with the *same* ``AsyncSession``, which is not safe for concurrent use, so the
overlapping calls raised ``This session is provisioning a new connection;
concurrent operations are not permitted``.

The window is widest when the session holds no connection yet — exactly the
state ``agent_v2._release_db_between_steps()`` leaves it in before each loop
iteration's refresh. Measured: every one of 20 refreshes that followed a commit
lost at least one section.

The damage was silent. Each builder catches its own failure and returns an empty
section, so the planner was handed a ``<queries>`` section with no items — the
agent's record of what it had already queried — as though the report genuinely
had none.

Guarding only the builder list was not enough, which is what these tests now
pin. Both public methods touch the session *outside* their builder list
(``_instruction_query()``, the org-settings read, the scheduled-tasks read), and
``agent_v2`` gathers the two methods concurrently on one hub at startup — so the
same race survived one layer up until the lock moved to the public entry points.
"""
import asyncio
import logging

import pytest

from app.ai.context.context_hub import ContextHub


def _bare_hub():
    """A ContextHub with only what the locking machinery touches.

    The real constructor wires a dozen builders against live ORM objects; this
    exercises the serialization contract without that apparatus.
    """
    hub = object.__new__(ContextHub)
    hub._db_lock = asyncio.Lock()
    return hub


class _ConcurrencyTracker:
    """Records the peak number of overlapping sections."""

    def __init__(self):
        self._active = 0
        self.max_concurrency = 0

    async def section(self, hold=0.01):
        self._active += 1
        self.max_concurrency = max(self.max_concurrency, self._active)
        try:
            await asyncio.sleep(hold)
        finally:
            self._active -= 1


class _SharedSessionBuilder:
    """Mimics a builder bound to one AsyncSession.

    Overlapping calls raise the way SQLAlchemy does for concurrent operations
    on a single session; serialized callers never collide.
    """

    def __init__(self):
        self._active = 0
        self.max_concurrency = 0

    async def build(self, value):
        self._active += 1
        self.max_concurrency = max(self.max_concurrency, self._active)
        try:
            if self._active > 1:
                raise RuntimeError(
                    "This session is provisioning a new connection; "
                    "concurrent operations are not permitted"
                )
            await asyncio.sleep(0.005)  # widen the overlap window
            return value
        finally:
            self._active -= 1


# --------------------------------------------------------------------------
# The public lifecycle: what agent_v2 actually does at startup
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_prime_static_and_refresh_warm_serialize_against_each_other():
    """``agent_v2`` gathers these two on one hub and one session:

        await asyncio.gather(
            self.context_hub.prime_static(query=prompt_text),
            self.context_hub.refresh_warm(),
        )

    So the guard has to sit at the public entry points. Stubbing the locked
    bodies keeps this pinned to lock *placement* rather than to the shape of the
    builder graph underneath.
    """
    hub = _bare_hub()
    tracker = _ConcurrencyTracker()

    async def fake_prime(query=None):
        await tracker.section()

    async def fake_warm():
        await tracker.section()

    hub._prime_static_locked = fake_prime
    hub._refresh_warm_locked = fake_warm

    await asyncio.gather(hub.prime_static(query="q"), hub.refresh_warm())

    assert tracker.max_concurrency == 1, (
        "prime_static and refresh_warm overlapped on the shared session"
    )


@pytest.mark.asyncio
async def test_run_builders_refuses_to_run_without_the_lock():
    """The lock moved to the callers, so a direct call must not silently skip it.

    Without this, re-introducing the original gap — guarding only the builder
    list while the surrounding session reads run free — would pass unnoticed.
    """
    hub = _bare_hub()

    async def ok():
        return "fine"

    with pytest.raises(RuntimeError, match="_db_lock"):
        await hub._run_builders("refresh_warm", [("messages", ok)])


# --------------------------------------------------------------------------
# Inside one phase
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_builders_sharing_a_session_are_serialized():
    """One builder in flight at a time, every section returned."""
    shared = _SharedSessionBuilder()
    hub = _bare_hub()

    async with hub._db_lock:
        results = await hub._run_builders("refresh_warm", [
            ("messages", lambda: shared.build("messages")),
            ("queries", lambda: shared.build("queries")),
            ("mentions", lambda: shared.build("mentions")),
            ("entities", lambda: shared.build("entities")),
        ])

    assert shared.max_concurrency == 1, "builders overlapped on the shared session"
    assert results == ["messages", "queries", "mentions", "entities"]
    assert not any(isinstance(r, Exception) for r in results)


@pytest.mark.asyncio
async def test_gathering_the_same_builders_would_race():
    """Guards that the serialization above is actually load-bearing.

    If this ever stops racing, the fake no longer models the hazard and the
    tests above prove nothing.
    """
    shared = _SharedSessionBuilder()

    results = await asyncio.gather(
        shared.build("messages"),
        shared.build("queries"),
        shared.build("mentions"),
        shared.build("entities"),
        return_exceptions=True,
    )

    assert shared.max_concurrency > 1, "expected the builders to overlap"
    assert any(isinstance(r, Exception) for r in results), (
        "expected at least one section to be lost to the race"
    )


@pytest.mark.asyncio
async def test_failing_builder_is_reported_not_swallowed():
    """A failure names its section in the log and comes back positionally.

    The old ``return_exceptions=True`` tuple was unpacked straight into
    ``x if not isinstance(x, Exception) else None``, so a lost section left no
    trace anywhere.

    Captures via an explicit handler rather than ``caplog``, and re-enables the
    logger for the duration: the test configuration leaves the app's loggers
    with ``disabled = True``, so neither ``caplog`` nor a plain handler sees
    anything until that is lifted.
    """
    async def ok():
        return "fine"

    async def boom():
        raise RuntimeError("builder exploded")

    records = []

    class _Capture(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    handler = _Capture(level=logging.WARNING)
    hub_logger = logging.getLogger("app.ai.context.context_hub")
    was_disabled = hub_logger.disabled
    hub_logger.disabled = False
    hub_logger.addHandler(handler)
    try:
        hub = _bare_hub()
        async with hub._db_lock:
            results = await hub._run_builders("prime_static", [
                ("schemas", ok),
                ("instructions", boom),
            ])
    finally:
        hub_logger.removeHandler(handler)
        hub_logger.disabled = was_disabled

    assert results[0] == "fine"
    assert isinstance(results[1], RuntimeError)
    text = "\n".join(records)
    assert "instructions" in text, f"section not named in log: {text!r}"
    assert "prime_static" in text, f"phase not named in log: {text!r}"


@pytest.mark.asyncio
async def test_lock_is_released_when_a_builder_raises():
    """A failure must not strand the session lock and deadlock the next refresh."""
    async def boom():
        raise RuntimeError("first refresh failed")

    async def ok():
        return "second refresh ran"

    hub = _bare_hub()
    async with hub._db_lock:
        await hub._run_builders("refresh_warm", [("messages", boom)])
    assert not hub._db_lock.locked()

    async def second():
        async with hub._db_lock:
            return await hub._run_builders("refresh_warm", [("messages", ok)])

    assert await asyncio.wait_for(second(), timeout=1.0) == ["second refresh ran"]


@pytest.mark.asyncio
async def test_cancellation_leaves_no_unstarted_coroutines():
    """Builders are built on demand, so a cancelled phase orphans nothing.

    When every coroutine was constructed up front, cancelling during the first
    await left the rest never-awaited, surfacing as ``RuntimeWarning: coroutine
    ... was never awaited`` during later event-loop cleanup. Asserting that the
    later factory is never *called* is the deterministic form of that check —
    the warning itself only fires at garbage-collection time.
    """
    hub = _bare_hub()
    built_later = []

    async def slow():
        await asyncio.sleep(10)

    def first_factory():
        return slow()

    def second_factory():
        built_later.append("second")
        return slow()

    async def runner():
        async with hub._db_lock:
            await hub._run_builders("refresh_warm", [
                ("messages", first_factory),
                ("queries", second_factory),
            ])

    task = asyncio.create_task(runner())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert built_later == [], (
        "a later builder's coroutine was constructed before its turn, so "
        "cancellation orphaned it"
    )
    assert not hub._db_lock.locked(), "cancellation stranded the session lock"
