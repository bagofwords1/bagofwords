import pytest


@pytest.fixture
def create_api_key(test_client):
    def _create_api_key(user_token, name="Test API Key", expires_at=None):
        payload = {"name": name}
        if expires_at:
            payload["expires_at"] = expires_at
        
        response = test_client.post(
            "/api/api_keys",
            json=payload,
            headers={"Authorization": f"Bearer {user_token}"}
        )
        assert response.status_code == 200, response.json()
        return response.json()
    
    return _create_api_key


@pytest.fixture
def list_api_keys(test_client):
    def _list_api_keys(user_token):
        response = test_client.get(
            "/api/api_keys",
            headers={"Authorization": f"Bearer {user_token}"}
        )
        assert response.status_code == 200, response.json()
        return response.json()
    
    return _list_api_keys


@pytest.fixture
def delete_api_key(test_client):
    def _delete_api_key(key_id, user_token):
        response = test_client.delete(
            f"/api/api_keys/{key_id}",
            headers={"Authorization": f"Bearer {user_token}"}
        )
        assert response.status_code == 200, response.json()
        return response.json()
    
    return _delete_api_key


@pytest.fixture
def api_key_request(test_client):
    """Make an authenticated request using an API key instead of JWT."""
    def _api_key_request(method, url, api_key, org_id=None, **kwargs):
        headers = {"X-API-Key": api_key}
        if org_id:
            headers["X-Organization-Id"] = org_id
        
        response = getattr(test_client, method.lower())(
            url,
            headers=headers,
            **kwargs
        )
        return response
    
    return _api_key_request



