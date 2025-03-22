import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from typing import AsyncGenerator
from app.models.base import Base

class TestBase:
    @pytest.fixture(autouse=True)
    async def setup_test(self, db_session):
        """Setup the test with a database session"""
        self.db_session = db_session
        yield
        # Optional cleanup after test
        await self.cleanup_db()
        
    async def cleanup_db(self):
        """Helper method to clean up the database after tests"""
        if hasattr(self, 'db_session'):
            for table in reversed(Base.metadata.sorted_tables):
                await self.db_session.execute(table.delete())
            await self.db_session.commit() 