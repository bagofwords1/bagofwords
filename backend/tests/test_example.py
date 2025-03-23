import pytest
from sqlalchemy import select
import json

# Import the Plan model
from app.models.plan import Plan

# Add this decorator to all async test functions
pytestmark = pytest.mark.asyncio

async def test_create_and_read_plan(db_session):
    """Test creating and retrieving a plan from the database."""
    # Create a test plan with minimal data
    test_plan = Plan(
        content={"step1": "Do this", "step2": "Do that"},
        # Foreign keys are nullable, so we can omit them for this test
    )
    db_session.add(test_plan)
    await db_session.commit()
    
    # Refresh to get the generated ID
    await db_session.refresh(test_plan)
    plan_id = test_plan.id
    
    # Query the plan back
    stmt = select(Plan).where(Plan.id == plan_id)
    result = await db_session.execute(stmt)
    plan = result.scalars().first()
    
    # Assertions
    assert plan is not None
    assert plan.content["step1"] == "Do this"
    assert plan.content["step2"] == "Do that"
    assert plan.completion_id is None
    assert plan.user_id is None

async def test_update_plan(db_session):
    """Test updating a plan in the database."""
    # Create a test plan
    test_plan = Plan(
        content={"initial": "content"},
        organization_id="test-org-id"
    )
    db_session.add(test_plan)
    await db_session.commit()
    await db_session.refresh(test_plan)
    
    # Update the plan
    test_plan.content = {"updated": "content", "with": "new values"}
    await db_session.commit()
    
    # Query the plan back
    stmt = select(Plan).where(Plan.id == test_plan.id)
    result = await db_session.execute(stmt)
    updated_plan = result.scalars().first()
    
    # Assertions
    assert updated_plan.content["updated"] == "content"
    assert updated_plan.content["with"] == "new values"
    assert "initial" not in updated_plan.content
    assert updated_plan.organization_id == "test-org-id"

async def test_database_isolation(db_session):
    """Test that each test gets a fresh database."""
    # Check if plans table is empty at the start of the test
    stmt = select(Plan)
    result = await db_session.execute(stmt)
    plans = result.scalars().all()
    
    # Assert that we don't have data from the previous tests
    assert len(plans) == 0
    
    # Add a new plan
    new_plan = Plan(
        content={"isolation": "test"},
        report_id="test-report-id"
    )
    db_session.add(new_plan)
    await db_session.commit()
    
    # Verify this plan exists
    stmt = select(Plan)
    result = await db_session.execute(stmt)
    plans = result.scalars().all()
    assert len(plans) == 1
    assert plans[0].content["isolation"] == "test"
    assert plans[0].report_id == "test-report-id"
