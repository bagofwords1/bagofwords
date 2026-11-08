"""A unit test's event loop closes with nothing left running on it.

A task abandoned on a closed loop keeps going where it has a thread under it,
and that has broken the tests after it (see ``finish_loop_tasks`` in
``tests/unit/conftest.py``).
"""
import asyncio

import pytest

from app.core.fire_and_forget import spawn
from tests.unit.conftest import finish_loop_tasks


@pytest.fixture
def tasks_at_loop_close():
    """Set up before the event loop, so its teardown sees it already closed."""
    tasks = {}
    yield tasks
    assert tasks["hook"].get_loop().is_closed()
    assert tasks["hook"].result() == "finished"
    assert tasks["other"].cancelled()


@pytest.mark.asyncio
async def test_a_spawned_hook_finishes_and_any_other_task_is_cancelled(tasks_at_loop_close):
    async def hook():
        await asyncio.sleep(0.01)
        return "finished"

    tasks_at_loop_close["hook"] = spawn(hook())
    tasks_at_loop_close["other"] = asyncio.create_task(asyncio.Event().wait())


def test_a_hook_spawned_while_finishing_finishes_too():
    loop = asyncio.new_event_loop()
    finished = []

    async def nested():
        await asyncio.sleep(0)
        finished.append("nested")

    async def hook():
        await asyncio.sleep(0)
        spawn(nested())

    async def start():
        spawn(hook())

    try:
        loop.run_until_complete(start())
        finish_loop_tasks(loop)
        assert finished == ["nested"]
    finally:
        loop.close()


def test_a_hook_that_never_finishes_is_cancelled_after_the_grace_period():
    loop = asyncio.new_event_loop()

    async def start():
        return spawn(asyncio.Event().wait())

    try:
        task = loop.run_until_complete(start())
        finish_loop_tasks(loop, grace_seconds=0.01)
        assert task.cancelled()
    finally:
        loop.close()
