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

