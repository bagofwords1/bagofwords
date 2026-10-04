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

@pytest.fixture
def backdate_organization():
    """Move an organization's ``created_at`` into the past.

    "All time" on the console starts on the org's creation day, and seeded
    runs (see ``seed_agent_executions``) are given arbitrary past timestamps,
    so a test that wants runs older than the org must age the org first. No
    API can change an org's creation time — a direct DB write is the only way.
    """
    def _backdate(org_id, days):
        import os
        from datetime import datetime, timedelta
        from sqlalchemy import create_engine, text

        url = os.environ["TEST_DATABASE_URL"]
        sync_url = url.replace("sqlite+aiosqlite:", "sqlite:").replace("postgresql+asyncpg:", "postgresql:")
        engine = create_engine(sync_url)
        created_at = datetime.utcnow() - timedelta(days=days)
        try:
            with engine.begin() as conn:
                conn.execute(
                    text("UPDATE organizations SET created_at = :created_at WHERE id = :id"),
                    {"created_at": created_at, "id": org_id},
                )
        finally:
            engine.dispose()
        return created_at

    return _backdate
