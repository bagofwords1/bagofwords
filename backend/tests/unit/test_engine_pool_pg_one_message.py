"""Postgres engines built for the one-message read, and the pool's backstop.

``PostgresqlClient.execute_query`` turns psycopg2's local ``autocommit``
flag on for its read and off again before the connection goes back. Whether
an engine is used that way is decided once, when ``get_engine`` builds it
(``BOW_PG_ONE_MESSAGE``, default on), so a change of the variable at run
time cannot mix the two read paths on one engine.

Such an engine also gets a pool ``reset`` listener: a connection that comes
back with ``autocommit`` still on is dropped instead of pooled. It looks at
that flag only (a ``connect()`` user returns with its transaction open and
the pool rolls it back), runs before the checkin listeners, and never
raises.
"""

import logging
from types import SimpleNamespace

import pytest
import sqlalchemy
import sqlalchemy.event
from sqlalchemy.pool import QueuePool

from app.data_sources import engine_pool

PG_URI = "postgresql://u:p@db.invalid:5432/d"
POOL_LOGGER = "app.data_sources.engine_pool"


@pytest.fixture(autouse=True)
def _clean_pool(monkeypatch):
    monkeypatch.delenv("BOW_PG_ONE_MESSAGE", raising=False)
    engine_pool.dispose_all()
    yield
    engine_pool.dispose_all()


def _backstop_attached(eng) -> bool:
    return sqlalchemy.event.contains(eng, "reset", engine_pool._drop_autocommit_on_reset)


# ------------------------------------------------------------------ get_engine wiring

@pytest.mark.parametrize("uri", [PG_URI, "postgresql+psycopg2://u:p@db.invalid/d"])
def test_a_cached_psycopg2_engine_is_built_for_the_one_message_read(uri):
    eng = engine_pool.get_engine(uri)
    assert engine_pool.pg_one_message(eng) is True
    assert _backstop_attached(eng)
    # The idle reaper disposes pools; the replacement pool keeps the listener.
    eng.dispose()
    assert _backstop_attached(eng)


def test_an_ephemeral_psycopg2_engine_is_built_for_it_too():
    with engine_pool.ephemeral():
        eng = engine_pool.get_engine(PG_URI)
        assert engine_pool.pg_one_message(eng) is True
        assert _backstop_attached(eng)


@pytest.mark.parametrize("value", ["off", "0", "false", "no", "OFF"])
def test_the_switch_off_builds_an_engine_without_it(monkeypatch, value):
    monkeypatch.setenv("BOW_PG_ONE_MESSAGE", value)
    eng = engine_pool.get_engine(PG_URI)
    assert engine_pool.pg_one_message(eng) is False
    assert not _backstop_attached(eng)
    with engine_pool.ephemeral():
        eng = engine_pool.get_engine(PG_URI)
        assert engine_pool.pg_one_message(eng) is False
        assert not _backstop_attached(eng)


def test_the_switch_is_read_when_the_engine_is_built(monkeypatch):
    eng = engine_pool.get_engine(PG_URI)
    monkeypatch.setenv("BOW_PG_ONE_MESSAGE", "off")
    assert engine_pool.get_engine(PG_URI) is eng
    assert engine_pool.pg_one_message(eng) is True


@pytest.mark.parametrize("uri", ["sqlite:///{tmp}/db.sqlite", "mysql+pymysql://u:p@db.invalid/app",
                                 "mssql+pymssql://u:p@db.invalid/app"])
def test_other_engines_are_not_built_for_it(tmp_path, uri):
    eng = engine_pool.get_engine(uri.format(tmp=tmp_path))
    assert engine_pool.pg_one_message(eng) is False
    assert not _backstop_attached(eng)


def test_an_object_that_is_no_engine_of_ours_is_not_built_for_it():
    assert engine_pool.pg_one_message(object()) is False
    assert engine_pool.pg_one_message(sqlalchemy.create_engine("sqlite://")) is False


# ------------------------------------------------------------------ the backstop, on a real QueuePool

class FakeConnection:
    """The parts of a psycopg2 connection the pool and the listeners touch.
    Setting ``readonly`` in autocommit would send a SET: recorded."""

    def __init__(self):
        self.autocommit = False
        self._readonly = None
        self.readonly_set_in_autocommit = []
        self.closed = 0
        self.rollbacks = 0

    @property
    def readonly(self):
        return self._readonly

    @readonly.setter
    def readonly(self, value):
        if self.autocommit:
            self.readonly_set_in_autocommit.append(value)
        self._readonly = value

    def rollback(self):
        if self.closed:
            raise RuntimeError("connection already closed")
        self.rollbacks += 1

    def close(self):
        self.closed = 1


class BrokenFlagConnection(FakeConnection):
    @property
    def autocommit(self):
        raise RuntimeError("cannot read autocommit")

    @autocommit.setter
    def autocommit(self, value):
        pass


def _pool(make, *, with_readonly_reset=False):
    made = []

    def creator():
        made.append(make())
        return made[-1]

    pool = QueuePool(creator, pool_size=1, max_overflow=0, timeout=0.5)
    sqlalchemy.event.listen(pool, "reset", engine_pool._drop_autocommit_on_reset)
    if with_readonly_reset:
        sqlalchemy.event.listen(pool, "checkin", engine_pool._reset_readonly_on_checkin)
    return pool, made


def _warnings(caplog):
    return [r for r in caplog.records if r.name == POOL_LOGGER and r.levelno == logging.WARNING]


def test_a_connection_returned_in_autocommit_is_dropped(caplog):
    logging.getLogger(POOL_LOGGER).disabled = False
    caplog.set_level(logging.WARNING, logger=POOL_LOGGER)
    pool, made = _pool(FakeConnection)
    fairy = pool.connect()
    fairy.dbapi_connection.autocommit = True
    fairy.close()
    assert made[0].closed
    pool.connect().close()
    assert len(made) == 2 and not made[1].closed
    assert _warnings(caplog), "the drop is logged"


def test_a_connection_returned_without_autocommit_is_pooled_whatever_its_transaction():
    """`connect()` users come back with their transaction open: the pool's
    rollback ends it, and the connection is reused."""
    pool, made = _pool(FakeConnection)
    for _ in range(3):
        pool.connect().close()
    assert len(made) == 1 and not made[0].closed
    assert made[0].rollbacks == 3


def test_the_backstop_never_raises_and_the_pool_keeps_its_slot(caplog):
    logging.getLogger(POOL_LOGGER).disabled = False
    caplog.set_level(logging.WARNING, logger=POOL_LOGGER)
    pool, made = _pool(BrokenFlagConnection)
    for _ in range(3):
        # A lost slot would make a later checkout time out here.
        pool.connect().close()
    assert pool.checkedout() == 0
    assert len(made) == 3 and all(c.closed for c in made)
    assert all("RuntimeError" in r.getMessage() and "cannot read" not in r.getMessage()
               for r in _warnings(caplog))


def test_the_backstop_runs_before_the_readonly_reset_at_checkin():
    """In autocommit psycopg2 sends a SET when `readonly` changes: the
    connection is gone before the checkin listener could touch it."""
    pool, made = _pool(FakeConnection, with_readonly_reset=True)
    fairy = pool.connect()
    fairy.dbapi_connection._readonly = False  # what SQLAlchemy leaves after a read-only read
    fairy.dbapi_connection.autocommit = True
    fairy.close()
    assert made[0].closed
    assert made[0].readonly_set_in_autocommit == []
    pool.connect().close()
    assert len(made) == 2


def test_the_backstop_leaves_a_missing_or_closed_connection_alone():
    dropped = []
    record = SimpleNamespace(invalidate=lambda e=None, soft=False: dropped.append(e))
    engine_pool._drop_autocommit_on_reset(None, record, None)
    closed = FakeConnection()
    closed.closed = 1
    closed.autocommit = True
    engine_pool._drop_autocommit_on_reset(closed, record, None)
    assert dropped == []
