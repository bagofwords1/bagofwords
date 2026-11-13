"""Unit-suite conftest.

The root conftest's autouse ``run_migrations`` swaps in a fresh SQLite file
(dispose the async engine, unlink, copy the session template) before *and*
after every test. That is the right default for ``e2e/`` where every test goes
through HTTP routes into the DB, but it cost ~1s of pure overhead per test here
— on a ~6500-test suite that was most of the hour the unit job took in CI —
while only a small minority of unit tests ever open a session.

So in ``unit/`` the fresh-DB fixture is opt-in: mark a test or module with
``@pytest.mark.db`` (``pytestmark = pytest.mark.db`` at module level) and it
gets exactly the same per-test fresh schema as before. Everything else runs
with no database work at all. A test that forgets the marker fails loudly with
"no such table" rather than silently sharing state, because no schema exists
without it.
"""
import asyncio
from functools import partial
import logging

import pytest

from app.core.fire_and_forget import _pending_tasks

# How long a test's spawned tasks get to finish after the test has ended. Normal
# hooks take milliseconds; this only bounds one that waits on something the test
# never provides, and is what such a test pays.
SPAWNED_TASK_GRACE_SECONDS = 5.0


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "db: this unit test opens a database session — give it the per-test fresh "
        "schema the root conftest builds for e2e tests (opt-in under tests/unit).",
    )


@pytest.fixture(scope="function", autouse=True)
def run_migrations(request):
    """Override the root autouse fixture: only build the DB when asked for."""
    if request.node.get_closest_marker("db") is not None:
        # Resolving it lazily means unmarked tests never even build the
        # session-scoped template; its teardown is chained after ours.
        request.getfixturevalue("_fresh_test_database")
    yield


def finish_loop_tasks(loop, grace_seconds=SPAWNED_TASK_GRACE_SECONDS):
    """Leave no task behind on a loop that is about to be closed.

    Saving a finished CompletionBlock or Step spawns a chat-platform hook that
    opens the app's own database, even from a test on an in-memory one.
    pytest-asyncio closes a test's loop without cancelling anything (unlike
    ``asyncio.run``), so a hook still running at that point is abandoned, while
    the aiosqlite thread under it carries on with its statement. That has done
    two kinds of damage to the tests that follow:

    - in a test on an in-memory database the hook can be the first connection
      to the database file, which makes it the one to switch that file to WAL.
      If it is still doing so when the next test swaps the file, it shares the
      ``-journal`` path with that test's own switch: the new connection takes
      the journal for a hot one and fails deleting it after the hook already
      has (SQLITE_IOERR_DELETE_NOENT, "disk I/O error", issue #1573);
    - as the first connection of the process-wide engine, an abandoned hook
      keeps a lock, and the next test to connect waits forever.

    So a loop ends the way ``asyncio.run`` ends one, with one difference: hooks
    get to finish, because their work is real behaviour a test may be seeding
    for. Only one still running after ``grace_seconds`` is cancelled, and
    anything else on the loop is cancelled straight away.
    """
    if loop.is_closed() or not asyncio.all_tasks(loop):
        return

    async def finish():
        this_task = asyncio.current_task()
        while True:
            spawned = {t for t in _pending_tasks if t.get_loop() is loop and not t.done()}
            if not spawned:
                break
            _, unfinished = await asyncio.wait(spawned, timeout=grace_seconds)
            if unfinished:
                break
        others = asyncio.all_tasks(loop) - {this_task}
        for task in others:
            task.cancel()
        if others:
            await asyncio.wait(others, timeout=grace_seconds)

    loop.run_until_complete(finish())


@pytest.hookimpl(hookwrapper=True)
def pytest_fixture_setup(fixturedef):
    """Run ``finish_loop_tasks`` before every test loop closes.

    On the fixture and not on a test, so it covers each user of a loop (async
    tests and async fixtures alike) and costs a sync test nothing. Finalizers
    run last-in-first-out, so this one runs after the fixtures using the loop
    and before the ``loop.close()`` in the fixture itself.
    """
    outcome = yield
    if fixturedef.argname == "event_loop" and outcome.excinfo is None:
        fixturedef.addfinalizer(partial(finish_loop_tasks, outcome.get_result()))


_KMS_LOGGERS = (
    "app.data_sources.clients.kms_lighthouse_client",
    "app.data_sources.clients.kms_lighthouse_diagnostics",
    "urllib3",
    "urllib3.connectionpool",
    "urllib3.connection",
    "urllib3.util.retry",
)


@pytest.fixture(autouse=True)
def _kms_loggers_enabled(request):
    """KMS unit tests assert on logs, and earlier tests in the worker can silence them.

    alembic's ``fileConfig`` disables every logger that exists when it runs, and a
    level set on ``urllib3`` hides its debug records, so these tests passed or failed
    by which test came first in the worker.
    """
    if not request.node.path.name.startswith("test_kms_lighthouse_"):
        yield
        return
    loggers = [logging.getLogger(name) for name in _KMS_LOGGERS]
    saved = [(logger.disabled, logger.level) for logger in loggers]
    for logger in loggers:
        logger.disabled = False
        logger.setLevel(logging.NOTSET)
    yield
    for logger, (disabled, level) in zip(loggers, saved):
        logger.disabled = disabled
        logger.setLevel(level)
