import pytest

@pytest.fixture
def login_user(test_client, create_user):
    def _login_user(email="testuser1@example.com", password="testpass"):
        response = test_client.post(
            "/api/auth/jwt/login",
            data={"username": email, "password": password}
        )

        assert response.status_code == 200, response.json()
        return response.json().get("access_token", None) 

    return _login_user