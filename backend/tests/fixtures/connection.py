"""
Connection API test fixtures.
"""
import pytest


@pytest.fixture
def create_connection(test_client):
    """Create a new database connection."""
    def _create_connection(
        *,
        name: str,
        type: str,
        config: dict = None,
        credentials: dict = None,
        auth_policy: str = "system_only",
        allowed_user_auth_modes: list = None,
        user_token: str = None,
        org_id: str = None,
    ):
        if user_token is None:
            pytest.fail("User token is required for create_connection")
        if org_id is None:
            pytest.fail("Organization ID is required for create_connection")

        payload = {
            "name": name,
            "type": type,
            "config": config or {},
            "credentials": credentials or {},
            "auth_policy": auth_policy,
        }
        if allowed_user_auth_modes:
            payload["allowed_user_auth_modes"] = allowed_user_auth_modes

        headers = {
            "Authorization": f"Bearer {user_token}",
            "X-Organization-Id": str(org_id),
        }

        response = test_client.post(
            "/api/connections",
            json=payload,
            headers=headers,
        )

        assert response.status_code == 200, response.json()
        return response.json()

    return _create_connection


@pytest.fixture
def get_connections(test_client):
    """List all connections for the organization."""
    def _get_connections(*, user_token: str = None, org_id: str = None):
        if user_token is None:
            pytest.fail("User token is required for get_connections")
        if org_id is None:
            pytest.fail("Organization ID is required for get_connections")

        headers = {
            "Authorization": f"Bearer {user_token}",
            "X-Organization-Id": str(org_id),
        }

        response = test_client.get(
            "/api/connections",
            headers=headers,
        )

        assert response.status_code == 200, response.json()
        return response.json()

    return _get_connections


@pytest.fixture
def get_connection(test_client):
    """Get connection details by ID."""
    def _get_connection(*, connection_id: str, user_token: str = None, org_id: str = None):
        if user_token is None:
            pytest.fail("User token is required for get_connection")
        if org_id is None:
            pytest.fail("Organization ID is required for get_connection")

        headers = {
            "Authorization": f"Bearer {user_token}",
            "X-Organization-Id": str(org_id),
        }

        response = test_client.get(
            f"/api/connections/{connection_id}",
            headers=headers,
        )

        assert response.status_code == 200, response.json()
        return response.json()

    return _get_connection


@pytest.fixture
def update_connection(test_client):
    """Update a connection."""
    def _update_connection(
        *,
        connection_id: str,
        payload: dict,
        user_token: str = None,
        org_id: str = None,
    ):
        if user_token is None:
            pytest.fail("User token is required for update_connection")
        if org_id is None:
            pytest.fail("Organization ID is required for update_connection")

        headers = {
            "Authorization": f"Bearer {user_token}",
            "X-Organization-Id": str(org_id),
        }

        response = test_client.put(
            f"/api/connections/{connection_id}",
            json=payload,
            headers=headers,
        )

        assert response.status_code == 200, response.json()
        return response.json()

    return _update_connection


@pytest.fixture
def delete_connection(test_client):
    """Delete a connection."""
    def _delete_connection(
        *,
        connection_id: str,
        user_token: str = None,
        org_id: str = None,
        expect_success: bool = True,
    ):
        if user_token is None:
            pytest.fail("User token is required for delete_connection")
        if org_id is None:
            pytest.fail("Organization ID is required for delete_connection")

        headers = {
            "Authorization": f"Bearer {user_token}",
            "X-Organization-Id": str(org_id),
        }

        response = test_client.delete(
            f"/api/connections/{connection_id}",
            headers=headers,
        )

        if expect_success:
            assert response.status_code == 200, response.json()
        return response

    return _delete_connection


@pytest.fixture
def test_connection_connectivity(test_client):
    """Test a connection's connectivity."""
    def _test_connection_connectivity(
        *,
        connection_id: str,
        user_token: str = None,
        org_id: str = None,
    ):
        if user_token is None:
            pytest.fail("User token is required for test_connection_connectivity")
        if org_id is None:
            pytest.fail("Organization ID is required for test_connection_connectivity")

        headers = {
            "Authorization": f"Bearer {user_token}",
            "X-Organization-Id": str(org_id),
        }

        response = test_client.post(
            f"/api/connections/{connection_id}/test",
            headers=headers,
        )

        assert response.status_code == 200, response.json()
        return response.json()

    return _test_connection_connectivity


@pytest.fixture
def refresh_connection_schema(test_client):
    """Refresh schema for a connection (discover tables)."""
    def _refresh_connection_schema(
        *,
        connection_id: str,
        user_token: str = None,
        org_id: str = None,
    ):
        if user_token is None:
            pytest.fail("User token is required for refresh_connection_schema")
        if org_id is None:
            pytest.fail("Organization ID is required for refresh_connection_schema")

        headers = {
            "Authorization": f"Bearer {user_token}",
            "X-Organization-Id": str(org_id),
        }

        response = test_client.post(
            f"/api/connections/{connection_id}/refresh",
            headers=headers,
        )

        assert response.status_code == 200, response.json()
        return response.json()

    return _refresh_connection_schema


@pytest.fixture
def get_connection_tables(test_client):
    """Get tables for a connection."""
    def _get_connection_tables(
        *,
        connection_id: str,
        user_token: str = None,
        org_id: str = None,
    ):
        if user_token is None:
            pytest.fail("User token is required for get_connection_tables")
        if org_id is None:
            pytest.fail("Organization ID is required for get_connection_tables")

        headers = {
            "Authorization": f"Bearer {user_token}",
            "X-Organization-Id": str(org_id),
        }

        response = test_client.get(
            f"/api/connections/{connection_id}/tables",
            headers=headers,
        )

        assert response.status_code == 200, response.json()
        return response.json()

    return _get_connection_tables



@pytest.fixture
def wait_for_connection_indexing(test_client):
    """Wait until a connection's latest indexing run is no longer in flight.

    Creating a connection, or an agent on one, starts a background indexing
    run that keeps writing the catalog (tables, tools, agent tables) after
    the request returns. Snapshot what it writes only once it has finished.
    Returns the final run (or None if the connection has none).
    """
    import time

    def _wait(*, connection_id: str, user_token: str, org_id: str, timeout_s: float = 60.0):
        headers = {
            "Authorization": f"Bearer {user_token}",
            "X-Organization-Id": str(org_id),
        }
        deadline = time.monotonic() + timeout_s
        last = None
        while time.monotonic() < deadline:
            response = test_client.get(f"/api/connections/{connection_id}/indexing", headers=headers)
            if response.status_code == 404:
                return None
            assert response.status_code == 200, response.text
            last = response.json()
            if last["status"] in ("completed", "failed", "cancelled"):
                return last
            time.sleep(0.1)
        pytest.fail(f"indexing for {connection_id} still running after {timeout_s}s: {last!r}")

    return _wait
