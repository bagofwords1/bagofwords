from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from app.settings.config import settings
import os


def _get_test_database_url() -> str:
    """Get test database URL from env var (set by conftest.py) or settings."""
    return os.environ.get("TEST_DATABASE_URL", settings.TEST_DATABASE_URL)


def create_database_engine():
    if settings.TESTING:
        database_url = _get_test_database_url()
        # Normalize postgres URL variants
        if "postgres" in database_url:
            database_url = database_url.replace("postgres://", "postgresql://")
            # NullPool for tests to avoid connection issues
            return create_engine(database_url, poolclass=NullPool)
        return create_engine(database_url)
    else:
        if "postgres" in settings.bow_config.database.url:
            database_url = settings.bow_config.database.url.replace("postgres://", "postgresql://")
        elif "sqlite" in settings.bow_config.database.url:
            database_url = settings.bow_config.database.url
        else:
            database_url = "sqlite:///./app.db"  # Default fallback
        return create_engine(database_url)


def create_session_factory():
    engine = create_database_engine()
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    return SessionLocal


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
                # Required for SQLite to handle concurrent requests
                connect_args={"check_same_thread": False}
            )
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
        if "postgres" in settings.bow_config.database.url:
            database_url = settings.bow_config.database.url.replace(
                "postgres://", "postgresql+asyncpg://"
            ).replace(
                "postgresql://", "postgresql+asyncpg://"
            )
        elif "sqlite" in settings.bow_config.database.url:
            database_url = settings.bow_config.database.url.replace(
                "sqlite://", "sqlite+aiosqlite://"
            )
        else:
            database_url = "sqlite+aiosqlite:///./app.db"
        
        engine = create_async_engine(database_url, echo=False)

    return engine


def create_async_session_factory():
    engine = create_async_database_engine()
    async_session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    return async_session
