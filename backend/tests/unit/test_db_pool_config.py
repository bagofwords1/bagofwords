"""The database pool geometry is operator-tunable, and must fail safe.

These knobs exist because the pool was hardcoded at `pool_size=20,
max_overflow=20`. The ceiling against the server is
`(pool_size + max_overflow) * workers * replicas`, and `start.sh` runs up to 4
workers — so a default deployment can ask for 160 connections against a stock
PostgreSQL's 97 usable ones. The server then refuses the connect outright
("remaining connection slots are reserved for roles with the SUPERUSER
attribute") rather than queueing, which reaches the user as a failed agent run.
An operator hitting that had no lever short of editing the image.

A malformed value must never take the process down at import: these are read
while the engine is being built, before anything can report a config error
usefully.
"""
import pytest

from app.settings import database as db


@pytest.fixture
def env(monkeypatch):
    for name in (
        "BOW_DB_POOL_SIZE",
        "BOW_DB_MAX_OVERFLOW",
        "BOW_DB_IDLE_SESSION_TIMEOUT_MS",
    ):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def test_defaults_when_unset(env):
    assert db._pool_size() == db.DEFAULT_DB_POOL_SIZE
    assert db._max_overflow() == db.DEFAULT_DB_MAX_OVERFLOW
    # Idle reaping stays off unless asked for: the GUC behind it is PG 14+, and
    # an unrecognised startup parameter fails every connection.
    assert db._idle_session_timeout_ms() == 0


def test_overrides_are_honoured(env):
    env.setenv("BOW_DB_POOL_SIZE", "5")
    env.setenv("BOW_DB_MAX_OVERFLOW", "3")
    env.setenv("BOW_DB_IDLE_SESSION_TIMEOUT_MS", "600000")
    assert db._pool_size() == 5
    assert db._max_overflow() == 3
    assert db._idle_session_timeout_ms() == 600000


@pytest.mark.parametrize("raw", ["", "   ", "twenty", "20.5", "-1"])
def test_unusable_values_fall_back_to_the_default(env, raw):
    """Empty, non-numeric and negative all fall back rather than raise."""
    env.setenv("BOW_DB_POOL_SIZE", raw)
    assert db._pool_size() == db.DEFAULT_DB_POOL_SIZE


def test_zero_pool_size_is_allowed(env):
    """0 is a legitimate SQLAlchemy pool_size (unbounded), so keep it."""
    env.setenv("BOW_DB_POOL_SIZE", "0")
    assert db._pool_size() == 0


def test_zero_disables_idle_reaping(env):
    """0 is the documented 'off' switch, not a value to pass to the server."""
    env.setenv("BOW_DB_IDLE_SESSION_TIMEOUT_MS", "0")
    assert db._idle_session_timeout_ms() == 0
