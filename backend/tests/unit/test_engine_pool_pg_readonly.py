"""A pooled Postgres connection goes back to the session default on checkin.

A read with ``postgresql_readonly`` leaves psycopg2's ``readonly`` at
``False`` once SQLAlchemy resets it, and ``False`` is not the default: the
next plain transaction on that connection starts with ``BEGIN READ WRITE``,
which a hot standby refuses and which overrides a database whose default is
read-only. The pool's checkin listener puts it back to ``None`` (the server
default), and must never cost the pool a slot.
"""

import logging
import types

import pytest
import sqlalchemy

from app.data_sources import engine_pool


def _listener():
    return engine_pool._reset_readonly_on_checkin


POOL_LOGGER = "app.data_sources.engine_pool"
# Driver modules that are not installed: SQLAlchemy only needs a paramstyle
# to build the engine, and nothing here connects.
FAKE_DBAPI = types.SimpleNamespace(paramstyle="format")


@pytest.fixture(autouse=True)
def _clean_pool():
    engine_pool.dispose_all()
    yield
    engine_pool.dispose_all()


def _attached(eng) -> bool:
    return any(True for _ in eng.pool.dispatch.checkin)


# --------------------------------------------------------------------------
# Where the listener is attached
# --------------------------------------------------------------------------

PSYCOPG2_URIS = ["postgresql://u:p@db.invalid/app", "postgresql+psycopg2://u:p@db.invalid/app"]


@pytest.mark.parametrize("uri", PSYCOPG2_URIS)
def test_a_cached_psycopg2_engine_resets_readonly_on_checkin(uri):
    eng = engine_pool.get_engine(uri)
    assert _attached(eng)
    # The idle reaper disposes pools; the replacement pool keeps the listener.
    eng.dispose()
    assert _attached(eng)


@pytest.mark.parametrize("uri", PSYCOPG2_URIS)
def test_an_ephemeral_psycopg2_engine_resets_readonly_on_checkin(uri):
    with engine_pool.ephemeral():
        assert _attached(engine_pool.get_engine(uri))


OTHER_ENGINES = [
    ("sqlite:///{tmp}/db.sqlite", {}),
    ("mysql+pymysql://u:p@db.invalid/app", {}),
    ("mssql+pymssql://u:p@db.invalid/app", {}),
    ("postgresql+pg8000://u:p@db.invalid/app", {"module": FAKE_DBAPI}),
]


@pytest.mark.parametrize("uri,kwargs", OTHER_ENGINES)
def test_other_dialects_and_drivers_get_no_checkin_listener(tmp_path, uri, kwargs):
    uri = uri.format(tmp=tmp_path)
    assert not _attached(engine_pool.get_engine(uri, **kwargs))
    with engine_pool.ephemeral():
        assert not _attached(engine_pool.get_engine(uri, **kwargs))


# --------------------------------------------------------------------------
# What the listener does, on a real QueuePool
# --------------------------------------------------------------------------

class FakeConnection:
    """The parts of a psycopg2 connection the pool and the listener touch."""

    def __init__(self, readonly=None):
        self._readonly = readonly
        self.set_to = []
        self.closed = 0

    @property
    def readonly(self):
        return self._readonly

    @readonly.setter
    def readonly(self, value):
        self.set_to.append(value)
        self._readonly = value

    def rollback(self):
        pass

    def close(self):
        self.closed = 1


class BrokenSetterConnection(FakeConnection):
    @FakeConnection.readonly.setter
    def readonly(self, value):
        raise RuntimeError("cannot set readonly")


def _pool(make, *, pool_size=1, max_overflow=0):
    made = []

    def creator():
        made.append(make())
        return made[-1]

    pool = sqlalchemy.pool.QueuePool(creator, pool_size=pool_size, max_overflow=max_overflow, timeout=0.5)
    sqlalchemy.event.listen(pool, "checkin", _listener())
    return pool, made


@pytest.mark.parametrize("before", [False, True])
def test_checkin_puts_readonly_back_to_the_session_default(before):
    pool, made = _pool(FakeConnection)
    fairy = pool.connect()
    fairy.dbapi_connection._readonly = before
    fairy.close()
    assert made[0].readonly is None
    assert made[0].set_to == [None]


def test_checkin_leaves_a_default_connection_untouched():
    pool, made = _pool(FakeConnection)
    pool.connect().close()
    assert made[0].readonly is None
    assert made[0].set_to == []


def test_checkin_of_a_closed_connection_does_nothing():
    pool, made = _pool(lambda: FakeConnection(readonly=False))
    fairy = pool.connect()
    fairy.dbapi_connection.closed = 1
    fairy.close()
    assert made[0].set_to == []


def test_checkin_without_a_dbapi_connection_does_not_raise():
    _listener()(None, object())


def test_a_reset_that_fails_drops_the_connection_and_keeps_the_pool_slot(caplog):
    logging.getLogger(POOL_LOGGER).disabled = False
    caplog.set_level(logging.WARNING, logger=POOL_LOGGER)
    pool_size, max_overflow = 1, 1
    pool, made = _pool(lambda: BrokenSetterConnection(readonly=False), pool_size=pool_size,
                       max_overflow=max_overflow)
    rounds = 2 * (pool_size + max_overflow) + 1
    for _ in range(rounds):
        # A lost slot would make a later checkout time out here.
        pool.connect().close()
    assert pool.checkedout() == 0
    # Each failed reset dropped its connection, so every checkout opened a new one.
    assert len(made) == rounds
    assert all(c.closed for c in made)
    warnings = [r for r in caplog.records if r.name == POOL_LOGGER and r.levelno == logging.WARNING]
    assert len(warnings) == rounds
    assert all("RuntimeError" in r.getMessage() and "cannot set readonly" not in r.getMessage()
               for r in warnings)
