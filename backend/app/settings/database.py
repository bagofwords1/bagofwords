from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from app.settings.config import settings

def create_database_engine():
    if settings.TESTING:
        database_url = settings.bow_config.database.url  # Use SQLite for testing or as fallback
    else:
        if "postgres" in settings.bow_config.database.url:
            database_url = settings.bow_config.database.url.replace("postgres://", "postgresql://")
        elif "sqlite" in settings.bow_config.database.url:
            database_url = settings.bow_config.database.url
        else:
            database_url = "sqlite:///./app.db"  # Default fallback
    engine = create_engine(database_url)
    return engine

def create_session_factory():
    engine = create_database_engine()
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    return SessionLocal

def create_async_database_engine():
    if settings.TESTING:
        database_url = "sqlite+aiosqlite:///./app.db"  # Use async SQLite for testing or as fallback
        engine = create_async_engine(database_url, echo=True)
    else:
        if "postgres" in settings.bow_config.database.url:
            database_url = settings.bow_config.database.url.replace(
                "postgres://", "postgresql+asyncpg://"
            ).replace(
                "postgresql://", "postgresql+asyncpg://"
            )
            engine = create_async_engine(database_url, pool_size=50, max_overflow=20, echo=True)
        elif "sqlite" in settings.bow_config.database.url:
            database_url = settings.bow_config.database.url.replace(
                "sqlite://", "sqlite+aiosqlite://"
            )
            engine = create_async_engine(database_url, echo=True)
        else:
            database_url = "sqlite+aiosqlite:///./app.db"
            engine = create_async_engine(database_url, echo=True)

    return engine

def create_async_session_factory():
    engine = create_async_database_engine()
    async_session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    return async_session
