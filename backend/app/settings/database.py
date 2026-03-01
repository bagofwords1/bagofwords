from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from app.settings.config import settings
from app.settings.db_auth import get_auth_provider
import logging
import os

logger = logging.getLogger(__name__)


def _set_sqlite_pragmas(dbapi_connection, connection_record):
    """Set SQLite pragmas for better concurrency handling."""
    cursor = dbapi_connection.cursor()
    # Wait up to 30 seconds for locks to be released
    cursor.execute("PRAGMA busy_timeout = 30000")
    cursor.close()


def _get_test_database_url() -> str:
    """Get test database URL from env var (set by conftest.py) or settings."""
    return os.environ.get("TEST_DATABASE_URL", settings.TEST_DATABASE_URL)


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
    """Build SSL connect_args for asyncpg (uses a different ssl param format)."""
    ssl_mode = db_config.auth.ssl_mode
    if not ssl_mode:
        return {}
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


def create_async_database_engine():
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
            # NullPool: no connection pooling - avoids stale connection issues with TestClient
            engine = create_async_engine(database_url, echo=False, future=True, poolclass=NullPool)
    else:
        db_config = settings.bow_config.database
        database_url = _get_async_database_url()

        if "postgresql+asyncpg" in database_url:
            connect_args = _get_async_ssl_connect_args(db_config) if db_config.uses_iam_auth else {}
            # PostgreSQL: use connection pooling for production
            engine = create_async_engine(
                database_url,
                echo=False,
                pool_size=5,           # connections per worker
                max_overflow=10,       # extra connections under load
                pool_timeout=30,       # wait time for connection
                pool_recycle=1800,     # recycle connections every 30min (avoids stale connections)
                pool_pre_ping=True,    # check connection health before use
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

    return engine


def create_async_session_factory():
    engine = create_async_database_engine()
    async_session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    return async_session
