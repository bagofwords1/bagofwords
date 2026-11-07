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
import logging

import pytest
import pytest_asyncio


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

@pytest_asyncio.fixture
async def finish_spawned_tasks():
    """Finish fire-and-forget tasks inside the test that spawned them.

    Saving a finished CompletionBlock or Step spawns a chat-platform hook that
    opens the app's own database, even from a test on an in-memory one. Left
    running when the test ends, it has done two kinds of damage:

    - a swap of the test's database file under it: its switch to WAL shares the
      ``-journal`` path with the next test's, and one deletes the other's
      journal (SQLITE_IOERR_DELETE_NOENT, issue #1573);
    - a loop closed under it: as the process-wide engine's first connection it
      keeps a lock, and the next test to connect waits forever.

    Use it in a test that seeds such rows and doesn't wait for the hook.
    """
    yield
    from app.core.fire_and_forget import _pending_tasks

    loop = asyncio.get_running_loop()
    await asyncio.gather(*(t for t in _pending_tasks if t.get_loop() is loop), return_exceptions=True)
