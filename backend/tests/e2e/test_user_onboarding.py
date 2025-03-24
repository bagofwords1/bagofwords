import pytest
from fastapi.testclient import TestClient
from main import app

@pytest.mark.e2e
def test_user_onboarding(create_user, login_user, create_organization):
    user = create_user()
    token = login_user(user["email"], user["password"])
    assert token is not None

    org_id = create_organization(user_token=token)
    assert org_id is not None