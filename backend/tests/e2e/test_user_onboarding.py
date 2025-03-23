import pytest
from fastapi.testclient import TestClient
from main import app

@pytest.mark.e2e
def test_user_onboarding(create_user, login_user):
    user = create_user()
    token = login_user(user["email"], user["password"])
    assert token is not None