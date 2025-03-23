import pytest
from sqlalchemy import select
import json
from fastapi.testclient import TestClient
from main import app
import uuid

# Import the Plan model
from app.models.plan import Plan

# Add this decorator to all async test functions
pytestmark = pytest.mark.asyncio

def test_user_registration_e2e():
    """Test user registration through the API endpoint."""
    client = TestClient(app)
    
    # Generate a unique email to avoid conflicts in subsequent test runs
    unique_email = f"test-{uuid.uuid4()}@example.com"
    
    # User registration data
    user_data = {
        "name": "Test User",
        "email": unique_email,
        "password": "StrongPassword123!",
    }
    
    # Send POST request to registration endpoint
    response = client.post("/api/auth/register", json=user_data)
    # Assertions
    assert response.status_code == 201
    user = response.json()
    assert user["email"] == unique_email
    assert "id" in user
    assert "is_active" in user
    assert "hashed_password" not in user  # Password should not be returned
