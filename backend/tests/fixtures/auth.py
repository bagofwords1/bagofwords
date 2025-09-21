import pytest
from tests.utils.user_creds import main_user


@pytest.fixture
def login_user(test_client, create_user):
    def _login_user(email=main_user["email"], password=main_user["password"]):
        response = test_client.post(
            "/api/auth/jwt/login",
            data={"username": email, "password": password}
        )

        assert response.status_code == 200, response.json()
        return response.json().get("access_token", None) 

    return _login_user

@pytest.fixture
def whoami(test_client):
    def _whoami(token):
        response = test_client.get("/api/users/whoami", headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 200, response.json()
        return response.json()
    return _whoami