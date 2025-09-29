from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from app.settings.config import settings
import os

def create_database_engine():
    if settings.TESTING:
        database_url = settings.TEST_DATABASE_URL
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
        database_url = settings.TEST_DATABASE_URL.replace('sqlite:', 'sqlite+aiosqlite:')
        engine = create_async_engine(
            database_url,
            echo=False,
            future=True,
            # Required for SQLite to handle concurrent requests
            connect_args={"check_same_thread": False}
        )
        print("✅ Test database engine created")
    else:
        if "postgres" in settings.bow_config.database.url:
            database_url = settings.bow_config.database.url.replace(
                "postgres://", "postgresql+asyncpg://"
            ).replace(
                "postgresql://", "postgresql+asyncpg://"
            )
            print(f"🔍 Using PostgreSQL database: {database_url}")
        elif "sqlite" in settings.bow_config.database.url:
            database_url = settings.bow_config.database.url.replace(
                "sqlite://", "sqlite+aiosqlite://"
            )
            print(f"🔍 Using SQLite database: {database_url}")
        else:
            database_url = "sqlite+aiosqlite:///./app.db"
            print(f"🔍 Using default SQLite database: {database_url}")
        
        engine = create_async_engine(database_url, echo=False)

    return engine

def create_async_session_factory():
    engine = create_async_database_engine()
    async_session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    return async_session
