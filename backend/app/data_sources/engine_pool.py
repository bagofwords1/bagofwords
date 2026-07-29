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
  * ``pool_recycle`` — many DBAs set an idle-session timeout; recycling below it
    avoids handing out a connection the server already closed.
  * The cache key includes credentials via the URI, so rotating a password
    naturally yields a new engine rather than reusing one authenticated as the
    old identity.
  * ``key_extra`` exists for connectors whose identity is not in the URI — the
    SQL Server Kerberos path binds a connection to whichever ccache was active
    during the handshake, so its engine must not be shared across ccaches.
"""

import logging
import threading
from collections import OrderedDict
from typing import Any, Dict, Optional, Tuple

import sqlalchemy

logger = logging.getLogger(__name__)

# Per-engine pool sizing. Deliberately small: this is a cap on how many
# connections BOW holds open against ONE source, and the whole point is to be
# gentle with a box that cannot take load. Concurrency beyond this queues.
DEFAULT_POOL_SIZE = 5
DEFAULT_MAX_OVERFLOW = 5
DEFAULT_POOL_RECYCLE_S = 1800   # 30 min, under a typical idle-session timeout
DEFAULT_POOL_TIMEOUT_S = 30

# How many distinct engines to keep. An org with hundreds of connections should
# not hold pools open for all of them forever; the least-recently-used engine is
# disposed when the cache is full.
MAX_CACHED_ENGINES = 64

_lock = threading.Lock()
_engines: "OrderedDict[Tuple, sqlalchemy.engine.Engine]" = OrderedDict()


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
    **engine_kwargs: Any,
) -> sqlalchemy.engine.Engine:
    """Return a pooled engine for `uri`, creating it once."""
    k = _key(uri, connect_args, key_extra)
    with _lock:
        eng = _engines.get(k)
        if eng is not None:
            _engines.move_to_end(k)
            return eng

    # Build outside the lock: create_engine can do real work (driver import,
    # DSN parsing) and must not serialize every other connection's lookup.
    kwargs: Dict[str, Any] = {
        "pool_size": pool_size,
        "max_overflow": max_overflow,
        "pool_pre_ping": True,
        "pool_recycle": DEFAULT_POOL_RECYCLE_S,
        "pool_timeout": DEFAULT_POOL_TIMEOUT_S,
    }
    kwargs.update(engine_kwargs)
    if connect_args:
        kwargs["connect_args"] = connect_args
    eng = sqlalchemy.create_engine(uri, **kwargs)

    evicted = None
    with _lock:
        existing = _engines.get(k)
        if existing is not None:
            # Lost a race — keep the winner and throw ours away.
            eng.dispose()
            _engines.move_to_end(k)
            return existing
        _engines[k] = eng
        _engines.move_to_end(k)
        if len(_engines) > MAX_CACHED_ENGINES:
            _, evicted = _engines.popitem(last=False)

    if evicted is not None:
        try:
            evicted.dispose()
        except Exception:
            pass
    return eng


def dispose_for_uri(uri: str) -> int:
    """Drop every cached engine for `uri`.

    Called when a connection's config or credentials change, or it is deleted —
    otherwise a pool authenticated as the old identity would keep serving.
    """
    disposed = []
    with _lock:
        for k in [k for k in _engines if k[0] == uri]:
            disposed.append(_engines.pop(k))
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
    for eng in engines:
        try:
            eng.dispose()
        except Exception:
            pass


def stats() -> dict:
    with _lock:
        return {"cached_engines": len(_engines)}
