"""Postgres engines ping a pooled connection only when it may be dead.

``pool_pre_ping`` sends a ``SELECT 1`` on every checkout: one network round
trip per query. On Postgres engines (psycopg2) a checkout listener replaces
it and pings only when the connection's socket is readable (a backend the
server ended has sent its goodbye and closed) or the connection has sat in
the pool past the idle threshold. A brand-new connection is not pinged, as
with pre-ping. A disconnect raises the same error pre-ping raises, so the
pool flushes and retries the same way. At checkin the listener stamps the
time. (The read-only flag reset at checkin is a separate listener, covered
in ``test_engine_pool_pg_readonly.py``.)

Every other dialect keeps ``pool_pre_ping``. ``BOW_PG_PING_LISTENER=off``
gives Postgres back the plain pre-ping and no ping listeners.
"""

import socket
from types import SimpleNamespace

import pytest
import sqlalchemy
import sqlalchemy.event
from sqlalchemy import exc as sa_exc
from sqlalchemy.pool import QueuePool
from sqlalchemy.pool.base import _ConnDialect

from app.data_sources import engine_pool

PG_URI = "postgresql://u:p@db.invalid:5432/d"


@pytest.fixture(autouse=True)
def _clean_pool(monkeypatch):
    monkeypatch.delenv("BOW_PG_PING_LISTENER", raising=False)
    monkeypatch.delenv("BOW_PG_IDLE_PING_S", raising=False)
    engine_pool.dispose_all()
    yield
    engine_pool.dispose_all()


class FakeDbError(Exception):
    pass


class FakeDialect:
    """The two calls the listener makes on the engine's dialect."""

    loaded_dbapi = SimpleNamespace(Error=FakeDbError)

    def __init__(self, ping_error=None, disconnect=False):
        self.pings = []
        self.ping_error = ping_error
        self.disconnect = disconnect

    def do_ping(self, dbapi_connection):
        self.pings.append(dbapi_connection)
        if self.ping_error is not None:
            raise self.ping_error
        return True

    def is_disconnect(self, error, dbapi_connection, cursor):
        return self.disconnect


class FakeConnection:
    """A DBAPI connection over one end of a socket pair."""

    def __init__(self, sock):
        self._sock = sock

    def fileno(self):
        return self._sock.fileno()


class NoSocket:
    """A DBAPI connection whose ``fileno()`` gives no usable descriptor."""

    def __init__(self, fileno):
        self._fileno = fileno

    def fileno(self):
        if isinstance(self._fileno, Exception):
            raise self._fileno
        return self._fileno


@pytest.fixture
def pair():
    ours, server = socket.socketpair()
    yield FakeConnection(ours), server
    ours.close()
    server.close()


class Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


def _listener(dialect, idle_s=120.0, clock=None):
    return engine_pool._PingWhenNeeded(dialect, idle_s, clock=clock or Clock())


def _record():
    return SimpleNamespace(info={})


# ------------------------------------------------------------------ checkout

def test_a_fresh_connection_is_not_pinged(pair):
    conn, server = pair
    server.sendall(b"x")  # even when readable: it has never been checked in
    dialect = FakeDialect()
    _listener(dialect).checkout(conn, _record(), None)
    assert dialect.pings == []


def test_a_recent_quiet_connection_is_not_pinged(pair):
    conn, _server = pair
    clock = Clock()
    dialect = FakeDialect()
    listener = _listener(dialect, idle_s=120.0, clock=clock)
    record = _record()
    listener.checkin(conn, record)
    clock.now += 119.0
    listener.checkout(conn, record, None)
    assert dialect.pings == []


def test_a_readable_connection_is_pinged(pair):
    conn, server = pair
    clock = Clock()
    dialect = FakeDialect()
    listener = _listener(dialect, clock=clock)
    record = _record()
    listener.checkin(conn, record)
    clock.now += 1.0
    server.sendall(b"x")
    listener.checkout(conn, record, None)
    assert dialect.pings == [conn]


def test_a_connection_whose_peer_closed_is_pinged(pair):
    conn, server = pair
    dialect = FakeDialect()
    listener = _listener(dialect)
    record = _record()
    listener.checkin(conn, record)
    server.close()
    listener.checkout(conn, record, None)
    assert dialect.pings == [conn]


def test_a_connection_idle_past_the_threshold_is_pinged(pair):
    conn, _server = pair
    clock = Clock()
    dialect = FakeDialect()
    listener = _listener(dialect, idle_s=120.0, clock=clock)
    record = _record()
    listener.checkin(conn, record)
    clock.now += 121.0
    listener.checkout(conn, record, None)
    assert dialect.pings == [conn]


@pytest.mark.parametrize("fileno", [OSError("no socket"), -1, "not a descriptor"],
                         ids=["fileno-raises", "minus-one", "poll-refuses-it"])
def test_a_recent_connection_without_a_usable_socket_is_pinged(fileno):
    conn = NoSocket(fileno)
    dialect = FakeDialect()
    listener = _listener(dialect)
    record = _record()
    listener.checkin(conn, record)
    listener.checkout(conn, record, None)
    assert dialect.pings == [conn]


def test_a_disconnect_on_the_ping_raises_what_pre_ping_raises(pair):
    conn, server = pair
    dialect = FakeDialect(ping_error=FakeDbError("server closed the connection"), disconnect=True)
    listener = _listener(dialect)
    record = _record()
    listener.checkin(conn, record)
    server.close()
    with pytest.raises(sa_exc.InvalidatePoolError) as error:
        listener.checkout(conn, record, None)
    # The pool flushes every older connection, as pre-ping does.
    assert error.value.invalidate_pool is True
    assert isinstance(error.value.__cause__, FakeDbError)


def test_another_ping_error_propagates(pair):
    conn, server = pair
    dialect = FakeDialect(ping_error=FakeDbError("permission denied"), disconnect=False)
    listener = _listener(dialect)
    record = _record()
    listener.checkin(conn, record)
    server.sendall(b"x")
    with pytest.raises(FakeDbError, match="permission denied"):
        listener.checkout(conn, record, None)


# ------------------------------------------------------------------ checkin

def test_checkin_stamps_the_time(pair):
    conn, _server = pair
    clock = Clock(now=42.0)
    record = _record()
    _listener(FakeDialect(), clock=clock).checkin(conn, record)
    assert 42.0 in record.info.values()


def test_checkin_of_an_invalidated_connection_does_not_fail():
    record = _record()
    _listener(FakeDialect()).checkin(None, record)


# ------------------------------------------------------------------ a real pool

class PoolConnection(FakeConnection):
    """What the pool needs from a DBAPI connection, over a socket pair."""

    def __init__(self, sock, server):
        super().__init__(sock)
        self.server = server
        self.closed = False

    def rollback(self):
        pass

    def close(self):
        self.closed = True
        self._sock.close()


class PoolDialect(_ConnDialect):
    """The pool's own stand-in dialect, plus the ping and the disconnect
    test the listener asks for. A ping on a connection whose peer is gone
    fails as a disconnect."""

    loaded_dbapi = SimpleNamespace(Error=FakeDbError)

    def __init__(self):
        self.pings = []

    def do_ping(self, dbapi_connection):
        self.pings.append(dbapi_connection)
        if dbapi_connection.server.fileno() == -1:
            raise FakeDbError("server closed the connection unexpectedly")
        return True

    def is_disconnect(self, error, dbapi_connection, cursor):
        return "server closed" in str(error)


def test_a_real_pool_replaces_a_connection_the_ping_finds_dead():
    made = []

    def creator():
        ours, server = socket.socketpair()
        made.append(PoolConnection(ours, server))
        return made[-1]

    dialect = PoolDialect()
    pool = QueuePool(creator, dialect=dialect, pool_size=1, max_overflow=0)
    listener = engine_pool._PingWhenNeeded(dialect, 120.0)
    sqlalchemy.event.listen(pool, "checkout", listener.checkout)
    sqlalchemy.event.listen(pool, "checkin", listener.checkin)
    invalidated = []
    sqlalchemy.event.listen(pool, "invalidate", lambda conn, rec, err: invalidated.append((conn, err)))
    try:
        pool.connect().close()
        old = made[0]
        old.server.close()  # the server ended the backend: the socket turns readable

        fairy = pool.connect()

        # The listener raised the pre-ping error and the pool retried once.
        assert [type(err) for _conn, err in invalidated] == [sa_exc.InvalidatePoolError]
        assert invalidated[0][0] is old
        assert isinstance(invalidated[0][1].__cause__, FakeDbError)
        assert old.closed
        assert len(made) == 2 and fairy.dbapi_connection is made[1]
        # The new connection carries no checkin stamp and is not pinged.
        assert engine_pool._PG_CHECKIN_AT not in fairy._connection_record.info
        assert dialect.pings == [old]
        fairy.close()
    finally:
        pool.dispose()
        for conn in made:
            conn.server.close()
            conn.close()


# ------------------------------------------------------------------ get_engine wiring

def _ping_listeners(eng) -> tuple:
    """(checkout, checkin) ping listeners on the engine's pool."""

    def ours(listeners):
        return [fn for fn in listeners
                if isinstance(getattr(fn, "__self__", None), engine_pool._PingWhenNeeded)]

    return ours(eng.pool.dispatch.checkout), ours(eng.pool.dispatch.checkin)


def _has_both_ping_listeners(eng) -> bool:
    checkout, checkin = _ping_listeners(eng)
    return len(checkout) == 1 and len(checkin) == 1 and checkout[0].__self__ is checkin[0].__self__


def _has_no_ping_listener(eng) -> bool:
    return _ping_listeners(eng) == ([], [])


def test_a_postgres_engine_gets_the_listener_instead_of_pre_ping():
    eng = engine_pool.get_engine(PG_URI)
    assert eng.pool._pre_ping is False
    assert _has_both_ping_listeners(eng)


def test_the_listener_survives_the_reaper_disposing_the_pool():
    eng = engine_pool.get_engine(PG_URI)
    eng.dispose()
    assert eng.pool._pre_ping is False
    assert _has_both_ping_listeners(eng)


def test_an_ephemeral_postgres_engine_gets_the_listener_too():
    with engine_pool.ephemeral():
        eng = engine_pool.get_engine(PG_URI)
        assert eng.pool._pre_ping is False
        assert _has_both_ping_listeners(eng)


@pytest.mark.parametrize("ephemeral", [False, True])
def test_an_engine_built_from_a_url_object_gets_every_postgres_listener(ephemeral):
    url = sqlalchemy.engine.make_url(PG_URI)
    if ephemeral:
        with engine_pool.ephemeral():
            eng = engine_pool.get_engine(url)
    else:
        eng = engine_pool.get_engine(url)
    assert eng.pool._pre_ping is False
    assert _has_both_ping_listeners(eng)
    assert engine_pool._reset_readonly_on_checkin in list(eng.pool.dispatch.checkin)
    assert engine_pool.pg_one_message(eng)


@pytest.mark.parametrize("value", ["off", "0", "false", "OFF"])
def test_the_switch_off_restores_pre_ping_and_no_ping_listener(monkeypatch, value):
    monkeypatch.setenv("BOW_PG_PING_LISTENER", value)
    eng = engine_pool.get_engine(PG_URI)
    assert eng.pool._pre_ping is True
    assert _has_no_ping_listener(eng)
    # The read-only reset is not part of the switch.
    assert sqlalchemy.event.contains(eng, "checkin", engine_pool._reset_readonly_on_checkin)


def test_a_non_postgres_engine_keeps_pre_ping(tmp_path):
    eng = engine_pool.get_engine(f"sqlite:///{tmp_path / 'db.sqlite'}")
    assert eng.pool._pre_ping is True
    assert _has_no_ping_listener(eng)


@pytest.mark.parametrize("uri", ["postgresql+pg8000://u:p@h/d", "postgresql+asyncpg://u:p@h/d",
                                 "mysql+pymysql://u:p@h/d", "sqlite://"])
def test_only_psycopg2_postgres_urls_get_the_listener(uri):
    assert engine_pool._pg_idle_ping_s(uri) is None


@pytest.mark.parametrize("uri", [PG_URI, "postgresql+psycopg2://u:p@h/d"])
def test_psycopg2_postgres_urls_get_the_listener_with_the_default_threshold(uri):
    assert engine_pool._pg_idle_ping_s(uri) == 120.0


@pytest.mark.parametrize("value, expected", [("5", 5.0), ("0", 0.0), ("junk", 120.0)])
def test_the_idle_threshold_comes_from_the_environment(monkeypatch, value, expected):
    monkeypatch.setenv("BOW_PG_IDLE_PING_S", value)
    assert engine_pool._pg_idle_ping_s(PG_URI) == expected
