import time
import tempfile
import sqlite3
import os

import pytest  # type: ignore

@pytest.fixture
def create_data_source(test_client):
    def _create_data_source(
        *,
        name: str,
        type: str,
        config: dict = None,
        credentials: dict = None,
        auth_policy: str = "system_only",
        user_token: str = None,
        org_id: str = None
    ):
        if user_token is None:
            pytest.fail("User token is required for create_data_source")
        if org_id is None:
            pytest.fail("Organization ID is required for create_data_source")
        
        payload = {
            "name": name,
            "type": type,
            "config": config or {},
            "credentials": credentials or {},
            "auth_policy": auth_policy,
            "generate_summary": False,
            "generate_conversation_starters": False,
            "generate_ai_rules": False
        }
        
        headers = {
            "Authorization": f"Bearer {user_token}",
            "X-Organization-Id": str(org_id)
        }
        
        response = test_client.post(
            "/api/data_sources",
            json=payload,
            headers=headers
        )
        
        assert response.status_code == 200, response.json()
        return response.json()
    
    return _create_data_source

@pytest.fixture
def get_data_sources(test_client):
    def _get_data_sources(*, user_token: str = None, org_id: str = None):
        if user_token is None:
            pytest.fail("User token is required for get_data_sources")
        if org_id is None:
            pytest.fail("Organization ID is required for get_data_sources")
        
        headers = {
            "Authorization": f"Bearer {user_token}",
            "X-Organization-Id": str(org_id)
        }
        
        response = test_client.get(
            "/api/data_sources",
            headers=headers
        )
        
        assert response.status_code == 200, response.json()
        return response.json()
    
    return _get_data_sources

@pytest.fixture
def test_connection(test_client):  # Changed back to original name
    def _test_connection(*, data_source_id: str, user_token: str = None, org_id: str = None):
        if user_token is None:
            pytest.fail("User token is required for test_connection")
        if org_id is None:
            pytest.fail("Organization ID is required for test_connection")
        
        headers = {
            "Authorization": f"Bearer {user_token}",
            "X-Organization-Id": str(org_id)
        }
        
        response = test_client.get(
            f"/api/data_sources/{data_source_id}/test_connection",
            headers=headers
        )
        
        assert response.status_code == 200, response.json()
        return response.json()
    
    return _test_connection


@pytest.fixture
def update_data_source(test_client):
    def _update_data_source(*, data_source_id: str, payload: dict, user_token: str = None, org_id: str = None):
        if user_token is None:
            pytest.fail("User token is required for update_data_source")
        if org_id is None:
            pytest.fail("Organization ID is required for update_data_source")

        headers = {
            "Authorization": f"Bearer {user_token}",
            "X-Organization-Id": str(org_id)
        }

        response = test_client.put(
            f"/api/data_sources/{data_source_id}",
            json=payload,
            headers=headers
        )

        assert response.status_code == 200, response.json()
        return response.json()

    return _update_data_source


@pytest.fixture
def delete_data_source(test_client):
    def _delete_data_source(*, data_source_id: str, user_token: str = None, org_id: str = None):
        if user_token is None:
            pytest.fail("User token is required for delete_data_source")
        if org_id is None:
            pytest.fail("Organization ID is required for delete_data_source")

        headers = {
            "Authorization": f"Bearer {user_token}",
            "X-Organization-Id": str(org_id)
        }

        response = test_client.delete(
            f"/api/data_sources/{data_source_id}",
            headers=headers
        )

        assert response.status_code == 200, response.json()
        return response.json()

    return _delete_data_source


@pytest.fixture
def get_schema(test_client):
    def _get_schema(*, data_source_id: str, user_token: str = None, org_id: str = None):
        if user_token is None:
            pytest.fail("User token is required for get_schema")
        if org_id is None:
            pytest.fail("Organization ID is required for get_schema")

        headers = {
            "Authorization": f"Bearer {user_token}",
            "X-Organization-Id": str(org_id)
        }

        response = test_client.get(
            f"/api/data_sources/{data_source_id}/schema",
            headers=headers
        )

        assert response.status_code == 200, response.json()
        return response.json()

    return _get_schema


@pytest.fixture
def refresh_schema(test_client):
    def _refresh_schema(*, data_source_id: str, user_token: str = None, org_id: str = None):
        if user_token is None:
            pytest.fail("User token is required for refresh_schema")
        if org_id is None:
            pytest.fail("Organization ID is required for refresh_schema")

        headers = {
            "Authorization": f"Bearer {user_token}",
            "X-Organization-Id": str(org_id)
        }

        response = test_client.get(
            f"/api/data_sources/{data_source_id}/refresh_schema",
            headers=headers
        )

        assert response.status_code == 200, response.json()
        return response.json()

    return _refresh_schema


@pytest.fixture
def get_metadata_resources(test_client):
    def _get_metadata_resources(
        *,
        data_source_id: str,
        user_token: str = None,
        org_id: str = None,
    ):
        if user_token is None:
            pytest.fail("User token is required for get_metadata_resources")
        if org_id is None:
            pytest.fail("Organization ID is required for get_metadata_resources")

        headers = {
            "Authorization": f"Bearer {user_token}",
            "X-Organization-Id": str(org_id),
        }

        response = test_client.get(
            f"/api/data_sources/{data_source_id}/metadata_resources",
            headers=headers,
        )
        assert response.status_code == 200, response.json()
        return response.json()

    return _get_metadata_resources


@pytest.fixture
def update_metadata_resources(test_client):
    def _update_metadata_resources(
        *,
        data_source_id: str,
        resources: list,
        user_token: str = None,
        org_id: str = None,
    ):
        if user_token is None:
            pytest.fail("User token is required for update_metadata_resources")
        if org_id is None:
            pytest.fail("Organization ID is required for update_metadata_resources")

        headers = {
            "Authorization": f"Bearer {user_token}",
            "X-Organization-Id": str(org_id),
        }

        response = test_client.put(
            f"/api/data_sources/{data_source_id}/update_metadata_resources",
            json=resources,
            headers=headers,
        )

        assert response.status_code == 200, response.json()
        return response.json()

    return _update_metadata_resources


@pytest.fixture
def get_full_schema_paginated(test_client):
    """Get paginated full schema with filtering and sorting."""
    def _get_full_schema_paginated(
        *,
        data_source_id: str,
        user_token: str = None,
        org_id: str = None,
        page: int = 1,
        page_size: int = 100,
        schema_filter: str = None,
        search: str = None,
        sort_by: str = None,
        sort_dir: str = None,
        selected_state: str = None,
    ):
        if user_token is None:
            pytest.fail("User token is required")
        if org_id is None:
            pytest.fail("Organization ID is required")

        headers = {
            "Authorization": f"Bearer {user_token}",
            "X-Organization-Id": str(org_id),
        }

        params = {"page": page, "page_size": page_size}
        if schema_filter:
            params["schema_filter"] = schema_filter
        if search:
            params["search"] = search
        if sort_by:
            params["sort_by"] = sort_by
        if sort_dir:
            params["sort_dir"] = sort_dir
        if selected_state:
            params["selected_state"] = selected_state

        response = test_client.get(
            f"/api/data_sources/{data_source_id}/full_schema",
            params=params,
            headers=headers,
        )

        assert response.status_code == 200, response.json()
        return response.json()

    return _get_full_schema_paginated


@pytest.fixture
def bulk_update_tables(test_client):
    """Bulk activate/deactivate tables."""
    def _bulk_update_tables(
        *,
        data_source_id: str,
        action: str,
        filter: dict = None,
        user_token: str = None,
        org_id: str = None,
    ):
        if user_token is None:
            pytest.fail("User token is required")
        if org_id is None:
            pytest.fail("Organization ID is required")

        headers = {
            "Authorization": f"Bearer {user_token}",
            "X-Organization-Id": str(org_id),
        }

        response = test_client.post(
            f"/api/data_sources/{data_source_id}/bulk_update_tables",
            json={"action": action, "filter": filter},
            headers=headers,
        )

        assert response.status_code == 200, response.json()
        return response.json()

    return _bulk_update_tables


@pytest.fixture
def update_tables_status_delta(test_client):
    """Update table status using delta (activate/deactivate lists)."""
    def _update_tables_status_delta(
        *,
        data_source_id: str,
        activate: list = None,
        deactivate: list = None,
        user_token: str = None,
        org_id: str = None,
    ):
        if user_token is None:
            pytest.fail("User token is required")
        if org_id is None:
            pytest.fail("Organization ID is required")

        headers = {
            "Authorization": f"Bearer {user_token}",
            "X-Organization-Id": str(org_id),
        }

        response = test_client.put(
            f"/api/data_sources/{data_source_id}/update_tables_status",
            json={"activate": activate or [], "deactivate": deactivate or []},
            headers=headers,
        )

        assert response.status_code == 200, response.json()
        return response.json()

    return _update_tables_status_delta


# ============================================================================
# Demo Data Source Fixtures
# ============================================================================

@pytest.fixture
def list_demo_data_sources(test_client):
    """List all available demo data sources."""
    def _list_demo_data_sources(*, user_token: str = None, org_id: str = None):
        if user_token is None:
            pytest.fail("User token is required for list_demo_data_sources")
        if org_id is None:
            pytest.fail("Organization ID is required for list_demo_data_sources")

        headers = {
            "Authorization": f"Bearer {user_token}",
            "X-Organization-Id": str(org_id),
        }

        response = test_client.get(
            "/api/data_sources/demos",
            headers=headers,
        )

        assert response.status_code == 200, response.json()
        return response.json()

    return _list_demo_data_sources


@pytest.fixture
def install_demo_data_source(test_client):
    """Install a demo data source."""
    def _install_demo_data_source(*, demo_id: str, user_token: str = None, org_id: str = None):
        if user_token is None:
            pytest.fail("User token is required for install_demo_data_source")
        if org_id is None:
            pytest.fail("Organization ID is required for install_demo_data_source")

        headers = {
            "Authorization": f"Bearer {user_token}",
            "X-Organization-Id": str(org_id),
        }

        response = test_client.post(
            f"/api/data_sources/demos/{demo_id}",
            headers=headers,
        )

        assert response.status_code == 200, response.json()
        return response.json()

    return _install_demo_data_source


# ============================================================================
# Domain from Connection Fixtures
# ============================================================================

@pytest.fixture
def create_domain_from_connection(test_client):
    """Create a domain (data source) linked to an existing connection."""
    def _create_domain_from_connection(
        *,
        name: str,
        connection_id: str,
        user_token: str = None,
        org_id: str = None,
        use_llm_sync: bool = False,
        is_public: bool = True,
    ):
        if user_token is None:
            pytest.fail("User token is required for create_domain_from_connection")
        if org_id is None:
            pytest.fail("Organization ID is required for create_domain_from_connection")

        payload = {
            "name": name,
            "connection_id": connection_id,
            "generate_summary": False,
            "generate_conversation_starters": False,
            "generate_ai_rules": False,
            "use_llm_sync": use_llm_sync,
            "is_public": is_public,
        }

        headers = {
            "Authorization": f"Bearer {user_token}",
            "X-Organization-Id": str(org_id),
        }

        response = test_client.post(
            "/api/data_sources",
            json=payload,
            headers=headers,
        )

        assert response.status_code == 200, response.json()
        return response.json()

    return _create_domain_from_connection


@pytest.fixture
def create_domain_from_connections(test_client):
    """Create a domain (data source) linked to multiple existing connections."""
    def _create_domain_from_connections(
        *,
        name: str,
        connection_ids: list,
        user_token: str = None,
        org_id: str = None,
        use_llm_sync: bool = False,
        is_public: bool = True,
    ):
        if user_token is None:
            pytest.fail("User token is required for create_domain_from_connections")
        if org_id is None:
            pytest.fail("Organization ID is required for create_domain_from_connections")

        payload = {
            "name": name,
            "connection_ids": connection_ids,
            "generate_summary": False,
            "generate_conversation_starters": False,
            "generate_ai_rules": False,
            "use_llm_sync": use_llm_sync,
            "is_public": is_public,
        }

        headers = {
            "Authorization": f"Bearer {user_token}",
            "X-Organization-Id": str(org_id),
        }

        response = test_client.post(
            "/api/data_sources",
            json=payload,
            headers=headers,
        )

        assert response.status_code == 200, response.json()
        return response.json()

    return _create_domain_from_connections


# ============================================================================
# Dynamic SQLite Database Fixture (for schema drift tests)
# ============================================================================

@pytest.fixture
def dynamic_sqlite_db():
    """Create a temporary SQLite database that can be modified during tests.
    
    Yields the path to a SQLite database with initial tables:
    - users (id, name)
    - orders (id, user_id, amount)
    
    The database file is deleted after the test completes.
    """
    # Create a temporary file for the database
    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as f:
        db_path = f.name

    # Create initial schema
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT)")
    conn.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, user_id INTEGER, amount REAL)")
    conn.commit()
    conn.close()

    yield db_path

    # Cleanup - remove the temp database file
    try:
        os.unlink(db_path)
    except OSError:
        pass  # File may already be deleted


@pytest.fixture
def empty_sqlite_db():
    """Create an empty temporary SQLite database.
    
    Yields the path to an empty SQLite database (no tables).
    The database file is deleted after the test completes.
    """
    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as f:
        db_path = f.name

    # Just create the file, no tables
    conn = sqlite3.connect(db_path)
    conn.close()

    yield db_path

    # Cleanup
    try:
        os.unlink(db_path)
    except OSError:
        pass
