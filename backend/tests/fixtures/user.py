import pytest

@pytest.fixture
def create_user(test_client):
    def _create_user(name="testuser", email="testuser1@example.com", password="testpass"):
        response = test_client.post("/api/auth/register", json={"name": name, "email": email, "password": password})
        assert response.status_code == 201, response.json()
        return {"name": name, "email": email, "password": password}
    return _create_user