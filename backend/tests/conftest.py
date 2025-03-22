import asyncio
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy import create_engine
from typing import Generator, AsyncGenerator

from app.models.base import Base

from app.settings.config import settings
from app.settings.database import create_async_session_factory

# Override the database URL for testing
settings.TESTING = True
TEST_DATABASE_URL = "sqlite+aiosqlite:///./test.db"

# Create async engine for testing
test_async_engine = create_async_engine(
    TEST_DATABASE_URL,
    echo=True,
    future=True
)

# Create async session factory
test_async_session_factory = create_async_session_factory()

@pytest.fixture(scope="session")
def event_loop() -> Generator:
    """Create an instance of the default event loop for each test case."""
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()

@pytest.fixture(scope="session")
async def async_session_factory() -> AsyncGenerator:
    """Fixture that creates a session factory for the test session."""
    async with test_async_engine.connect() as conn:
        await conn.run_sync(Base.metadata.create_all)  # Create all tables
        
        try:
            # Yield the factory itself
            yield test_async_session_factory
        finally:
            # Drop all tables after tests
            await conn.run_sync(Base.metadata.drop_all)
    
    await test_async_engine.dispose()

@pytest.fixture(scope="function")
async def db_session(async_session_factory) -> AsyncGenerator[AsyncSession, None]:
    """Fixture that creates a new database session for each test function."""
    # Create a new session using the factory
    async_session = async_session_factory()
    try:
        yield async_session
        await async_session.commit()
    except Exception:
        await async_session.rollback()
        raise
    finally:
        await async_session.close() 