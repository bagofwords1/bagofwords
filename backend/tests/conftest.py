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
async def async_session() -> AsyncGenerator[AsyncSession, None]:
    """Fixture that creates a new database session for each test session."""
    async with test_async_engine.connect() as conn:
        await conn.run_sync(Base.metadata.create_all)  # Create all tables
        
        session_factory = test_async_session_factory
        async_session = session_factory()
        try:
            yield async_session
        finally:
            await async_session.close()
            
        # Drop all tables after tests
        await conn.run_sync(Base.metadata.drop_all)
    
    await test_async_engine.dispose()

@pytest.fixture(scope="function")
async def db_session():
    """Create a database session for a test."""
    # Use in-memory database for tests
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        echo=True
    )
    
    # Create all tables
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    
    # Create session
    async_session_maker = create_async_session_factory()
    session = async_session_maker()
    
    try:
        yield session
    finally:
        await session.close()
        
        # Drop all tables
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
        await engine.dispose() 