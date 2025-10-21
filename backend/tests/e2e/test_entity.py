import pytest


@pytest.mark.e2e
def test_get_entities_empty_list(create_user, login_user, whoami, get_entities):
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]

    entities = get_entities(user_token=token, org_id=org_id)
    assert isinstance(entities, list)
    assert len(entities) == 0


@pytest.mark.e2e
def test_get_entity_404(create_user, login_user, whoami, get_entity):
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]

    response = get_entity("non-existent-id", user_token=token, org_id=org_id)
    assert response.status_code == 404
    assert response.json()["detail"] == "Entity not found or access denied"

