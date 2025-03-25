import pytest
import uuid

@pytest.fixture
def create_organization(test_client):
    def _create_organization(name="Test Organization", user_token=None):
        if user_token is None:
            pytest.fail("User token is required for create_organization")
        
        # Create the organization
        response = test_client.post(
            "/api/organizations",
            json={"name": name},
            headers={"Authorization": f"Bearer {user_token}"}
        )
        
        assert response.status_code == 200, response.json()
        
        return response.json().get("id", None)
    
    return _create_organization

@pytest.fixture
def add_organization_member(test_client):
    def _add_member(organization_id, user_id, role_id="member", token=None):
        response = test_client.post(
            f"/api/organizations/{organization_id}/members",
            json={
                "user_id": user_id,
                "role_id": role_id
            },
            headers={"Authorization": f"Bearer {token}"}
        )
        
        assert response.status_code == 200, response.json()
        return response.json()
    
    return _add_member

@pytest.fixture
def get_organization_members(test_client):
    def _get_members(organization_id, token):
        response = test_client.get(
            f"/organizations/{organization_id}/members",
            headers={"Authorization": f"Bearer {token}"}
        )
        
        assert response.status_code == 200, response.json()
        return response.json()
    
    return _get_members

@pytest.fixture
def update_organization_member(test_client):
    def _update_member(organization_id, membership_id, role_id, token):
        response = test_client.put(
            f"/organizations/{organization_id}/members/{membership_id}",
            json={"role_id": role_id},
            headers={"Authorization": f"Bearer {token}"}
        )
        
        assert response.status_code == 200, response.json()
        return response.json()
    
    return _update_member

@pytest.fixture
def remove_organization_member(test_client):
    def _remove_member(organization_id, membership_id, token):
        response = test_client.delete(
            f"/organizations/{organization_id}/members/{membership_id}",
            headers={"Authorization": f"Bearer {token}"}
        )
        
        assert response.status_code == 204
        return True
    
    return _remove_member

@pytest.fixture
def get_user_organizations(test_client):
    def _get_organizations(token):
        response = test_client.get(
            "/api/organizations",
            headers={"Authorization": f"Bearer {token}"}
        )
        
        assert response.status_code == 200, response.json()
        return response.json()
    
    return _get_organizations