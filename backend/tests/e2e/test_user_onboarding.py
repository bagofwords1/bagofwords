import pytest
from tests.utils.user_creds import main_user

@pytest.mark.e2e
def test_user_onboarding(create_user, login_user, whoami):
    # First create the user
    user = create_user()
    assert user is not None
    
    # Then try to login
    token = login_user(main_user["email"], main_user["password"])
    assert token is not None

    whoami = whoami(token)
    assert whoami["email"] == main_user["email"]
    assert whoami['organizations'][0]['id'] is not None
