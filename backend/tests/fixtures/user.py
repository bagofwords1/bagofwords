import pytest
import uuid
from tests.utils.user_creds import main_user

@pytest.fixture
def create_user(test_client):
    def _create_user(name=None, email=main_user["email"], password=main_user["password"]):
        # Generate unique name if not provided to avoid UNIQUE constraint failures
        if name is None:
            name = f"testuser_{uuid.uuid4().hex[:8]}"
        response = test_client.post("/api/auth/register", json={"name": name, "email": email, "password": password})
        assert response.status_code == 201, response.json()
        return {"name": name, "email": email, "password": password}
    return _create_user

