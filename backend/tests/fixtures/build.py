"""
Fixtures for Build API testing.

Provides helper functions for:
- Listing builds
- Getting build details
- Getting main build
- Getting build contents
- Comparing builds (diff)
- Rolling back builds
"""
import pytest


@pytest.fixture
def get_builds(test_client):
    """List builds for the organization."""
    def _get_builds(user_token=None, org_id=None, status=None, skip=0, limit=50):
        if user_token is None:
            pytest.fail("User token is required for get_builds")
        if org_id is None:
            pytest.fail("Organization ID is required for get_builds")
        
        headers = {
            "Authorization": f"Bearer {user_token}",
            "X-Organization-Id": str(org_id)
        }
        
        params = {"skip": skip, "limit": limit}
        if status:
            params["status"] = status
        
        response = test_client.get(
            "/api/builds",
            headers=headers,
            params=params
        )
        
        assert response.status_code == 200, response.json()
        return response.json()
    
    return _get_builds


@pytest.fixture
def get_build(test_client):
    """Get a single build by ID."""
    def _get_build(build_id, user_token=None, org_id=None):
        if user_token is None:
            pytest.fail("User token is required for get_build")
        if org_id is None:
            pytest.fail("Organization ID is required for get_build")
        
        headers = {
            "Authorization": f"Bearer {user_token}",
            "X-Organization-Id": str(org_id)
        }
        
        response = test_client.get(
            f"/api/builds/{build_id}",
            headers=headers
        )
        
        assert response.status_code == 200, response.json()
        return response.json()
    
    return _get_build


@pytest.fixture
def get_main_build(test_client):
    """Get the main (is_main=True) build for the organization."""
    def _get_main_build(user_token=None, org_id=None):
        if user_token is None:
            pytest.fail("User token is required for get_main_build")
        if org_id is None:
            pytest.fail("Organization ID is required for get_main_build")
        
        headers = {
            "Authorization": f"Bearer {user_token}",
            "X-Organization-Id": str(org_id)
        }
        
        response = test_client.get(
            "/api/builds/main",
            headers=headers
        )
        
        # May return 404 if no main build exists yet
        if response.status_code == 404:
            return None
        
        assert response.status_code == 200, response.json()
        return response.json()
    
    return _get_main_build


@pytest.fixture
def get_build_contents(test_client):
    """Get contents (instructions) of a build."""
    def _get_build_contents(build_id, user_token=None, org_id=None):
        if user_token is None:
            pytest.fail("User token is required for get_build_contents")
        if org_id is None:
            pytest.fail("Organization ID is required for get_build_contents")
        
        headers = {
            "Authorization": f"Bearer {user_token}",
            "X-Organization-Id": str(org_id)
        }
        
        response = test_client.get(
            f"/api/builds/{build_id}/contents",
            headers=headers
        )
        
        assert response.status_code == 200, response.json()
        data = response.json()
        # API returns {"items": [...], "total": N, ...}, extract items list
        if isinstance(data, dict) and "items" in data:
            return data["items"]
        return data
    
    return _get_build_contents


@pytest.fixture
def get_build_diff(test_client):
    """Compare two builds and get the diff."""
    def _get_build_diff(build_id, compare_to_build_id, user_token=None, org_id=None):
        if user_token is None:
            pytest.fail("User token is required for get_build_diff")
        if org_id is None:
            pytest.fail("Organization ID is required for get_build_diff")
        
        headers = {
            "Authorization": f"Bearer {user_token}",
            "X-Organization-Id": str(org_id)
        }
        
        response = test_client.get(
            f"/api/builds/{build_id}/diff",
            headers=headers,
            params={"compare_to": compare_to_build_id}
        )
        
        assert response.status_code == 200, response.json()
        return response.json()
    
    return _get_build_diff


@pytest.fixture
def get_build_diff_detailed(test_client):
    """Compare two builds and get detailed diff with instruction content."""
    def _get_build_diff_detailed(build_id, compare_to_build_id, user_token=None, org_id=None):
        if user_token is None:
            pytest.fail("User token is required for get_build_diff_detailed")
        if org_id is None:
            pytest.fail("Organization ID is required for get_build_diff_detailed")
        
        headers = {
            "Authorization": f"Bearer {user_token}",
            "X-Organization-Id": str(org_id)
        }
        
        response = test_client.get(
            f"/api/builds/{build_id}/diff/details",
            headers=headers,
            params={"compare_to": compare_to_build_id}
        )
        
        assert response.status_code == 200, response.json()
        return response.json()
    
    return _get_build_diff_detailed


@pytest.fixture
def rollback_build(test_client):
    """Rollback to a previous build."""
    def _rollback_build(build_id, user_token=None, org_id=None):
        if user_token is None:
            pytest.fail("User token is required for rollback_build")
        if org_id is None:
            pytest.fail("Organization ID is required for rollback_build")
        
        headers = {
            "Authorization": f"Bearer {user_token}",
            "X-Organization-Id": str(org_id)
        }
        
        response = test_client.post(
            f"/api/builds/{build_id}/rollback",
            headers=headers
        )
        
        assert response.status_code == 200, response.json()
        return response.json()
    
    return _rollback_build
