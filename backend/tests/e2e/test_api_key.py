import pytest


@pytest.mark.e2e
def test_api_key_crud(
    create_api_key,
    list_api_keys,
    delete_api_key,
    create_user,
    login_user,
    whoami
):
    """Test creating, listing, and deleting API keys."""
    # Setup user
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    
    # Create an API key
    api_key = create_api_key(user_token=user_token, name="Test Key")
    
    # Verify key structure
    assert api_key is not None
    assert "id" in api_key
    assert "key" in api_key  # Full key only returned on creation
    assert "key_prefix" in api_key
    assert "name" in api_key
    assert "created_at" in api_key
    assert api_key["name"] == "Test Key"
    assert api_key["key"].startswith("bow_")
    assert api_key["key_prefix"] == api_key["key"][:12]
    
    # Store the full key for later tests
    full_key = api_key["key"]
    key_id = api_key["id"]
    
    # List API keys - should contain the created key
    keys = list_api_keys(user_token=user_token)
    assert isinstance(keys, list)
    assert len(keys) >= 1
    assert any(k["id"] == key_id for k in keys)
    
    # Listed keys should NOT contain the full key (security)
    listed_key = next(k for k in keys if k["id"] == key_id)
    assert "key" not in listed_key or listed_key.get("key") is None
    assert listed_key["key_prefix"] == full_key[:12]
    
    # Delete the API key
    delete_response = delete_api_key(key_id=key_id, user_token=user_token)
    assert delete_response.get("message") == "API key revoked"
    
    # Verify key no longer listed
    remaining_keys = list_api_keys(user_token=user_token)
    assert all(k["id"] != key_id for k in remaining_keys)


@pytest.mark.e2e
def test_api_key_authentication(
    create_api_key,
    api_key_request,
    create_user,
    login_user,
    whoami
):
    """Test that API keys can authenticate requests."""
    # Setup user and org
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    user_info = whoami(user_token)
    org_id = user_info['organizations'][0]['id']
    user_id = user_info['id']
    
    # Create an API key
    api_key = create_api_key(user_token=user_token, name="Auth Test Key")
    full_key = api_key["key"]
    
    # Use the API key to make an authenticated request
    response = api_key_request(
        method="GET",
        url="/api/users/whoami",
        api_key=full_key
    )
    
    assert response.status_code == 200
    response_data = response.json()
    assert response_data["id"] == user_id
    assert response_data["email"] == user["email"]


@pytest.mark.e2e
def test_invalid_api_key_rejected(
    api_key_request,
    create_user,
    login_user
):
    """Test that invalid API keys are rejected."""
    # Setup user (just to ensure DB is populated)
    user = create_user()
    login_user(user["email"], user["password"])
    
    # Try to authenticate with an invalid API key
    response = api_key_request(
        method="GET",
        url="/api/users/whoami",
        api_key="bow_invalid_key_that_does_not_exist"
    )
    
    assert response.status_code == 401


@pytest.mark.e2e
def test_deleted_api_key_rejected(
    create_api_key,
    delete_api_key,
    api_key_request,
    create_user,
    login_user
):
    """Test that deleted API keys are rejected."""
    # Setup user
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    
    # Create and then delete an API key
    api_key = create_api_key(user_token=user_token, name="To Be Deleted")
    full_key = api_key["key"]
    key_id = api_key["id"]
    
    # Verify it works before deletion
    response = api_key_request(
        method="GET",
        url="/api/users/whoami",
        api_key=full_key
    )
    assert response.status_code == 200
    
    # Delete the key
    delete_api_key(key_id=key_id, user_token=user_token)
    
    # Verify it's rejected after deletion
    response = api_key_request(
        method="GET",
        url="/api/users/whoami",
        api_key=full_key
    )
    assert response.status_code == 401


@pytest.mark.e2e
def test_multiple_api_keys(
    create_api_key,
    list_api_keys,
    delete_api_key,
    create_user,
    login_user
):
    """Test that users can have multiple API keys."""
    # Setup user
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    
    # Create multiple API keys
    key1 = create_api_key(user_token=user_token, name="Key 1")
    key2 = create_api_key(user_token=user_token, name="Key 2")
    key3 = create_api_key(user_token=user_token, name="Key 3")
    
    # List should show all three
    keys = list_api_keys(user_token=user_token)
    assert len(keys) >= 3
    
    key_ids = [k["id"] for k in keys]
    assert key1["id"] in key_ids
    assert key2["id"] in key_ids
    assert key3["id"] in key_ids
    
    # Delete one, others should remain
    delete_api_key(key_id=key2["id"], user_token=user_token)
    
    remaining = list_api_keys(user_token=user_token)
    remaining_ids = [k["id"] for k in remaining]
    assert key1["id"] in remaining_ids
    assert key2["id"] not in remaining_ids
    assert key3["id"] in remaining_ids