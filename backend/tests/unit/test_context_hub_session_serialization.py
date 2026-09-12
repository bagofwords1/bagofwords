"""Regression: context builders must not collide on the hub's shared session.

Production failure class (reproduced against Postgres in a live sandbox):
``ContextHub.refresh_warm`` and ``prime_static`` ran their builders under
``asyncio.gather(..., return_exceptions=True)``. Every builder is constructed
with the *same* ``AsyncSession``, which is not safe for concurrent use, so the
overlapping calls raised ``This session is provisioning a new connection;
concurrent operations are not permitted``.

The window is widest when the session holds no connection yet — exactly the
state ``agent_v2._release_db_between_steps()`` leaves it in before each loop
iteration's refresh. Measured: 100% of refreshes that followed a commit lost at
least one section, and 27% across a 10-agent concurrent run.

The damage was silent. Each builder catches its own failure and returns an empty
section, so the planner was handed a ``<queries>`` section with no items — the
agent's record of what it had already queried — as though the report genuinely
had none.

These tests pin the two properties that fix depends on: the builders are
entered one at a time, and a builder that does fail is reported rather than
swallowed.
"""
import asyncio
import logging

import pytest

from app.ai.context.context_hub import ContextHub


def _bare_hub():
    """A ContextHub with only what ``_run_builders`` touches.

    The real constructor wires a dozen builders against live ORM objects; this
    exercises the serialization contract without that apparatus.
    """
    hub = object.__new__(ContextHub)
    hub._db_lock = asyncio.Lock()
    return hub


class _SharedSessionBuilder:
    """Mimics a builder bound to one AsyncSession.

    Overlapping calls raise the way SQLAlchemy does for concurrent operations
    on a single session; serialized callers never collide.
    """

    def __init__(self):
        self._active = 0
        self.max_concurrency = 0
        self.calls = 0

    async def build(self, value):
        self.calls += 1
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


@pytest.mark.asyncio
async def test_builders_sharing_a_session_are_serialized():
    """The whole point: one builder in flight at a time, every section returned."""
    shared = _SharedSessionBuilder()
    hub = _bare_hub()

    results = await hub._run_builders("refresh_warm", [
        ("messages", shared.build("messages")),
        ("queries", shared.build("queries")),
        ("mentions", shared.build("mentions")),
        ("entities", shared.build("entities")),
    ])

    assert shared.max_concurrency == 1, "builders overlapped on the shared session"
    assert results == ["messages", "queries", "mentions", "entities"]
    assert not any(isinstance(r, Exception) for r in results)


@pytest.mark.asyncio
async def test_gathering_the_same_builders_would_race():
    """Guards that the serialization above is actually load-bearing.

    If this ever stops racing, the fake no longer models the hazard and the
    test above proves nothing.
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
        results = await hub._run_builders("prime_static", [
            ("schemas", ok()),
            ("instructions", boom()),
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
    await hub._run_builders("refresh_warm", [("messages", boom())])
    assert not hub._db_lock.locked()

    results = await asyncio.wait_for(
        hub._run_builders("refresh_warm", [("messages", ok())]), timeout=1.0
    )
    assert results == ["second refresh ran"]
