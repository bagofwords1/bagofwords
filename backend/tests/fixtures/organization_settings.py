import pytest


@pytest.fixture
def get_organization_settings(test_client):
    """Get organization settings."""
    def _get_organization_settings(user_token=None, org_id=None):
        if user_token is None:
            pytest.fail("User token is required for get_organization_settings")
        if org_id is None:
            pytest.fail("Organization ID is required for get_organization_settings")
        
        headers = {
            "Authorization": f"Bearer {user_token}",
            "X-Organization-Id": str(org_id)
        }
        
        response = test_client.get(
            "/api/organization/settings",
            headers=headers
        )
        
        assert response.status_code == 200, response.json()
        return response.json()
    
    return _get_organization_settings


@pytest.fixture
def update_organization_settings(test_client):
    """Update organization settings."""
    def _update_organization_settings(config, user_token=None, org_id=None):
        if user_token is None:
            pytest.fail("User token is required for update_organization_settings")
        if org_id is None:
            pytest.fail("Organization ID is required for update_organization_settings")
        
        headers = {
            "Authorization": f"Bearer {user_token}",
            "X-Organization-Id": str(org_id)
        }
        
        response = test_client.put(
            "/api/organization/settings",
            json={"config": config},
            headers=headers
        )
        
        assert response.status_code == 200, response.json()
        return response.json()
    
    return _update_organization_settings


@pytest.fixture
def upload_organization_icon(test_client):
    """Upload organization icon."""
    def _upload_organization_icon(icon_content, filename="icon.png", content_type="image/png", user_token=None, org_id=None):
        if user_token is None:
            pytest.fail("User token is required for upload_organization_icon")
        if org_id is None:
            pytest.fail("Organization ID is required for upload_organization_icon")
        
        headers = {
            "Authorization": f"Bearer {user_token}",
            "X-Organization-Id": str(org_id)
        }
        
        files = {"icon": (filename, icon_content, content_type)}
        
        response = test_client.post(
            "/api/organization/general/icon",
            files=files,
            headers=headers
        )
        
        assert response.status_code == 200, response.json()
        return response.json()
    
    return _upload_organization_icon


@pytest.fixture
def delete_organization_icon(test_client):
    """Delete organization icon."""
    def _delete_organization_icon(user_token=None, org_id=None):
        if user_token is None:
            pytest.fail("User token is required for delete_organization_icon")
        if org_id is None:
            pytest.fail("Organization ID is required for delete_organization_icon")
        
        headers = {
            "Authorization": f"Bearer {user_token}",
            "X-Organization-Id": str(org_id)
        }
        
        response = test_client.delete(
            "/api/organization/general/icon",
            headers=headers
        )
        
        assert response.status_code == 200, response.json()
        return response.json()
    
    return _delete_organization_icon


@pytest.fixture
def get_organization_icon(test_client):
    """Get organization icon by key."""
    def _get_organization_icon(icon_key):
        response = test_client.get(f"/api/general/icon/{icon_key}")
        return response
    
    return _get_organization_icon
