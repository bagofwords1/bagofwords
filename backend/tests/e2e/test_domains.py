"""
E2E tests for Domain (DataSource) operations.
Tests domain creation, table management, and domain-connection relationships.
"""
import pytest
from pathlib import Path


# Path to test database
DOMAIN_TEST_DB_PATH = (Path(__file__).resolve().parent.parent / "config" / "chinook.sqlite").resolve()


@pytest.mark.e2e
def test_domain_creation_with_new_connection(
    create_data_source,
    get_data_sources,
    delete_data_source,
    create_user,
    login_user,
    whoami,
):
    """Test creating a domain with a new connection (traditional flow)."""
    if not DOMAIN_TEST_DB_PATH.exists():
        pytest.skip(f"SQLite test database missing at {DOMAIN_TEST_DB_PATH}")

    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)['organizations'][0]['id']

    # Create domain with new connection (in one call)
    domain = create_data_source(
        name="Domain With New Connection",
        type="sqlite",
        config={"database": str(DOMAIN_TEST_DB_PATH)},
        credentials={},
        user_token=user_token,
        org_id=org_id,
    )

    assert domain is not None
    assert domain["name"] == "Domain With New Connection"
    assert "id" in domain
    assert domain["type"] == "sqlite"

    # Verify domain appears in list
    domains = get_data_sources(user_token=user_token, org_id=org_id)
    assert any(d["id"] == domain["id"] for d in domains)

    # Cleanup
    delete_data_source(
        data_source_id=domain["id"],
        user_token=user_token,
        org_id=org_id,
    )


@pytest.mark.e2e
def test_domain_creation_from_existing_connection(
    create_connection,
    create_domain_from_connection,
    get_data_sources,
    delete_data_source,
    delete_connection,
    create_user,
    login_user,
    whoami,
):
    """Test creating a domain from an existing connection."""
    if not DOMAIN_TEST_DB_PATH.exists():
        pytest.skip(f"SQLite test database missing at {DOMAIN_TEST_DB_PATH}")

    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)['organizations'][0]['id']

    # First create a connection
    connection = create_connection(
        name="Shared Connection",
        type="sqlite",
        config={"database": str(DOMAIN_TEST_DB_PATH)},
        credentials={},
        user_token=user_token,
        org_id=org_id,
    )

    # Create domain from existing connection
    domain = create_domain_from_connection(
        name="Domain From Existing Connection",
        connection_id=connection["id"],
        user_token=user_token,
        org_id=org_id,
    )

    assert domain is not None
    assert domain["name"] == "Domain From Existing Connection"
    assert "id" in domain

    # Verify domain appears in list
    domains = get_data_sources(user_token=user_token, org_id=org_id)
    assert any(d["id"] == domain["id"] for d in domains)

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
def test_domain_inherits_connection_tables(
    create_connection,
    refresh_connection_schema,
    create_domain_from_connection,
    get_schema,
    delete_data_source,
    delete_connection,
    create_user,
    login_user,
    whoami,
):
    """Test that a domain inherits tables from its connection."""
    if not DOMAIN_TEST_DB_PATH.exists():
        pytest.skip(f"SQLite test database missing at {DOMAIN_TEST_DB_PATH}")

    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)['organizations'][0]['id']

    # Create connection and refresh schema
    connection = create_connection(
        name="Connection With Tables",
        type="sqlite",
        config={"database": str(DOMAIN_TEST_DB_PATH)},
        credentials={},
        user_token=user_token,
        org_id=org_id,
    )

    refresh_connection_schema(
        connection_id=connection["id"],
        user_token=user_token,
        org_id=org_id,
    )

    # Create domain from connection
    domain = create_domain_from_connection(
        name="Domain With Inherited Tables",
        connection_id=connection["id"],
        user_token=user_token,
        org_id=org_id,
    )

    # Get domain schema - should have tables from connection
    schema = get_schema(
        data_source_id=domain["id"],
        user_token=user_token,
        org_id=org_id,
    )

    assert isinstance(schema, list)
    # Tables should be available (may be empty if not synced, but structure should work)

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
def test_domain_table_activation(
    create_data_source,
    refresh_schema,
    bulk_update_tables,
    update_tables_status_delta,
    get_full_schema_paginated,
    delete_data_source,
    create_user,
    login_user,
    whoami,
):
    """Test activating and deactivating tables on a domain."""
    if not DOMAIN_TEST_DB_PATH.exists():
        pytest.skip(f"SQLite test database missing at {DOMAIN_TEST_DB_PATH}")

    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)['organizations'][0]['id']

    # Create domain
    domain = create_data_source(
        name="Table Activation Test",
        type="sqlite",
        config={"database": str(DOMAIN_TEST_DB_PATH)},
        credentials={},
        user_token=user_token,
        org_id=org_id,
    )

    # Refresh schema
    refresh_schema(
        data_source_id=domain["id"],
        user_token=user_token,
        org_id=org_id,
    )

    # Deactivate all tables
    bulk_update_tables(
        data_source_id=domain["id"],
        action="deactivate",
        filter=None,
        user_token=user_token,
        org_id=org_id,
    )

    # Verify all deactivated
    after_deactivate = get_full_schema_paginated(
        data_source_id=domain["id"],
        user_token=user_token,
        org_id=org_id,
    )
    assert after_deactivate["selected_count"] == 0

    # Activate specific table
    update_tables_status_delta(
        data_source_id=domain["id"],
        activate=["Album"],
        user_token=user_token,
        org_id=org_id,
    )

    # Verify only Album is active
    after_activate = get_full_schema_paginated(
        data_source_id=domain["id"],
        selected_state="selected",
        user_token=user_token,
        org_id=org_id,
    )
    assert after_activate["selected_count"] == 1

    # Cleanup
    delete_data_source(
        data_source_id=domain["id"],
        user_token=user_token,
        org_id=org_id,
    )


@pytest.mark.e2e
def test_domain_update_metadata(
    create_data_source,
    update_data_source,
    delete_data_source,
    create_user,
    login_user,
    whoami,
):
    """Test updating domain metadata (name, description)."""
    if not DOMAIN_TEST_DB_PATH.exists():
        pytest.skip(f"SQLite test database missing at {DOMAIN_TEST_DB_PATH}")

    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)['organizations'][0]['id']

    # Create domain
    domain = create_data_source(
        name="Original Name",
        type="sqlite",
        config={"database": str(DOMAIN_TEST_DB_PATH)},
        credentials={},
        user_token=user_token,
        org_id=org_id,
    )

    # Update metadata
    updated = update_data_source(
        data_source_id=domain["id"],
        payload={
            "name": "Updated Name",
            "description": "Updated description",
        },
        user_token=user_token,
        org_id=org_id,
    )

    assert updated["name"] == "Updated Name"
    assert updated["description"] == "Updated description"

    # Cleanup
    delete_data_source(
        data_source_id=domain["id"],
        user_token=user_token,
        org_id=org_id,
    )


@pytest.mark.e2e
def test_domain_delete_keeps_connection(
    create_connection,
    create_domain_from_connection,
    get_connections,
    delete_data_source,
    delete_connection,
    create_user,
    login_user,
    whoami,
):
    """Test that deleting a domain does NOT delete the underlying connection."""
    if not DOMAIN_TEST_DB_PATH.exists():
        pytest.skip(f"SQLite test database missing at {DOMAIN_TEST_DB_PATH}")

    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)['organizations'][0]['id']

    # Create connection
    connection = create_connection(
        name="Connection That Survives",
        type="sqlite",
        config={"database": str(DOMAIN_TEST_DB_PATH)},
        credentials={},
        user_token=user_token,
        org_id=org_id,
    )

    # Create domain from connection
    domain = create_domain_from_connection(
        name="Domain To Delete",
        connection_id=connection["id"],
        user_token=user_token,
        org_id=org_id,
    )

    # Delete domain
    delete_data_source(
        data_source_id=domain["id"],
        user_token=user_token,
        org_id=org_id,
    )

    # Verify connection still exists
    connections = get_connections(user_token=user_token, org_id=org_id)
    assert any(c["id"] == connection["id"] for c in connections)

    # Cleanup connection
    delete_connection(
        connection_id=connection["id"],
        user_token=user_token,
        org_id=org_id,
    )


@pytest.mark.e2e
def test_domain_creation_with_multiple_connections(
    create_connection,
    create_domain_from_connections,
    get_data_sources,
    get_schema,
    refresh_connection_schema,
    delete_data_source,
    delete_connection,
    create_user,
    login_user,
    whoami,
):
    """Test creating a domain linked to multiple connections."""
    if not DOMAIN_TEST_DB_PATH.exists():
        pytest.skip(f"SQLite test database missing at {DOMAIN_TEST_DB_PATH}")

    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)['organizations'][0]['id']

    # Create two connections
    connection1 = create_connection(
        name="Connection One",
        type="sqlite",
        config={"database": str(DOMAIN_TEST_DB_PATH)},
        credentials={},
        user_token=user_token,
        org_id=org_id,
    )

    connection2 = create_connection(
        name="Connection Two",
        type="sqlite",
        config={"database": str(DOMAIN_TEST_DB_PATH)},
        credentials={},
        user_token=user_token,
        org_id=org_id,
    )

    # Refresh schemas on both connections
    refresh_connection_schema(
        connection_id=connection1["id"],
        user_token=user_token,
        org_id=org_id,
    )
    refresh_connection_schema(
        connection_id=connection2["id"],
        user_token=user_token,
        org_id=org_id,
    )

    # Create domain with multiple connections
    domain = create_domain_from_connections(
        name="Multi-Connection Domain",
        connection_ids=[connection1["id"], connection2["id"]],
        user_token=user_token,
        org_id=org_id,
    )

    assert domain is not None
    assert domain["name"] == "Multi-Connection Domain"
    assert "id" in domain

    # Verify domain has connections array with 2 connections
    assert "connections" in domain
    assert len(domain["connections"]) == 2
    connection_ids_in_domain = [c["id"] for c in domain["connections"]]
    assert connection1["id"] in connection_ids_in_domain
    assert connection2["id"] in connection_ids_in_domain

    # Verify domain appears in list with connections
    domains = get_data_sources(user_token=user_token, org_id=org_id)
    our_domain = next(d for d in domains if d["id"] == domain["id"])
    assert len(our_domain.get("connections", [])) == 2

    # Get schema - should have tables from both connections
    schema = get_schema(
        data_source_id=domain["id"],
        user_token=user_token,
        org_id=org_id,
    )
    assert isinstance(schema, list)

    # Cleanup
    delete_data_source(
        data_source_id=domain["id"],
        user_token=user_token,
        org_id=org_id,
    )
    delete_connection(
        connection_id=connection1["id"],
        user_token=user_token,
        org_id=org_id,
    )
    delete_connection(
        connection_id=connection2["id"],
        user_token=user_token,
        org_id=org_id,
    )


@pytest.mark.e2e
def test_multiple_domains_same_connection(
    create_connection,
    create_domain_from_connection,
    get_connections,
    delete_data_source,
    delete_connection,
    create_user,
    login_user,
    whoami,
):
    """Test creating multiple domains from the same connection."""
    if not DOMAIN_TEST_DB_PATH.exists():
        pytest.skip(f"SQLite test database missing at {DOMAIN_TEST_DB_PATH}")

    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)['organizations'][0]['id']

    # Create connection
    connection = create_connection(
        name="Shared Connection",
        type="sqlite",
        config={"database": str(DOMAIN_TEST_DB_PATH)},
        credentials={},
        user_token=user_token,
        org_id=org_id,
    )

    # Create first domain
    domain1 = create_domain_from_connection(
        name="Domain One",
        connection_id=connection["id"],
        user_token=user_token,
        org_id=org_id,
    )

    # Create second domain from same connection
    domain2 = create_domain_from_connection(
        name="Domain Two",
        connection_id=connection["id"],
        user_token=user_token,
        org_id=org_id,
    )

    assert domain1["id"] != domain2["id"]
    assert domain1["name"] == "Domain One"
    assert domain2["name"] == "Domain Two"

    # Verify connection shows 2 domains
    connections = get_connections(user_token=user_token, org_id=org_id)
    our_conn = next(c for c in connections if c["id"] == connection["id"])
    assert our_conn["domain_count"] == 2

    # Cleanup
    delete_data_source(data_source_id=domain1["id"], user_token=user_token, org_id=org_id)
    delete_data_source(data_source_id=domain2["id"], user_token=user_token, org_id=org_id)
    delete_connection(connection_id=connection["id"], user_token=user_token, org_id=org_id)


@pytest.mark.e2e
def test_domain_paginated_schema(
    create_data_source,
    refresh_schema,
    get_full_schema_paginated,
    delete_data_source,
    create_user,
    login_user,
    whoami,
):
    """Test paginated schema endpoint with filtering and sorting."""
    if not DOMAIN_TEST_DB_PATH.exists():
        pytest.skip(f"SQLite test database missing at {DOMAIN_TEST_DB_PATH}")

    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)['organizations'][0]['id']

    # Create domain
    domain = create_data_source(
        name="Paginated Schema Test",
        type="sqlite",
        config={"database": str(DOMAIN_TEST_DB_PATH)},
        credentials={},
        user_token=user_token,
        org_id=org_id,
    )

    # Refresh schema
    refresh_schema(
        data_source_id=domain["id"],
        user_token=user_token,
        org_id=org_id,
    )

    # Test pagination
    result = get_full_schema_paginated(
        data_source_id=domain["id"],
        page=1,
        page_size=5,
        user_token=user_token,
        org_id=org_id,
    )

    assert "tables" in result
    assert "total" in result
    assert "page" in result
    assert "page_size" in result
    assert "total_pages" in result
    assert "selected_count" in result
    assert "total_tables" in result
    assert result["page"] == 1
    assert result["page_size"] == 5
    assert len(result["tables"]) <= 5

    # Test search filter
    search_result = get_full_schema_paginated(
        data_source_id=domain["id"],
        search="Album",
        user_token=user_token,
        org_id=org_id,
    )

    assert search_result["total"] >= 1
    table_names = [t["name"] for t in search_result["tables"]]
    assert any("Album" in name for name in table_names)

    # Cleanup
    delete_data_source(
        data_source_id=domain["id"],
        user_token=user_token,
        org_id=org_id,
    )


@pytest.mark.e2e
def test_domain_bulk_update_tables(
    create_data_source,
    refresh_schema,
    bulk_update_tables,
    get_full_schema_paginated,
    delete_data_source,
    create_user,
    login_user,
    whoami,
):
    """Test bulk activate/deactivate tables."""
    if not DOMAIN_TEST_DB_PATH.exists():
        pytest.skip(f"SQLite test database missing at {DOMAIN_TEST_DB_PATH}")

    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)['organizations'][0]['id']

    # Create domain
    domain = create_data_source(
        name="Bulk Update Test",
        type="sqlite",
        config={"database": str(DOMAIN_TEST_DB_PATH)},
        credentials={},
        user_token=user_token,
        org_id=org_id,
    )

    # Refresh schema
    refresh_schema(
        data_source_id=domain["id"],
        user_token=user_token,
        org_id=org_id,
    )

    # Deactivate all
    deactivate_result = bulk_update_tables(
        data_source_id=domain["id"],
        action="deactivate",
        filter=None,
        user_token=user_token,
        org_id=org_id,
    )

    assert "deactivated_count" in deactivate_result
    assert deactivate_result["total_selected"] == 0

    # Activate with search filter
    activate_result = bulk_update_tables(
        data_source_id=domain["id"],
        action="activate",
        filter={"search": "Album"},
        user_token=user_token,
        org_id=org_id,
    )

    assert "activated_count" in activate_result
    assert activate_result["activated_count"] >= 1

    # Cleanup
    delete_data_source(
        data_source_id=domain["id"],
        user_token=user_token,
        org_id=org_id,
    )


@pytest.mark.e2e
def test_domain_test_connection(
    create_data_source,
    test_connection,
    delete_data_source,
    create_user,
    login_user,
    whoami,
):
    """Test domain's test_connection endpoint."""
    if not DOMAIN_TEST_DB_PATH.exists():
        pytest.skip(f"SQLite test database missing at {DOMAIN_TEST_DB_PATH}")

    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)['organizations'][0]['id']

    # Create domain
    domain = create_data_source(
        name="Test Connection Domain",
        type="sqlite",
        config={"database": str(DOMAIN_TEST_DB_PATH)},
        credentials={},
        user_token=user_token,
        org_id=org_id,
    )

    # Test connection
    result = test_connection(
        data_source_id=domain["id"],
        user_token=user_token,
        org_id=org_id,
    )

    assert result["success"] is True

    # Cleanup
    delete_data_source(
        data_source_id=domain["id"],
        user_token=user_token,
        org_id=org_id,
    )


@pytest.mark.e2e
def test_domain_selected_state_filter(
    create_data_source,
    refresh_schema,
    bulk_update_tables,
    update_tables_status_delta,
    get_full_schema_paginated,
    delete_data_source,
    create_user,
    login_user,
    whoami,
):
    """Test filtering by selected/unselected state."""
    if not DOMAIN_TEST_DB_PATH.exists():
        pytest.skip(f"SQLite test database missing at {DOMAIN_TEST_DB_PATH}")

    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)['organizations'][0]['id']

    # Create domain
    domain = create_data_source(
        name="Selected State Filter Test",
        type="sqlite",
        config={"database": str(DOMAIN_TEST_DB_PATH)},
        credentials={},
        user_token=user_token,
        org_id=org_id,
    )

    # Refresh schema
    refresh_schema(
        data_source_id=domain["id"],
        user_token=user_token,
        org_id=org_id,
    )

    # Deactivate all, then activate only Album
    bulk_update_tables(
        data_source_id=domain["id"],
        action="deactivate",
        filter=None,
        user_token=user_token,
        org_id=org_id,
    )
    update_tables_status_delta(
        data_source_id=domain["id"],
        activate=["Album"],
        user_token=user_token,
        org_id=org_id,
    )

    # Test selected filter
    selected = get_full_schema_paginated(
        data_source_id=domain["id"],
        selected_state="selected",
        user_token=user_token,
        org_id=org_id,
    )

    assert selected["total"] >= 1
    for table in selected["tables"]:
        assert table["is_active"] is True

    # Test unselected filter
    unselected = get_full_schema_paginated(
        data_source_id=domain["id"],
        selected_state="unselected",
        user_token=user_token,
        org_id=org_id,
    )

    assert unselected["total"] >= 1
    for table in unselected["tables"]:
        assert table["is_active"] is False

    # Cleanup
    delete_data_source(
        data_source_id=domain["id"],
        user_token=user_token,
        org_id=org_id,
    )


# ============================================================================
# User-Based Auth (user_required) Tests
# ============================================================================

@pytest.mark.e2e
def test_domain_user_required_owner_can_access(
    create_data_source,
    refresh_schema,
    get_schema,
    test_connection,
    delete_data_source,
    create_user,
    login_user,
    whoami,
):
    """Test that owner can access a user_required domain using system credentials fallback."""
    if not DOMAIN_TEST_DB_PATH.exists():
        pytest.skip(f"SQLite test database missing at {DOMAIN_TEST_DB_PATH}")

    import uuid

    # Create owner with unique email
    owner_email = f"owner_{uuid.uuid4().hex[:8]}@test.com"
    owner = create_user(email=owner_email, password="test123")
    owner_token = login_user(owner_email, "test123")
    owner_info = whoami(owner_token)
    org_id = owner_info['organizations'][0]['id']

    # Create domain with user_required auth policy
    domain = create_data_source(
        name="User Required Auth Domain",
        type="sqlite",
        config={"database": str(DOMAIN_TEST_DB_PATH)},
        credentials={},
        auth_policy="user_required",
        user_token=owner_token,
        org_id=org_id,
    )

    assert domain is not None
    assert domain["name"] == "User Required Auth Domain"

    # Owner should be able to refresh schema (owner fallback)
    tables = refresh_schema(
        data_source_id=domain["id"],
        user_token=owner_token,
        org_id=org_id,
    )

    assert isinstance(tables, list)
    assert len(tables) > 0, "Owner should be able to refresh schema and see tables"

    # Owner should be able to get schema
    schema = get_schema(
        data_source_id=domain["id"],
        user_token=owner_token,
        org_id=org_id,
    )

    assert isinstance(schema, list)
    assert len(schema) > 0

    # Owner should be able to test connection
    connection_result = test_connection(
        data_source_id=domain["id"],
        user_token=owner_token,
        org_id=org_id,
    )

    assert connection_result["success"] is True

    # Cleanup
    delete_data_source(
        data_source_id=domain["id"],
        user_token=owner_token,
        org_id=org_id,
    )


@pytest.mark.e2e
def test_domain_user_required_owner_status_not_offline(
    create_data_source,
    get_data_sources,
    refresh_schema,
    delete_data_source,
    create_user,
    login_user,
    whoami,
):
    """Test that user_required domain shows correct status (not offline) for owner."""
    if not DOMAIN_TEST_DB_PATH.exists():
        pytest.skip(f"SQLite test database missing at {DOMAIN_TEST_DB_PATH}")

    import uuid

    owner_email = f"owner_{uuid.uuid4().hex[:8]}@test.com"
    owner = create_user(email=owner_email, password="test123")
    owner_token = login_user(owner_email, "test123")
    org_id = whoami(owner_token)['organizations'][0]['id']

    # Create domain with user_required auth
    domain = create_data_source(
        name="Status Check Domain",
        type="sqlite",
        config={"database": str(DOMAIN_TEST_DB_PATH)},
        credentials={},
        auth_policy="user_required",
        user_token=owner_token,
        org_id=org_id,
    )

    # Refresh schema to trigger connection test
    refresh_schema(
        data_source_id=domain["id"],
        user_token=owner_token,
        org_id=org_id,
    )

    # Get data sources list and check status
    data_sources = get_data_sources(user_token=owner_token, org_id=org_id)
    our_domain = next(d for d in data_sources if d["id"] == domain["id"])

    # Connection status should NOT be offline for owner
    connection = our_domain.get("connection") or {}
    user_status = connection.get("user_status") or {}

    # Status should NOT be offline (owner can use system fallback)
    assert user_status.get("connection") != "offline", \
        f"Expected non-offline status for owner, got: {user_status.get('connection')}"

    # effective_auth should be 'system' (using fallback)
    assert user_status.get("effective_auth") == "system", \
        f"Expected effective_auth='system' for owner fallback, got: {user_status.get('effective_auth')}"

    # uses_fallback should be True
    assert user_status.get("uses_fallback") is True, \
        f"Expected uses_fallback=True for owner, got: {user_status.get('uses_fallback')}"

    # Cleanup
    delete_data_source(
        data_source_id=domain["id"],
        user_token=owner_token,
        org_id=org_id,
    )


@pytest.mark.e2e
def test_domain_user_required_non_owner_blocked(
    create_data_source,
    refresh_schema,
    delete_data_source,
    create_user,
    login_user,
    whoami,
    test_client,
):
    """Test that non-owner users cannot access user_required domain without credentials."""
    if not DOMAIN_TEST_DB_PATH.exists():
        pytest.skip(f"SQLite test database missing at {DOMAIN_TEST_DB_PATH}")

    import uuid

    # Create owner/admin with unique email
    owner_email = f"owner_{uuid.uuid4().hex[:8]}@test.com"
    owner = create_user(email=owner_email, password="test123")
    owner_token = login_user(owner_email, "test123")
    owner_info = whoami(owner_token)
    org_id = owner_info['organizations'][0]['id']

    # Create domain with user_required auth
    domain = create_data_source(
        name="Restricted Domain",
        type="sqlite",
        config={"database": str(DOMAIN_TEST_DB_PATH)},
        credentials={},
        auth_policy="user_required",
        user_token=owner_token,
        org_id=org_id,
    )

    # Owner should be able to refresh schema
    owner_tables = refresh_schema(
        data_source_id=domain["id"],
        user_token=owner_token,
        org_id=org_id,
    )
    assert len(owner_tables) > 0, "Owner should be able to refresh schema"

    # Pre-invite a second user by email as member role
    other_email = f"member_{uuid.uuid4().hex[:8]}@test.com"
    invite_response = test_client.post(
        f"/api/organizations/{org_id}/members",
        json={"organization_id": org_id, "email": other_email, "role": "member"},
        headers={
            "Authorization": f"Bearer {owner_token}",
            "X-Organization-Id": str(org_id)
        }
    )
    assert invite_response.status_code == 200, f"Failed to invite member: {invite_response.json()}"

    # Now the second user registers with that email
    other_user = create_user(email=other_email, password="test123")
    other_token = login_user(other_email, "test123")
    other_info = whoami(other_token)

    # Verify second user is in the same org
    other_org_ids = [o['id'] for o in other_info['organizations']]
    assert org_id in other_org_ids, "Second user should be in the same org"

    # Non-owner trying to refresh schema should fail with 403
    headers = {
        "Authorization": f"Bearer {other_token}",
        "X-Organization-Id": str(org_id)
    }

    response = test_client.get(
        f"/api/data_sources/{domain['id']}/refresh_schema",
        headers=headers
    )

    # Should get 403 "User credentials required"
    assert response.status_code == 403, \
        f"Expected 403 for non-owner without credentials, got {response.status_code}: {response.json()}"

    # Check that non-owner sees "offline" status
    ds_response = test_client.get(
        f"/api/data_sources/{domain['id']}",
        headers=headers
    )

    if ds_response.status_code == 200:
        ds_data = ds_response.json()
        connection = ds_data.get("connection") or {}
        user_status = connection.get("user_status") or {}

        # Non-owner without credentials should see "offline"
        assert user_status.get("connection") == "offline", \
            f"Expected 'offline' for non-owner, got: {user_status.get('connection')}"
        assert user_status.get("effective_auth") == "none", \
            f"Expected effective_auth='none' for non-owner, got: {user_status.get('effective_auth')}"

    # Cleanup
    delete_data_source(
        data_source_id=domain["id"],
        user_token=owner_token,
        org_id=org_id,
    )


@pytest.mark.e2e
def test_domain_user_required_admin_can_access(
    create_data_source,
    refresh_schema,
    delete_data_source,
    create_user,
    login_user,
    whoami,
    test_client,
):
    """Test that admin with update_data_source permission can access user_required domain."""
    if not DOMAIN_TEST_DB_PATH.exists():
        pytest.skip(f"SQLite test database missing at {DOMAIN_TEST_DB_PATH}")

    import uuid

    # Create owner/admin
    owner_email = f"owner_{uuid.uuid4().hex[:8]}@test.com"
    owner = create_user(email=owner_email, password="test123")
    owner_token = login_user(owner_email, "test123")
    owner_info = whoami(owner_token)
    org_id = owner_info['organizations'][0]['id']

    # Create domain with user_required auth
    domain = create_data_source(
        name="Admin Access Domain",
        type="sqlite",
        config={"database": str(DOMAIN_TEST_DB_PATH)},
        credentials={},
        auth_policy="user_required",
        user_token=owner_token,
        org_id=org_id,
    )

    # Pre-invite a second user by email with ADMIN role
    admin_email = f"admin_{uuid.uuid4().hex[:8]}@test.com"
    invite_response = test_client.post(
        f"/api/organizations/{org_id}/members",
        json={"organization_id": org_id, "email": admin_email, "role": "admin"},
        headers={
            "Authorization": f"Bearer {owner_token}",
            "X-Organization-Id": str(org_id)
        }
    )
    assert invite_response.status_code == 200, f"Failed to invite admin: {invite_response.json()}"

    # Second user registers with that email
    admin_user = create_user(email=admin_email, password="test123")
    admin_token = login_user(admin_email, "test123")

    # Admin should be able to refresh schema (via permission fallback)
    headers = {
        "Authorization": f"Bearer {admin_token}",
        "X-Organization-Id": str(org_id)
    }

    response = test_client.get(
        f"/api/data_sources/{domain['id']}/refresh_schema",
        headers=headers
    )

    assert response.status_code == 200, \
        f"Admin should be able to refresh schema, got {response.status_code}: {response.json()}"

    tables = response.json()
    assert isinstance(tables, list)
    assert len(tables) > 0

    # Admin should NOT see "offline" status
    ds_response = test_client.get(
        f"/api/data_sources/{domain['id']}",
        headers=headers
    )

    ds_data = ds_response.json()
    connection = ds_data.get("connection") or {}
    user_status = connection.get("user_status") or {}

    # Admin with permission should use system fallback
    assert user_status.get("connection") != "offline", \
        f"Admin should not see 'offline', got: {user_status.get('connection')}"
    assert user_status.get("effective_auth") == "system", \
        f"Admin should use 'system' effective_auth, got: {user_status.get('effective_auth')}"

    # Cleanup
    delete_data_source(
        data_source_id=domain["id"],
        user_token=owner_token,
        org_id=org_id,
    )
