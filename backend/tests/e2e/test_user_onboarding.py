import pytest
from fastapi.testclient import TestClient
from main import app
from tests.utils.user_creds import main_user

@pytest.mark.e2e
def test_user_onboarding(create_user, login_user, create_organization):
    # First create the user
    user = create_user()
    assert user is not None
    
    # Then try to login
    token = login_user(main_user["email"], main_user["password"])
    assert token is not None

    # Finally create the organization
    org_id = create_organization(user_token=token)
    assert org_id is not None