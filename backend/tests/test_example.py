import pytest
from app.models.plan import Plan

@pytest.mark.asyncio
class TestYourFeature:
    async def test_create_item(self, db_session):
        # Create a test item
        test_item = Plan(content={"test": "test"})
        db_session.add(test_item)
        await db_session.commit()
        
        # Query the item
        result = await db_session.get(Plan, test_item.id)
        assert result.content == {"test": "test"} 