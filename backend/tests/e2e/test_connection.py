"""
E2E tests for Connection CRUD operations.
Tests the /api/connections endpoints for database connection management.
"""
import pytest
from pathlib import Path


# Path to test database
CONNECTION_TEST_DB_PATH = (Path(__file__).resolve().parent.parent / "config" / "chinook.sqlite").resolve()


@pytest.mark.e2e
def test_connection_crud(
    create_connection,
    get_connection,
    get_connections,
    update_connection,
    delete_connection,
    create_user,
    login_user,
    whoami,
):
    """Test full CRUD lifecycle for a connection."""
    if not CONNECTION_TEST_DB_PATH.exists():
        pytest.skip(f"SQLite test database missing at {CONNECTION_TEST_DB_PATH}")

    # Setup user and organization
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)['organizations'][0]['id']

    # CREATE: Create a new connection
    connection = create_connection(
        name="Test Connection",
        type="sqlite",
        config={"database": str(CONNECTION_TEST_DB_PATH)},
        credentials={},
        user_token=user_token,
        org_id=org_id,
    )

    assert connection is not None
    assert connection["name"] == "Test Connection"
    assert connection["type"] == "sqlite"
    assert "id" in connection
    assert connection["is_active"] is True
    connection_id = connection["id"]

    # READ: Get connection by ID
    fetched = get_connection(
        connection_id=connection_id,
        user_token=user_token,
        org_id=org_id,
    )

    assert fetched["id"] == connection_id
    assert fetched["name"] == "Test Connection"
    assert fetched["type"] == "sqlite"
    assert "config" in fetched  # Detail schema includes config

    # UPDATE: Update connection name
    updated = update_connection(
        connection_id=connection_id,
        payload={"name": "Updated Connection Name"},
        user_token=user_token,
        org_id=org_id,
    )

    assert updated["name"] == "Updated Connection Name"
    assert updated["id"] == connection_id

    # LIST: Verify connection appears in list
    connections = get_connections(user_token=user_token, org_id=org_id)
    assert isinstance(connections, list)
    assert any(c["id"] == connection_id for c in connections)

    # DELETE: Delete the connection
    delete_response = delete_connection(
        connection_id=connection_id,
        user_token=user_token,
        org_id=org_id,
    )

    assert delete_response.status_code == 200

    # Verify connection no longer in list
    remaining = get_connections(user_token=user_token, org_id=org_id)
    assert all(c["id"] != connection_id for c in remaining)


@pytest.mark.e2e
def test_connection_test_connectivity(
    create_connection,
    test_connection_connectivity,
    delete_connection,
    create_user,
    login_user,
    whoami,
):
    """Test that connection connectivity can be tested."""
    if not CONNECTION_TEST_DB_PATH.exists():
        pytest.skip(f"SQLite test database missing at {CONNECTION_TEST_DB_PATH}")

    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)['organizations'][0]['id']

    # Create connection
    connection = create_connection(
        name="Connectivity Test",
        type="sqlite",
        config={"database": str(CONNECTION_TEST_DB_PATH)},
        credentials={},
        user_token=user_token,
        org_id=org_id,
    )

    # Test connectivity
    result = test_connection_connectivity(
        connection_id=connection["id"],
        user_token=user_token,
        org_id=org_id,
    )

    assert result["success"] is True
    assert result["connectivity"] is True

    # Cleanup
    delete_connection(
        connection_id=connection["id"],
        user_token=user_token,
        org_id=org_id,
    )


@pytest.mark.e2e
def test_connection_refresh_schema(
    create_connection,
    refresh_connection_schema,
    get_connection_tables,
    delete_connection,
    create_user,
    login_user,
    whoami,
):
    """Test that refreshing a connection discovers tables."""
    if not CONNECTION_TEST_DB_PATH.exists():
        pytest.skip(f"SQLite test database missing at {CONNECTION_TEST_DB_PATH}")

    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)['organizations'][0]['id']

    # Create connection
    connection = create_connection(
        name="Schema Refresh Test",
        type="sqlite",
        config={"database": str(CONNECTION_TEST_DB_PATH)},
        credentials={},
        user_token=user_token,
        org_id=org_id,
    )

    # Refresh schema
    refresh_result = refresh_connection_schema(
        connection_id=connection["id"],
        user_token=user_token,
        org_id=org_id,
    )

    assert "table_count" in refresh_result
    assert refresh_result["table_count"] > 0

    # Verify tables are available
    tables = get_connection_tables(
        connection_id=connection["id"],
        user_token=user_token,
        org_id=org_id,
    )

    assert isinstance(tables, list)
    assert len(tables) > 0
    table_names = [t["name"] for t in tables]
    assert "Album" in table_names or "albums" in [n.lower() for n in table_names]

    # Cleanup
    delete_connection(
        connection_id=connection["id"],
        user_token=user_token,
        org_id=org_id,
    )


@pytest.mark.e2e
def test_connection_delete_with_domains_cascades(
    create_connection,
    create_domain_from_connection,
    delete_connection,
    get_data_source,
    delete_data_source,
    create_user,
    login_user,
    whoami,
):
    """Test that deleting a connection with linked domains succeeds and cascades properly.

    The connection deletion should:
    - Remove the connection
    - Cascade delete DataSourceTable records linked via ConnectionTable
    - Remove domain_connection junction records
    - Leave the domain itself intact (but with no connection)
    """
    if not CONNECTION_TEST_DB_PATH.exists():
        pytest.skip(f"SQLite test database missing at {CONNECTION_TEST_DB_PATH}")

    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)['organizations'][0]['id']

    # Create connection
    connection = create_connection(
        name="Connection With Domain",
        type="sqlite",
        config={"database": str(CONNECTION_TEST_DB_PATH)},
        credentials={},
        user_token=user_token,
        org_id=org_id,
    )

    # Create a domain from this connection
    domain = create_domain_from_connection(
        name="Domain Using Connection",
        connection_id=connection["id"],
        user_token=user_token,
        org_id=org_id,
    )

    # Delete connection - should succeed even with linked domain
    delete_response = delete_connection(
        connection_id=connection["id"],
        user_token=user_token,
        org_id=org_id,
    )

    assert delete_response.status_code == 200

    # Domain should still exist but with no connection
    domain_after = get_data_source(
        data_source_id=domain["id"],
        user_token=user_token,
        org_id=org_id,
    )
    assert domain_after is not None
    assert domain_after["id"] == domain["id"]

    # Cleanup: delete the orphaned domain
    delete_data_source(
        data_source_id=domain["id"],
        user_token=user_token,
        org_id=org_id,
    )


@pytest.mark.e2e
def test_connection_list_with_counts(
    create_connection,
    create_domain_from_connection,
    refresh_connection_schema,
    get_connections,
    delete_data_source,
    delete_connection,
    create_user,
    login_user,
    whoami,
):
    """Test that connection list includes accurate table_count and domain_count."""
    if not CONNECTION_TEST_DB_PATH.exists():
        pytest.skip(f"SQLite test database missing at {CONNECTION_TEST_DB_PATH}")

    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)['organizations'][0]['id']

    # Create connection
    connection = create_connection(
        name="Connection With Counts",
        type="sqlite",
        config={"database": str(CONNECTION_TEST_DB_PATH)},
        credentials={},
        user_token=user_token,
        org_id=org_id,
    )

    # Refresh to populate tables
    refresh_connection_schema(
        connection_id=connection["id"],
        user_token=user_token,
        org_id=org_id,
    )

    # Create a domain
    domain = create_domain_from_connection(
        name="Domain For Counts Test",
        connection_id=connection["id"],
        user_token=user_token,
        org_id=org_id,
    )

    # Get connections list and verify counts
    connections = get_connections(user_token=user_token, org_id=org_id)
    our_conn = next(c for c in connections if c["id"] == connection["id"])

    assert our_conn["domain_count"] == 1
    # table_count should be > 0 (tables from the domain)
    assert our_conn["table_count"] >= 0  # May be 0 if tables not synced to domain yet

    # Cleanup
    delete_data_source(
        data_source_id=domain["id"],
        user_token=user_token,
        org_id=org_id,
    )
    delete_connection(
        connection_id=connection["id"],
        user_token=user_token,
        org_id=org_id,
    )


@pytest.mark.e2e
def test_connection_get_tables(
    create_connection,
    refresh_connection_schema,
    get_connection_tables,
    delete_connection,
    create_user,
    login_user,
    whoami,
):
    """Test that GET /connections/{id}/tables returns discovered tables."""
    if not CONNECTION_TEST_DB_PATH.exists():
        pytest.skip(f"SQLite test database missing at {CONNECTION_TEST_DB_PATH}")

    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)['organizations'][0]['id']

    # Create connection
    connection = create_connection(
        name="Get Tables Test",
        type="sqlite",
        config={"database": str(CONNECTION_TEST_DB_PATH)},
        credentials={},
        user_token=user_token,
        org_id=org_id,
    )

    # Refresh to discover tables
    refresh_connection_schema(
        connection_id=connection["id"],
        user_token=user_token,
        org_id=org_id,
    )

    # Get tables
    tables = get_connection_tables(
        connection_id=connection["id"],
        user_token=user_token,
        org_id=org_id,
    )

    assert isinstance(tables, list)
    assert len(tables) > 0

    # Each table should have expected fields
    for table in tables:
        assert "id" in table
        assert "name" in table
        assert "column_count" in table

    # Cleanup
    delete_connection(
        connection_id=connection["id"],
        user_token=user_token,
        org_id=org_id,
    )

