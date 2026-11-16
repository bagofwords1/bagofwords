"""Shared, pooled SQLAlchemy engines for source connections.

Every SQL client used to do this per query::

    engine = sqlalchemy.create_engine(uri)
    conn = engine.connect()
    ...
    conn.close(); engine.dispose()

which means each agent query paid a full connect *and* teardown. Measured on a
local Postgres, six analytical queries produced **24 statements**: for each one
the driver issued `select pg_catalog.version()`, `select current_schema()` and a
type-catalog lookup before the actual SQL. On a remote source those are three
extra round-trips per query, and on Oracle — which forks a server process per
session — an agent's exploratory burst is a logon storm. It is the single
cheapest thing to fix for legacy on-prem sources, and unlike caching it helps
every connector whether or not anything is accelerated.

Engines are cached by (uri, connect-args, extra key) and never disposed
per query; `conn.close()` returns the connection to the pool instead.

Correctness notes:
  * ``pool_pre_ping`` — sources here really do go away (restarts, failovers), and
    a pooled connection that died in the pool must be detected rather than handed
    out mid-query.
  * Postgres (psycopg2) engines replace ``pool_pre_ping`` with a checkout
    listener, because the pre-ping is one ``SELECT 1`` round trip on every
    query. The listener pings (with the dialect's own ping) only a pooled
    connection whose socket is readable — a backend the server ended has
    already sent its goodbye — or one that sat in the pool longer than
    ``BOW_PG_IDLE_PING_S`` (default 120 s), which covers links dropped quietly
    by a firewall. Readable is not always dead (a pending notification is
    readable too), so it pings rather than discarding. The readable check
    uses ``select.poll``, which is POSIX-only (the product runs on Linux),
    and counts a connection with no usable descriptor as readable. A
    brand-new connection is not pinged, as with pre-ping, and a disconnect
    raises the error the pre-ping raises, so the pool flushes its older
    connections and retries the same way. At checkin the listener stamps
    the time.
    ``BOW_PG_PING_LISTENER=off`` gives Postgres engines the plain pre-ping
    and no ping listeners (the read-only reset below stays). Both settings
    are read when an engine is built. One case is weaker than pre-ping: a
    peer that vanishes without a FIN or RST (a host crash, a failover that
    moves the address, a firewall that drops the link) within the idle
    window leaves a socket that is not readable, so one query fails after
    the TCP timeout where pre-ping would have retried it. libpq TCP
    keepalives in ``connect_args`` (``keepalives_idle`` and friends) would
    shorten that wait; they are not set here.
  * ``pool_recycle`` — many DBAs set an idle-session timeout; recycling below it
    avoids handing out a connection the server already closed.
  * The cache key includes credentials via the URI, so rotating a password
    naturally yields a new engine rather than reusing one authenticated as the
    old identity.
  * ``key_extra`` exists for connectors whose identity is not in the URI — the
    SQL Server Kerberos path binds a connection to whichever ccache was active
    during the handshake, so its engine must not be shared across ccaches.
  * ``pool_recycle`` is applied lazily at checkout, so on its own it never
    closes a connection nobody checks out — an idle reaper thread
    (``prune_idle_engines``) disposes pools unused past ``BOW_ENGINE_IDLE_TTL``
    so quiet sources drain to zero open server sessions.
  * Health probes must not keep pools warm (that's what stopped quiet pools
    from ever draining) — ``ephemeral()`` gives them throwaway NullPool
    engines that leave nothing behind.
  * A Postgres (psycopg2) connection goes back to the session default on
    checkin: after a ``postgresql_readonly`` read SQLAlchemy leaves psycopg2's
    ``readonly`` at ``False``, and the next plain transaction would start with
    ``BEGIN READ WRITE`` — refused on a hot standby, and overriding a database
    whose default is read-only.
"""

import logging
import os
import select
import threading
import time
from collections import OrderedDict
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Callable, Dict, Iterator, Optional, Tuple

import sqlalchemy
import sqlalchemy.event
from sqlalchemy import exc as sa_exc

logger = logging.getLogger(__name__)

# pyodbc ships with ODBC driver-manager pooling ON by default — a second pool
# *underneath* SQLAlchemy's that keeps the server session alive after
# ``close()``, invisible to pool_recycle, NullPool probes, and the idle reaper
# alike (verified against SQL Server 2022: with it on, a closed connection's
# session stays in sys.dm_exec_sessions). SQLAlchemy's pyodbc docs say to
# disable it when SQLAlchemy owns pooling. It must be set before the first
# connection, hence at import here — every pooled client imports this module.
try:
    import pyodbc as _pyodbc

    _pyodbc.pooling = False
except ImportError:
    pass

# Per-engine pool sizing. Deliberately small: this is a cap on how many
# connections BOW holds open against ONE source, and the whole point is to be
# gentle with a box that cannot take load. Concurrency beyond this queues.
# The pool's ceiling (5 + 7 = 12) matches the default app-wide concurrent-query
# limit (query_concurrency.DEFAULT_MAX_CONCURRENT_QUERIES): every slot of a
# connection may land on one worker. Overflow connections open only under load.
DEFAULT_POOL_SIZE = 5
DEFAULT_MAX_OVERFLOW = 7
DEFAULT_POOL_RECYCLE_S = 1800   # 30 min, under a typical idle-session timeout
DEFAULT_POOL_TIMEOUT_S = 30

# How many distinct engines to keep. An org with hundreds of connections should
# not hold pools open for all of them forever; the least-recently-used engine is
# disposed when the cache is full.
MAX_CACHED_ENGINES = 64

# How long an engine may sit unused before the reaper closes its idle pooled
# connections. ``pool_recycle`` alone cannot do this: SQLAlchemy applies it
# lazily at checkout, so on a quiet source a pooled connection simply sits
# open on the server forever — DBAs see hour-idle sessions whose last
# statement was the pre-ping ``SELECT 1``. 0 disables the reaper.
DEFAULT_IDLE_TTL_S = 900
_REAPER_INTERVAL_S = 60

# Engine kwargs that only QueuePool understands; stripped when building an
# ephemeral (NullPool) engine. Pool-agnostic kwargs such as ``creator`` and
# ``pool_recycle`` pass through untouched.
_QUEUEPOOL_ONLY_KWARGS = ("pool_size", "max_overflow", "pool_timeout", "pool_use_lifo")

# Postgres engines ping a pooled connection that sat idle longer than this
# (see the module docstring). Well under typical load-balancer and NAT idle
# timeouts, and under the reaper's TTL. Env, read when an engine is built:
# ``BOW_PG_IDLE_PING_S`` (seconds) overrides it; ``BOW_PG_PING_LISTENER=off``
# (or 0/false/no) drops the listener and gives Postgres the plain pre-ping.
DEFAULT_PG_IDLE_PING_S = 120.0
_PG_CHECKIN_AT = "bow_pg_checkin_at"

_lock = threading.Lock()
_engines: "OrderedDict[Tuple, sqlalchemy.engine.Engine]" = OrderedDict()
# Monotonic timestamp of each engine's last get_engine() hit, for the reaper.
_last_used: Dict[Tuple, float] = {}
_reaper_started = False

# When set (via ``ephemeral()``), get_engine returns throwaway NullPool
# engines instead of cached pooled ones, collecting them for disposal at
# context exit. A ContextVar rather than a module flag so concurrent
# requests in other threads/tasks keep their pooled engines.
_ephemeral_engines: "ContextVar[Optional[list]]" = ContextVar(
    "bow_engine_pool_ephemeral", default=None
)


def idle_ttl_s() -> float:
    """Idle TTL before pooled connections are reaped; <= 0 disables."""
    try:
        return float(os.environ.get("BOW_ENGINE_IDLE_TTL", DEFAULT_IDLE_TTL_S))
    except (TypeError, ValueError):
        return float(DEFAULT_IDLE_TTL_S)


def _pg_idle_ping_s(uri: Any) -> Optional[float]:
    """The idle threshold when `uri`'s engine gets the ping listener, else None.

    Decided here from the URL rather than by the caller: the cache key ignores
    keyword arguments, so a caller-passed choice could land on an engine
    built with the other one.
    """
    if os.environ.get("BOW_PG_PING_LISTENER", "on").strip().lower() in ("off", "0", "false", "no"):
        return None
    try:
        url = sqlalchemy.engine.make_url(uri)
        if url.get_backend_name() != "postgresql" or url.get_driver_name() != "psycopg2":
            return None
    except Exception:
        return None  # create_engine reports a bad URL
    try:
        return float(os.environ.get("BOW_PG_IDLE_PING_S", DEFAULT_PG_IDLE_PING_S))
    except (TypeError, ValueError):
        return DEFAULT_PG_IDLE_PING_S


def _readable(dbapi_connection: Any) -> bool:
    """Whether the connection's socket has something to read, or is gone.

    ``select.poll`` rather than ``select.select``, which fails on file
    descriptors above 1024. ``select.poll`` is POSIX-only; the product runs
    on Linux. With no usable descriptor (``fileno()`` fails or gives -1, or
    ``poll`` refuses it) the answer is "readable", so the ping decides.
    """
    try:
        fd = dbapi_connection.fileno()
        if fd < 0:
            return True
        poller = select.poll()
        poller.register(fd, select.POLLIN | select.POLLPRI)
        return bool(poller.poll(0))
    except Exception:
        return True


class _PingWhenNeeded:
    """The Postgres checkout/checkin listeners (see the module docstring)."""

    def __init__(self, dialect: Any, idle_s: float, clock: Callable[[], float] = time.monotonic):
        self._dialect = dialect
        self._idle_s = idle_s
        self._clock = clock

    def checkout(self, dbapi_connection: Any, connection_record: Any, connection_proxy: Any) -> None:
        checked_in_at = connection_record.info.get(_PG_CHECKIN_AT)
        if checked_in_at is None:
            return  # brand new; the pool clears ``info`` on every reconnect
        if self._clock() - checked_in_at <= self._idle_s and not _readable(dbapi_connection):
            return
        try:
            self._dialect.do_ping(dbapi_connection)
        except self._dialect.loaded_dbapi.Error as err:
            if self._dialect.is_disconnect(err, dbapi_connection, None):
                # What the pre-ping raises: the pool invalidates this and every
                # older connection, then retries with a new one.
                raise sa_exc.InvalidatePoolError() from err
            raise

    def checkin(self, dbapi_connection: Any, connection_record: Any) -> None:
        # The read-only flag goes back to the default in
        # `_reset_readonly_on_checkin`, which every psycopg2 Postgres engine
        # has, so the ping's autocommit toggle sends no SET.
        connection_record.info[_PG_CHECKIN_AT] = self._clock()


def _attach_pg_ping(eng: sqlalchemy.engine.Engine, idle_s: float) -> None:
    listener = _PingWhenNeeded(eng.dialect, idle_s)
    sqlalchemy.event.listen(eng, "checkout", listener.checkout)
    sqlalchemy.event.listen(eng, "checkin", listener.checkin)


@contextmanager
def ephemeral() -> Iterator[None]:
    """Make get_engine() hand out throwaway, unpooled engines inside the block.

    Health probes (``test_connection``) use this: their whole job is "can I
    establish a connection right now?", so reusing a warm pooled connection
    half-defeats the test — and, worse, the periodic status sweep re-warming
    the pool every few minutes was the only traffic on otherwise-quiet
    sources, leaving idle server sessions open indefinitely. Inside this
    context every engine is NullPool (the physical connection closes with
    ``conn.close()``) and is disposed when the block exits, so a probe leaves
    zero sessions behind on the server.
    """
    token = _ephemeral_engines.set([])
    try:
        yield
    finally:
        engines = _ephemeral_engines.get() or []
        _ephemeral_engines.reset(token)
        for eng in engines:
            try:
                eng.dispose()
            except Exception:
                pass


def _reset_readonly_on_checkin(dbapi_connection: Any, connection_record: Any) -> None:
    """Pool ``checkin`` listener: return a psycopg2 connection's ``readonly``
    to ``None``, the session default, for whoever checks it out next.

    SQLAlchemy 2.0's ``_ConnectionRecord.checkin`` runs the characteristic
    reset (``set_readonly(False)``) from ``finalize_callback`` before
    ``dispatch.checkin``, so this listener has the last word. With autocommit
    off psycopg2 only changes the flag locally and sends nothing.

    It must never raise: a raise here skips ``pool._return_conn`` and the
    pool loses the slot for good. On failure the connection is dropped.
    """
    try:
        if dbapi_connection is not None and not dbapi_connection.closed and dbapi_connection.readonly is not None:
            dbapi_connection.readonly = None
    except Exception as e:
        # The type only, like the other connection warnings.
        logger.warning("engine_pool.readonly_reset_failed; the connection is dropped error=%s", type(e).__name__)
        connection_record.invalidate(e)


def _is_psycopg2_postgres(uri: str) -> bool:
    """`postgresql://` (psycopg2 is SQLAlchemy's default driver) or
    `postgresql+psycopg2://`, resolved the way SQLAlchemy resolves them."""
    url = sqlalchemy.engine.make_url(uri)
    return url.get_backend_name() == "postgresql" and url.get_driver_name() == "psycopg2"


def _attach_listeners(eng: sqlalchemy.engine.Engine, uri: str,
                      on_connect: Optional[Callable[[Any, Any], None]]) -> None:
    """Before the engine is published, so no connection can be opened or
    returned without them."""
    if on_connect is not None:
        sqlalchemy.event.listen(eng, "connect", on_connect)
    if _is_psycopg2_postgres(uri):
        sqlalchemy.event.listen(eng, "checkin", _reset_readonly_on_checkin)


def _key(uri: str, connect_args: Optional[Dict[str, Any]], key_extra: Optional[str]) -> Tuple:
    # connect_args can hold unhashable values (ssl_context); identify by repr,
    # which is stable enough for cache identity and never used for anything else.
    ca = tuple(sorted((k, repr(v)) for k, v in (connect_args or {}).items()))
    return (uri, ca, key_extra or "")


def get_engine(
    uri: str,
    connect_args: Optional[Dict[str, Any]] = None,
    *,
    key_extra: Optional[str] = None,
    pool_size: int = DEFAULT_POOL_SIZE,
    max_overflow: int = DEFAULT_MAX_OVERFLOW,
    on_connect: Optional[Callable[[Any, Any], None]] = None,
    **engine_kwargs: Any,
) -> sqlalchemy.engine.Engine:
    """Return a pooled engine for `uri`, creating it once.

    Inside an ``ephemeral()`` block this instead returns a fresh NullPool
    engine that is never cached and is disposed at context exit.

    ``on_connect(dbapi_connection, connection_record)`` runs once on every new
    physical connection, before anything else uses it (the SQL Server client
    sets its per-session lock options there). It is attached when the engine
    is built and is NOT part of the cache key: a caller whose hook can vary
    must put what varies into ``key_extra``, or it gets an engine built with
    somebody else's hook.

    A Postgres engine on psycopg2 also gets a ``checkin`` listener that
    returns each connection to the session default (``readonly=None``), and,
    unless ``BOW_PG_PING_LISTENER=off``, the ping listeners instead of
    ``pool_pre_ping``.
    """
    bucket = _ephemeral_engines.get()
    if bucket is not None:
        kwargs = {
            k2: v for k2, v in engine_kwargs.items() if k2 not in _QUEUEPOOL_ONLY_KWARGS
        }
        kwargs["poolclass"] = sqlalchemy.pool.NullPool
        if connect_args:
            kwargs["connect_args"] = connect_args
        eng = sqlalchemy.create_engine(uri, **kwargs)
        _attach_listeners(eng, uri, on_connect)
        pg_idle_s = _pg_idle_ping_s(uri)
        if pg_idle_s is not None:
            _attach_pg_ping(eng, pg_idle_s)
        bucket.append(eng)
        return eng

    k = _key(uri, connect_args, key_extra)
    now = time.monotonic()
    with _lock:
        eng = _engines.get(k)
        if eng is not None:
            _engines.move_to_end(k)
            _last_used[k] = now
            return eng

    # Build outside the lock: create_engine can do real work (driver import,
    # DSN parsing) and must not serialize every other connection's lookup.
    pg_idle_s = _pg_idle_ping_s(uri)
    kwargs: Dict[str, Any] = {
        "pool_size": pool_size,
        "max_overflow": max_overflow,
        # Postgres engines get the ping listener instead (module docstring).
        "pool_pre_ping": pg_idle_s is None,
        "pool_recycle": DEFAULT_POOL_RECYCLE_S,
        "pool_timeout": DEFAULT_POOL_TIMEOUT_S,
        # LIFO checkout: steady light traffic keeps reusing the same warm
        # connection instead of round-robining through the whole pool, so the
        # extra connections actually go idle and the reaper can retire them.
        "pool_use_lifo": True,
    }
    kwargs.update(engine_kwargs)
    if connect_args:
        kwargs["connect_args"] = connect_args
    eng = sqlalchemy.create_engine(uri, **kwargs)
    _attach_listeners(eng, uri, on_connect)
    if pg_idle_s is not None:
        # Before publishing too: an engine without pre-ping must never hand
        # out a connection without the listener.
        _attach_pg_ping(eng, pg_idle_s)

    evicted = None
    with _lock:
        existing = _engines.get(k)
        if existing is not None:
            # Lost a race — keep the winner and throw ours away.
            eng.dispose()
            _engines.move_to_end(k)
            _last_used[k] = now
            return existing
        _engines[k] = eng
        _engines.move_to_end(k)
        _last_used[k] = now
        if len(_engines) > MAX_CACHED_ENGINES:
            evicted_key, evicted = _engines.popitem(last=False)
            _last_used.pop(evicted_key, None)

    _ensure_reaper()

    if evicted is not None:
        try:
            evicted.dispose()
        except Exception:
            pass
    return eng


def prune_idle_engines(max_idle_s: Optional[float] = None, *, _now: Optional[float] = None) -> int:
    """Close pooled connections of engines unused for longer than the TTL.

    ``pool_recycle`` only fires at checkout, so a pool nobody checks out from
    keeps its server sessions open forever. This walks the cache and calls
    ``Engine.dispose()`` on engines idle past the TTL — dispose replaces the
    engine's pool with a fresh empty one, so the engine stays cached and
    usable; the next checkout just pays one reconnect. Engines with a
    connection currently checked out (a long-running query) are skipped.

    Returns the number of engines whose pools were disposed.
    """
    ttl = idle_ttl_s() if max_idle_s is None else max_idle_s
    if ttl < 0 or (max_idle_s is None and ttl == 0):
        return 0
    now = time.monotonic() if _now is None else _now

    with _lock:
        stale = [
            (k, eng)
            for k, eng in _engines.items()
            if now - _last_used.get(k, now) >= ttl
        ]

    pruned = 0
    for k, eng in stale:
        pool = eng.pool
        try:
            if getattr(pool, "checkedout", lambda: 1)() > 0:
                continue  # in use — a running query's connection must survive
            if getattr(pool, "checkedin", lambda: 0)() <= 0:
                continue  # nothing held open; nothing to reap
            eng.dispose(close=True)
            pruned += 1
        except Exception:
            logger.exception("engine_pool.prune_failed", extra={"uri": k[0][:80]})
    if pruned:
        logger.info("engine_pool.pruned_idle", extra={"engines": pruned, "ttl_s": ttl})
    return pruned


def _ensure_reaper() -> None:
    """Start the per-process idle reaper thread once, lazily.

    A daemon thread rather than an APScheduler job: engine caches are
    per-process, and worker processes that never run the scheduler still need
    their pools drained.
    """
    global _reaper_started
    if _reaper_started or idle_ttl_s() <= 0:
        return
    with _lock:
        if _reaper_started:
            return
        _reaper_started = True
    t = threading.Thread(target=_reaper_loop, name="bow-engine-pool-reaper", daemon=True)
    t.start()


def _reaper_loop() -> None:
    while True:
        time.sleep(_REAPER_INTERVAL_S)
        try:
            prune_idle_engines()
        except Exception:
            logger.exception("engine_pool.reaper_tick_failed")


def dispose_for_uri(uri: str) -> int:
    """Drop every cached engine for `uri`.

    Called when a connection's config or credentials change, or it is deleted —
    otherwise a pool authenticated as the old identity would keep serving.
    """
    disposed = []
    with _lock:
        for k in [k for k in _engines if k[0] == uri]:
            disposed.append(_engines.pop(k))
            _last_used.pop(k, None)
    for eng in disposed:
        try:
            eng.dispose()
        except Exception:
            pass
    return len(disposed)


def dispose_all() -> None:
    """Test/shutdown helper."""
    with _lock:
        engines = list(_engines.values())
        _engines.clear()
        _last_used.clear()
    for eng in engines:
        try:
            eng.dispose()
        except Exception:
            pass


def stats() -> dict:
    with _lock:
        return {"cached_engines": len(_engines)}
