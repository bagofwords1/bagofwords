from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from app.settings.config import settings
from app.settings.db_auth import get_auth_provider
from app.core.otel import instrument_db
import logging
import os

logger = logging.getLogger(__name__)


def _set_sqlite_pragmas(dbapi_connection, connection_record):
    """Set SQLite pragmas for better concurrency handling."""
    cursor = dbapi_connection.cursor()
    # Wait up to 30 seconds for locks to be released
    cursor.execute("PRAGMA busy_timeout = 30000")
    # WAL mode lets readers proceed concurrently with one writer instead of
    # blocking the whole DB on any open transaction. Sticks at the file
    # level, so the first connection sets it and the rest inherit. Cuts
    # 'database is locked' retries the agent's bg writers were hitting.
    cursor.execute("PRAGMA journal_mode = WAL")
    # NORMAL is the recommended sync level with WAL: durable across app
    # crashes, only loses on OS/power crash mid-fsync. FULL (default)
    # roughly doubles commit latency for marginal extra safety.
    cursor.execute("PRAGMA synchronous = NORMAL")
    cursor.close()


def _get_test_database_url() -> str:
    """Get test database URL from env var (set by conftest.py) or settings."""
    return os.environ.get("TEST_DATABASE_URL", settings.TEST_DATABASE_URL)


# Default pool geometry for the production async engine, per uvicorn worker.
# The effective ceiling against the database is
# ``(pool_size + max_overflow) * workers * replicas`` — size it under the
# server's ``max_connections - superuser_reserved_connections`` (stock Postgres
# is 100 - 3 = 97). Exceeding it does NOT surface as a QueuePool timeout: the
# server refuses the connect outright with "remaining connection slots are
# reserved for roles with the SUPERUSER attribute", which then surfaces to
# users as a failed agent run.
DEFAULT_DB_POOL_SIZE = 20
DEFAULT_DB_MAX_OVERFLOW = 20


def _env_pool_int(name: str, default: int) -> int:
    """Read a pool-geometry override from the environment.

    These were hardcoded, so an operator hitting connection-slot exhaustion had
    no lever short of editing the image. Invalid or negative values fall back to
    the default rather than failing startup.
    """
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        logger.warning("%s=%r is not an integer; using %d", name, raw, default)
        return default
    if value < 0:
        logger.warning("%s=%d is negative; using %d", name, value, default)
        return default
    return value


def _pool_size() -> int:
    return _env_pool_int("BOW_DB_POOL_SIZE", DEFAULT_DB_POOL_SIZE)


def _max_overflow() -> int:
    return _env_pool_int("BOW_DB_MAX_OVERFLOW", DEFAULT_DB_MAX_OVERFLOW)


def _idle_session_timeout_ms() -> int:
    """Server-side idle-session reaping, in ms. 0 (the default) disables it.

    A QueuePool never shrinks on its own: `pool_recycle` is applied lazily at
    checkout and *replaces* a stale connection rather than closing it, and
    SQLAlchemy has no idle-pool reaper. So once a burst has grown the pool to
    `pool_size`, that many Postgres backends stay open for the life of the
    worker. Measured here against a local PG16: after a 40-way burst the pool
    sat at 20 open backends indefinitely under light traffic, with or without
    `pool_use_lifo`.

    Letting the *server* close sessions idle past this timeout is what actually
    drains it; `pool_pre_ping` (enabled below) then detects the closed socket at
    next checkout and reconnects transparently. With the timeout at 5s and one
    query/second of traffic, the same pool settled at 4 open backends under FIFO
    checkout and 1 under LIFO.

    OFF BY DEFAULT because `idle_session_timeout` is PostgreSQL 14+, and an
    unrecognised GUC passed as a startup parameter makes *every* connection fail
    with `UndefinedObjectError` — so enabling it blindly would hard-break
    deployments on older servers. Operators on 14+ opt in with
    BOW_DB_IDLE_SESSION_TIMEOUT_MS (the Helm chart, which bundles PG 17, sets
    it). Keep it far longer than any gap between queries on a busy worker —
    minutes, not seconds — or the pool churns reconnects instead of draining.
    """
    return _env_pool_int("BOW_DB_IDLE_SESSION_TIMEOUT_MS", 0)


def _get_database_url() -> str:
    """Resolve the database URL from config, supporting IAM auth providers."""
    db = settings.bow_config.database
    url = db.get_url()
    if "postgres" in url:
        return url.replace("postgres://", "postgresql://")
    elif "sqlite" in url:
        return url
    return "sqlite:///./app.db"


def _get_ssl_connect_args(db_config) -> dict:
    """Build SSL connect_args for psycopg2 when ssl_mode is configured."""
    ssl_mode = db_config.auth.ssl_mode
    if not ssl_mode:
        return {}
    # psycopg2 uses sslmode (string), not ssl (context object)
    connect_args = {"sslmode": ssl_mode}
    if ssl_mode == "verify-full":
        rds_ca = "/app/certs/rds-combined-ca-bundle.pem"
        if os.path.exists(rds_ca):
            connect_args["sslrootcert"] = rds_ca
    return connect_args


def _attach_iam_auth_hook(engine, db_config):
    """Attach a connect event that injects a fresh IAM token as the password.

    This works for all cloud providers — the provider.get_password() call
    is the only cloud-specific part, and it's behind the protocol.
    """
    provider = get_auth_provider(db_config)
    host = db_config.host
    port = db_config.port
    username = db_config.username

    @event.listens_for(engine, "do_connect")
    def inject_token(dialect, conn_rec, cargs, cparams):
        cparams["password"] = provider.get_password(host, port, username)

    logger.info(
        "IAM auth hook attached (provider=%s, host=%s, user=%s)",
        db_config.auth.provider, host, username,
    )


def create_database_engine():
    if settings.TESTING:
        database_url = _get_test_database_url()
        # Normalize postgres URL variants
        if "postgres" in database_url:
            database_url = database_url.replace("postgres://", "postgresql://")
            # NullPool for tests to avoid connection issues
            return create_engine(database_url, poolclass=NullPool)
        return create_engine(database_url)

    database_url = _get_database_url()
    db_config = settings.bow_config.database

    connect_args = _get_ssl_connect_args(db_config) if db_config.uses_iam_auth else {}
    engine = create_engine(database_url, connect_args=connect_args)

    if db_config.uses_iam_auth:
        _attach_iam_auth_hook(engine, db_config)

    # Instrument with OpenTelemetry
    instrument_db(engine, settings.bow_config.otel)

    return engine


def create_session_factory():
    engine = create_database_engine()
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    return SessionLocal


def _get_async_database_url() -> str:
    """Resolve the async database URL (postgresql+asyncpg://)."""
    db = settings.bow_config.database
    url = db.get_url()
    if "postgres" in url:
        return url.replace(
            "postgres://", "postgresql+asyncpg://"
        ).replace(
            "postgresql://", "postgresql+asyncpg://"
        )
    elif "sqlite" in url:
        return url.replace("sqlite://", "sqlite+aiosqlite://")
    return "sqlite+aiosqlite:///./app.db"


def _get_async_ssl_connect_args(db_config) -> dict:
    """Build SSL connect_args for asyncpg (uses a different ssl param format).

    When ssl_mode is empty, explicitly pass ssl=False to prevent asyncpg from
    auto-detecting client certificates in ~/.postgresql/ (which fails on OCP
    where the container runs under an arbitrary UID that cannot read those files).
    """
    ssl_mode = db_config.auth.ssl_mode
    if not ssl_mode:
        return {"ssl": False}
    import ssl
    ssl_ctx = ssl.create_default_context()
    if ssl_mode == "verify-full":
        ssl_ctx.check_hostname = True
        ssl_ctx.verify_mode = ssl.CERT_REQUIRED
        rds_ca = "/app/certs/rds-combined-ca-bundle.pem"
        if os.path.exists(rds_ca):
            ssl_ctx.load_verify_locations(rds_ca)
    elif ssl_mode == "require":
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE
    return {"ssl": ssl_ctx}


def _attach_async_iam_auth_hook(engine, db_config):
    """Attach IAM auth hook to the async engine's underlying sync_engine."""
    provider = get_auth_provider(db_config)
    host = db_config.host
    port = db_config.port
    username = db_config.username

    @event.listens_for(engine.sync_engine, "do_connect")
    def inject_token(dialect, conn_rec, cargs, cparams):
        cparams["password"] = provider.get_password(host, port, username)

    logger.info(
        "Async IAM auth hook attached (provider=%s, host=%s, user=%s)",
        db_config.auth.provider, host, username,
    )


class _LoopLocalPool(NullPool):
    """Test-only pool: idle connections are kept per event loop and only ever
    handed out again on the loop that opened them.

    Every TestClient request (and every ``asyncio.run`` in a test) runs on its
    own short-lived loop, and asyncpg binds a connection to the loop that
    opened it — so a plain pool would pass connections across loops, which is
    why this used to be NullPool. NullPool paid a full connect for every
    session, and one request opens several.

    A connection is never touched from a loop other than its own while that
    loop is alive. Once its loop has closed it is abandoned: socket closed and
    marked aborted, since nothing can talk through a dead loop. ``test_client``
    releases the running loop's connections at the end of each request.
    """

    _MAX_IDLE_PER_LOOP = 5

    def __init__(self, *args, **kwargs):
        import threading

        super().__init__(*args, **kwargs)
        self._lock = threading.Lock()
        self._idle = {}  # id(loop) -> (weakref to loop, [records])

    def status(self) -> str:
        return "LoopLocalPool"

    @staticmethod
    def _running_loop():
        import asyncio

        try:
            return asyncio.get_running_loop()
        except RuntimeError:
            return None

    @staticmethod
    def _loop_of(record):
        ref = record.info.get("bow_loop")
        return ref() if ref is not None else None

    @staticmethod
    def _abandon(record):
        """Drop a connection whose loop has closed, without using that loop."""
        raw = getattr(record.dbapi_connection, "_connection", None)
        sock = getattr(getattr(raw, "_transport", None), "_sock", None)
        try:
            if sock is not None:
                sock.close()
        finally:
            if raw is not None:
                raw._aborted = True
                raw._protocol = None

    def _sweep_closed_loops(self):
        dead = []
        with self._lock:
            for key, (ref, records) in list(self._idle.items()):
                loop = ref()
                if loop is None or loop.is_closed():
                    dead.extend(records)
                    del self._idle[key]
        for record in dead:
            self._abandon(record)

    def _do_get(self):
        self._sweep_closed_loops()
        loop = self._running_loop()
        if loop is not None:
            with self._lock:
                entry = self._idle.get(id(loop))
                if entry is not None and entry[0]() is loop and entry[1]:
                    return entry[1].pop()
        return self._create_connection()

    def _do_return_conn(self, record):
        import weakref

        loop = self._loop_of(record)
        if loop is None or loop.is_closed():
            self._abandon(record)
            return
        if loop is self._running_loop():
            with self._lock:
                entry = self._idle.get(id(loop))
                if entry is None or entry[0]() is not loop:
                    entry = self._idle[id(loop)] = (weakref.ref(loop), [])
                if len(entry[1]) < self._MAX_IDLE_PER_LOOP:
                    entry[1].append(record)
                    return
        # Over the cap, or (never expected) returned off its own loop's thread:
        # close it on the loop we are on only when that loop owns it.
        if loop is self._running_loop():
            record.close()

    def release_running_loop(self):
        """Close the running loop's idle connections (call before it ends)."""
        loop = self._running_loop()
        if loop is None:
            return
        with self._lock:
            entry = self._idle.pop(id(loop), None)
        for record in (entry[1] if entry and entry[0]() is loop else []):
            record.close()

    def dispose(self):
        self.release_running_loop()
        self._sweep_closed_loops()
        # Idle connections of loops still running elsewhere are left to their
        # loop: closing them from here would act on another thread's loop.


def _tag_connection_loop(engine):
    import asyncio
    import weakref

    @event.listens_for(engine.sync_engine, "connect")
    def _remember_loop(dbapi_connection, connection_record):
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        connection_record.info["bow_loop"] = weakref.ref(loop) if loop else None


async def release_test_connections():
    """Test-only: close the pooled connections the running loop opened, while
    it can still close them (called at the end of each TestClient request)."""
    engine = _async_engine_singleton
    if engine is None:
        return
    pool = engine.sync_engine.pool
    if isinstance(pool, _LoopLocalPool):
        from sqlalchemy.util import greenlet_spawn

        await greenlet_spawn(pool.release_running_loop)


# Singleton async engine. Every call site that previously called
# `create_async_database_engine()` (or the wrapper `create_async_session_factory()`)
# was building a brand-new engine + 15-conn pool that never got disposed.
# In production this leaks idle Postgres backends until the singleton pool
# (via dependencies.async_session_maker) starves and `get_current_organization`
# starts timing out at 30s. We cache here and let session_factory rebind.
_async_engine_singleton = None


def _build_async_database_engine():
    if settings.TESTING:
        database_url = _get_test_database_url()

        if "sqlite" in database_url:
            # SQLite: use aiosqlite driver with special connect_args
            database_url = database_url.replace('sqlite:', 'sqlite+aiosqlite:')
            engine = create_async_engine(
                database_url,
                echo=False,
                future=True,
                # NullPool: close connections immediately to avoid "database is locked" in CI
                poolclass=NullPool,
                connect_args={
                    "check_same_thread": False,
                    # Timeout in seconds to wait for database lock
                    "timeout": 30,
                }
            )
            # Register event listener to set busy_timeout pragma on each connection
            event.listen(engine.sync_engine, "connect", _set_sqlite_pragmas)
        else:
            # PostgreSQL: use asyncpg driver with NullPool to avoid connection issues
            database_url = database_url.replace(
                "postgres://", "postgresql+asyncpg://"
            ).replace(
                "postgresql://", "postgresql+asyncpg://"
            )
            # Pooled per event loop (see _LoopLocalPool): an asyncpg
            # connection cannot cross loops, which is why this used to be
            # NullPool — but NullPool paid a full connect for every session,
            # and connecting was the largest share of the Postgres e2e leg's
            # wall time. Like NullPool it never blocks a checkout.
            # idle_in_transaction_session_timeout: if a leaked session stays idle
            # in transaction for >60s, Postgres terminates it automatically — keeps
            # a single bug from blocking DROP SCHEMA in the next test for hours.
            # synchronous_commit=off: a commit returns without waiting for its
            # WAL to reach disk. Visibility and isolation are unchanged; only
            # crash durability is given up, which a test database never needs.
            engine = create_async_engine(
                database_url,
                echo=False,
                future=True,
                poolclass=_LoopLocalPool,
                connect_args={
                    "server_settings": {
                        "idle_in_transaction_session_timeout": "60000",
                        "synchronous_commit": "off",
                    },
                },
            )
            _tag_connection_loop(engine)
    else:
        db_config = settings.bow_config.database
        database_url = _get_async_database_url()

        if "postgresql+asyncpg" in database_url:
            connect_args = _get_async_ssl_connect_args(db_config)
            # Server-side safety timeouts. Without these, any transaction that
            # holds a row lock (e.g. the agent's long-lived shared
            # agent-execution transaction, or a leaked/abandoned session) blocks
            # every other writer *forever* — Postgres defaults all three to 0
            # (no limit). The instruction build system is especially exposed:
            # promoting a build flips `is_main` on a single shared
            # `instruction_builds` row, so one stuck transaction stalls every
            # create/publish org-wide. Bounding them turns an unbounded hang into
            # a fast, retryable error and lets leaked transactions self-reap.
            #   - lock_timeout: a statement waits at most 30s for a lock, then
            #     errors instead of hanging indefinitely. Only affects *waiters*,
            #     never the lock holder, so legitimate long agent runs are safe.
            #   - idle_in_transaction_session_timeout: Postgres terminates a
            #     session left idle *inside a transaction* for >5min, releasing
            #     its locks. 5min is far longer than any healthy agent step, so
            #     it reaps genuine leaks without killing real work.
            server_settings = connect_args.setdefault("server_settings", {})
            server_settings.setdefault("lock_timeout", "30000")
            server_settings.setdefault("idle_in_transaction_session_timeout", "300000")
            #   - idle_session_timeout: lets the server close sessions the pool
            #     is holding but not using, so a quiet worker's backends drain
            #     instead of pinning `pool_size` slots forever. Off unless the
            #     operator opts in — see `_idle_session_timeout_ms` for why
            #     (PG 14+ only; an unknown GUC here fails every connect).
            _idle_ms = _idle_session_timeout_ms()
            if _idle_ms > 0:
                server_settings.setdefault("idle_session_timeout", str(_idle_ms))
            # PostgreSQL: use connection pooling for production
            # Pool sizing assumes one uvicorn worker. Each in-flight SSE
            # completion holds the agent's primary session for its entire
            # main loop (~30s per `create_data`), plus 2-3 short-lived
            # background-task sessions. 5+10=15 was too tight even at
            # ~5 concurrent users; 20+20 keeps headroom while still bounded.
            # Scale `pool_size * num_workers` against your DB's max_connections.
            engine = create_async_engine(
                database_url,
                echo=False,
                pool_size=_pool_size(),          # connections per worker
                max_overflow=_max_overflow(),    # extra under load
                pool_timeout=30,       # wait time for connection
                pool_recycle=1800,     # recycle every 30min (avoids stale connections)
                pool_pre_ping=True,    # check connection health before use
                # LIFO checkout: hand back the most recently used connection
                # instead of round-robining through the whole pool, so steady
                # light traffic concentrates on a few connections and the tail
                # goes genuinely idle. On its own this does NOT reduce open
                # backends (nothing closes a pooled connection) — it multiplies
                # what `idle_session_timeout` above can reclaim: measured on a
                # 20-connection pool under one query/second, FIFO settled at 4
                # open backends and LIFO at 1. Mirrors the data-source pool,
                # which sets it for the same reason
                # (data_sources/engine_pool.py).
                pool_use_lifo=True,
                connect_args=connect_args,
            )
            if db_config.uses_iam_auth:
                _attach_async_iam_auth_hook(engine, db_config)
        else:
            # SQLite: no connection pooling supported
            if "sqlite" in database_url:
                pass  # already converted by _get_async_database_url
            else:
                database_url = "sqlite+aiosqlite:///./app.db"
            engine = create_async_engine(database_url, echo=False)
            # Apply busy_timeout + WAL pragmas on every new connection
            # (matches the testing branch wiring at line 184).
            event.listen(engine.sync_engine, "connect", _set_sqlite_pragmas)

    # Instrument with OpenTelemetry
    instrument_db(engine, settings.bow_config.otel)

    return engine


def create_async_database_engine():
    """Return the process-wide async engine, building it on first call.

    The conftest test fixture calls `engine.dispose()` between tests; that's
    fine — dispose closes the existing connections but leaves the engine
    reusable, so we keep the same instance. If a future test fixture needs
    a truly fresh engine it can call `reset_async_engine_singleton()`.
    """
    global _async_engine_singleton
    if _async_engine_singleton is None:
        _async_engine_singleton = _build_async_database_engine()
    return _async_engine_singleton


def create_async_database_engine_for_indexing():
    """Dedicated NullPool async engine for the connection-indexing background
    loop. Mirrors the main engine's URL / IAM / SSL / sqlite pragma wiring,
    but forces NullPool so connections never get shared across event loops.

    Rationale: the main engine pools asyncpg connections, and each pooled
    connection binds to the first loop that uses it. Indexing runs on a
    daemon-thread event loop separate from FastAPI's; sharing the main pool
    between them causes 'attached to a different loop' / 'unknown protocol
    state' crashes. NullPool opens a fresh connection on the caller's loop
    for every session, so cross-loop is structurally impossible.
    """
    if settings.TESTING:
        database_url = _get_test_database_url()
        if "sqlite" in database_url:
            database_url = database_url.replace('sqlite:', 'sqlite+aiosqlite:')
            engine = create_async_engine(
                database_url,
                echo=False,
                future=True,
                poolclass=NullPool,
                connect_args={"check_same_thread": False, "timeout": 30},
            )
            event.listen(engine.sync_engine, "connect", _set_sqlite_pragmas)
        else:
            database_url = database_url.replace(
                "postgres://", "postgresql+asyncpg://"
            ).replace(
                "postgresql://", "postgresql+asyncpg://"
            )
            engine = create_async_engine(
                database_url,
                echo=False,
                future=True,
                poolclass=NullPool,
                connect_args={
                    "server_settings": {
                        "idle_in_transaction_session_timeout": "60000",
                    },
                },
            )
    else:
        db_config = settings.bow_config.database
        database_url = _get_async_database_url()
        if "postgresql+asyncpg" in database_url:
            connect_args = _get_async_ssl_connect_args(db_config)
            engine = create_async_engine(
                database_url,
                echo=False,
                future=True,
                poolclass=NullPool,
                connect_args=connect_args,
            )
            if db_config.uses_iam_auth:
                _attach_async_iam_auth_hook(engine, db_config)
        else:
            if "sqlite" not in database_url:
                database_url = "sqlite+aiosqlite:///./app.db"
            engine = create_async_engine(
                database_url,
                echo=False,
                future=True,
                poolclass=NullPool,
                connect_args={"check_same_thread": False, "timeout": 30},
            )
            event.listen(engine.sync_engine, "connect", _set_sqlite_pragmas)

    instrument_db(engine, settings.bow_config.otel)
    return engine


def reset_async_engine_singleton():
    """Drop the cached engine. Tests use this when they need a fresh one."""
    global _async_engine_singleton
    _async_engine_singleton = None


def create_async_session_factory():
    engine = create_async_database_engine()
    async_session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    return async_session
