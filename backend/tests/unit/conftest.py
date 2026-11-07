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
import logging

import pytest


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
    "urllib3.connectionpool",
    "urllib3.connection",
    "urllib3.util.retry",
)


@pytest.fixture(autouse=True)
def _kms_loggers_enabled(request):
    """KMS unit tests assert on logs, and a migration earlier in the worker disables them.

    alembic's ``fileConfig`` disables every logger that exists when it runs, so
    these tests passed or failed by which test came first in the worker.
    """
    if not request.node.path.name.startswith("test_kms_lighthouse_"):
        yield
        return
    loggers = [logging.getLogger(name) for name in _KMS_LOGGERS]
    was_disabled = [logger.disabled for logger in loggers]
    for logger in loggers:
        logger.disabled = False
    yield
    for logger, disabled in zip(loggers, was_disabled):
        logger.disabled = disabled
