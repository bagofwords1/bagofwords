import pytest


@pytest.mark.e2e
def test_get_available_mentions_sanity(get_available_mentions,
                                       create_user,
                                       login_user,
                                       whoami):

    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)['organizations'][0]['id']

    result = get_available_mentions(user_token=user_token, org_id=org_id)

    assert isinstance(result, dict)
    # Ensure all keys exist with list values
    assert "data_sources" in result
    assert "tables" in result
    assert "files" in result
    assert "entities" in result

    assert isinstance(result["data_sources"], list)
    assert isinstance(result["tables"], list)
    assert isinstance(result["files"], list)
    assert isinstance(result["entities"], list)


