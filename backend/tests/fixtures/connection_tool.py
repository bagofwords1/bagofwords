"""
Connection tool API test fixtures for MCP/API tool management.
"""
import pytest


@pytest.fixture
def create_mcp_connection(test_client):
    """Create an MCP connection pointing to the mock server."""
    def _create_mcp_connection(
        *,
        name: str = "Test MCP",
        server_url: str = "http://mock-mcp:3000/mcp",
        transport: str = "sse",
        user_token: str = None,
        org_id: str = None,
    ):
        if user_token is None:
            pytest.fail("User token is required")
        if org_id is None:
            pytest.fail("Organization ID is required")

        payload = {
            "name": name,
            "type": "mcp",
            "config": {
                "server_url": server_url,
                "transport": transport,
            },
            "credentials": {},
            "auth_policy": "system_only",
        }

        headers = {
            "Authorization": f"Bearer {user_token}",
            "X-Organization-Id": str(org_id),
        }

        response = test_client.post("/api/connections", json=payload, headers=headers)
        assert response.status_code == 200, response.json()
        return response.json()

    return _create_mcp_connection


@pytest.fixture
def create_custom_api_connection(test_client):
    """Create a custom API connection."""
    def _create_custom_api_connection(
        *,
        name: str = "Test API",
        base_url: str = "https://api.example.com/v1",
        endpoints: list = None,
        user_token: str = None,
        org_id: str = None,
    ):
        if user_token is None:
            pytest.fail("User token is required")
        if org_id is None:
            pytest.fail("Organization ID is required")

        if endpoints is None:
            endpoints = [
                {
                    "name": "get_items",
                    "method": "GET",
                    "path": "/items",
                    "description": "List items",
                    "parameters": [
                        {"name": "limit", "in": "query", "type": "integer", "description": "Max results"}
                    ],
                }
            ]

        payload = {
            "name": name,
            "type": "custom_api",
            "config": {
                "base_url": base_url,
                "endpoints": endpoints,
            },
            "credentials": {},
            "auth_policy": "system_only",
        }

        headers = {
            "Authorization": f"Bearer {user_token}",
            "X-Organization-Id": str(org_id),
        }

        response = test_client.post("/api/connections", json=payload, headers=headers)
        assert response.status_code == 200, response.json()
        return response.json()

    return _create_custom_api_connection


@pytest.fixture
def refresh_connection_tools(test_client):
    """Trigger tool discovery for a connection."""
    def _refresh_connection_tools(
        *,
        connection_id: str,
        user_token: str = None,
        org_id: str = None,
        expect_status: int = 200,
    ):
        headers = {
            "Authorization": f"Bearer {user_token}",
            "X-Organization-Id": str(org_id),
        }
        response = test_client.post(
            f"/api/connections/{connection_id}/refresh-tools",
            headers=headers,
        )
        if expect_status:
            assert response.status_code == expect_status, response.text
        return response.json() if response.status_code == 200 else response

    return _refresh_connection_tools


@pytest.fixture
def get_connection_tools(test_client):
    """Get tools for a connection."""
    def _get_connection_tools(
        *,
        connection_id: str,
        user_token: str = None,
        org_id: str = None,
    ):
        headers = {
            "Authorization": f"Bearer {user_token}",
            "X-Organization-Id": str(org_id),
        }
        response = test_client.get(
            f"/api/connections/{connection_id}/tools",
            headers=headers,
        )
        assert response.status_code == 200, response.json()
        return response.json()

    return _get_connection_tools


@pytest.fixture
def update_connection_tool(test_client):
    """Update a single tool (enable/disable, policy)."""
    def _update_connection_tool(
        *,
        connection_id: str,
        tool_id: str,
        payload: dict,
        user_token: str = None,
        org_id: str = None,
    ):
        headers = {
            "Authorization": f"Bearer {user_token}",
            "X-Organization-Id": str(org_id),
        }
        response = test_client.put(
            f"/api/connections/{connection_id}/tools/{tool_id}",
            json=payload,
            headers=headers,
        )
        assert response.status_code == 200, response.json()
        return response.json()

    return _update_connection_tool


@pytest.fixture
def batch_update_connection_tools(test_client):
    """Batch enable/disable tools."""
    def _batch_update_connection_tools(
        *,
        connection_id: str,
        tool_ids: list,
        is_enabled: bool,
        user_token: str = None,
        org_id: str = None,
    ):
        headers = {
            "Authorization": f"Bearer {user_token}",
            "X-Organization-Id": str(org_id),
        }
        response = test_client.put(
            f"/api/connections/{connection_id}/tools/batch",
            json={"tool_ids": tool_ids, "is_enabled": is_enabled},
            headers=headers,
        )
        assert response.status_code == 200, response.json()
        return response.json()

    return _batch_update_connection_tools
