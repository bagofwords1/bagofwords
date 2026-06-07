"""OAuth MCP server test fixtures."""

import pytest


@pytest.fixture
def create_oauth_client(test_client):
    """Create an OAuth MCP client for an organization."""
    def _create_oauth_client(user_token, org_id, name="Test Claude Web", redirect_uris=None):
        body = {"name": name}
        if redirect_uris is not None:
            body["redirect_uris"] = redirect_uris
        response = test_client.post(
            "/api/oauth/clients",
            json=body,
            headers={
                "Authorization": f"Bearer {user_token}",
                "X-Organization-Id": str(org_id),
            }
        )
        assert response.status_code == 200, response.json()
        return response.json()

    return _create_oauth_client


@pytest.fixture
def list_oauth_clients(test_client):
    """List OAuth MCP clients for an organization."""
    def _list_oauth_clients(user_token, org_id):
        response = test_client.get(
            "/api/oauth/clients",
            headers={
                "Authorization": f"Bearer {user_token}",
                "X-Organization-Id": str(org_id),
            }
        )
        assert response.status_code == 200, response.json()
        return response.json()

    return _list_oauth_clients


@pytest.fixture
def rotate_oauth_secret(test_client):
    """Rotate the client secret for an OAuth MCP client."""
    def _rotate(client_db_id, user_token, org_id):
        response = test_client.post(
            f"/api/oauth/clients/{client_db_id}/rotate",
            headers={
                "Authorization": f"Bearer {user_token}",
                "X-Organization-Id": str(org_id),
            }
        )
        assert response.status_code == 200, response.json()
        return response.json()

    return _rotate
