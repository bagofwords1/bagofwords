import pytest
from pathlib import Path


DATA_SOURCE_TEST_DB_PATH = (Path(__file__).resolve().parent.parent / "config" / "chinook.sqlite").resolve()


@pytest.mark.e2e
def test_data_source_creation(
    create_data_source,
    get_data_sources,
    test_connection,
    update_data_source,
    delete_data_source,
    get_schema,
    refresh_schema,
    create_user,
    login_user,
    whoami
):
    # Setup user and organization
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)['organizations'][0]['id']

    if not DATA_SOURCE_TEST_DB_PATH.exists():
        pytest.skip(f"SQLite test database missing at {DATA_SOURCE_TEST_DB_PATH}")

    # Create a basic SQLite data source
    data_source = create_data_source(
        name="Test SQLite DB",
        type="sqlite",
        config={"database": str(DATA_SOURCE_TEST_DB_PATH)},
        credentials={},
        user_token=user_token,
        org_id=org_id
    )
    # Basic assertions
    assert data_source is not None
    assert data_source["name"] == "Test SQLite DB"
    assert data_source["type"] == "sqlite"
    assert "id" in data_source
    assert "created_at" in data_source
    assert "updated_at" in data_source
    assert data_source["is_active"] is not None

    # Reload tables (refresh schema) to ensure metadata is captured
    refreshed_tables = refresh_schema(
        data_source_id=data_source["id"],
        user_token=user_token,
        org_id=org_id
    )

    assert isinstance(refreshed_tables, list)
    assert len(refreshed_tables) > 0

    # Check that refresh_schema returns tables (tables are inactive by default)
    table_names = [row["name"] for row in refreshed_tables]
    assert "Album" in table_names

    # Update data source metadata
    updated_name = "Updated SQLite DB"
    updated = update_data_source(
        data_source_id=data_source["id"],
        payload={
            "name": updated_name,
            "description": "Updated via e2e test"
        },
        user_token=user_token,
        org_id=org_id
    )

    assert updated["name"] == updated_name
    assert updated["description"] == "Updated via e2e test"

    # Test connection
    connection_result = test_connection(
        data_source_id=data_source["id"],
        user_token=user_token,
        org_id=org_id
    )

    assert connection_result is not None
    assert connection_result["success"] is True

    # Verify data source appears in list
    data_sources = get_data_sources(
        user_token=user_token,
        org_id=org_id
    )
    
    assert isinstance(data_sources, list)
    assert len(data_sources) >= 1
    assert any(ds["id"] == data_source["id"] for ds in data_sources)

    # Delete the data source
    delete_response = delete_data_source(
        data_source_id=data_source["id"],
        user_token=user_token,
        org_id=org_id
    )

    assert delete_response.get("message") == "Data source deleted successfully"

    # Ensure data source no longer listed
    remaining_sources = get_data_sources(
        user_token=user_token,
        org_id=org_id
    )
    assert all(ds["id"] != data_source["id"] for ds in remaining_sources)


@pytest.mark.e2e
def test_paginated_full_schema(
    create_data_source,
    delete_data_source,
    refresh_schema,
    get_full_schema_paginated,
    create_user,
    login_user,
    whoami
):
    """Test paginated full schema endpoint with filtering and sorting."""
    if not DATA_SOURCE_TEST_DB_PATH.exists():
        pytest.skip(f"SQLite test database missing at {DATA_SOURCE_TEST_DB_PATH}")

    # Setup
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)['organizations'][0]['id']

    data_source = create_data_source(
        name="Pagination Test DB",
        type="sqlite",
        config={"database": str(DATA_SOURCE_TEST_DB_PATH)},
        credentials={},
        user_token=user_token,
        org_id=org_id
    )
    
    # Refresh to load tables
    refresh_schema(
        data_source_id=data_source["id"],
        user_token=user_token,
        org_id=org_id
    )

    # Test basic pagination
    result = get_full_schema_paginated(
        data_source_id=data_source["id"],
        user_token=user_token,
        org_id=org_id,
        page=1,
        page_size=5
    )

    assert "tables" in result
    assert "total" in result
    assert "page" in result
    assert "page_size" in result
    assert "total_pages" in result
    assert "schemas" in result
    assert "selected_count" in result
    assert "total_tables" in result
    assert "has_more" in result

    assert result["page"] == 1
    assert result["page_size"] == 5
    assert len(result["tables"]) <= 5
    assert result["total"] > 0
    assert result["total_tables"] > 0

    # Test search filter
    search_result = get_full_schema_paginated(
        data_source_id=data_source["id"],
        user_token=user_token,
        org_id=org_id,
        page=1,
        page_size=100,
        search="Album"
    )

    assert search_result["total"] >= 1
    table_names = [t["name"] for t in search_result["tables"]]
    assert any("Album" in name for name in table_names)

    # Test sort by name ascending
    sorted_result = get_full_schema_paginated(
        data_source_id=data_source["id"],
        user_token=user_token,
        org_id=org_id,
        page=1,
        page_size=100,
        sort_by="name",
        sort_dir="asc"
    )

    names = [t["name"] for t in sorted_result["tables"]]
    assert names == sorted(names)

    # Cleanup
    delete_data_source(
        data_source_id=data_source["id"],
        user_token=user_token,
        org_id=org_id
    )


@pytest.mark.e2e
def test_bulk_update_and_delta_update(
    create_data_source,
    delete_data_source,
    refresh_schema,
    get_full_schema_paginated,
    bulk_update_tables,
    update_tables_status_delta,
    create_user,
    login_user,
    whoami
):
    """Test bulk update and delta update endpoints."""
    if not DATA_SOURCE_TEST_DB_PATH.exists():
        pytest.skip(f"SQLite test database missing at {DATA_SOURCE_TEST_DB_PATH}")

    # Setup
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)['organizations'][0]['id']

    data_source = create_data_source(
        name="Bulk Update Test DB",
        type="sqlite",
        config={"database": str(DATA_SOURCE_TEST_DB_PATH)},
        credentials={},
        user_token=user_token,
        org_id=org_id
    )
    
    # Refresh to load tables
    refresh_schema(
        data_source_id=data_source["id"],
        user_token=user_token,
        org_id=org_id
    )

    # Get initial state
    initial = get_full_schema_paginated(
        data_source_id=data_source["id"],
        user_token=user_token,
        org_id=org_id,
        page=1,
        page_size=100
    )
    initial_selected = initial["selected_count"]

    # Test bulk deactivate all
    deactivate_result = bulk_update_tables(
        data_source_id=data_source["id"],
        action="deactivate",
        filter=None,
        user_token=user_token,
        org_id=org_id
    )

    assert "deactivated_count" in deactivate_result
    assert "total_selected" in deactivate_result
    assert deactivate_result["total_selected"] == 0

    # Test bulk activate with search filter
    activate_result = bulk_update_tables(
        data_source_id=data_source["id"],
        action="activate",
        filter={"search": "Album"},
        user_token=user_token,
        org_id=org_id
    )

    assert "activated_count" in activate_result
    assert activate_result["activated_count"] >= 1

    # Verify only Album tables are active
    filtered = get_full_schema_paginated(
        data_source_id=data_source["id"],
        user_token=user_token,
        org_id=org_id,
        page=1,
        page_size=100,
        selected_state="selected"
    )

    for table in filtered["tables"]:
        assert "Album" in table["name"]

    # Test delta update - activate specific table
    delta_result = update_tables_status_delta(
        data_source_id=data_source["id"],
        activate=["Artist"],
        deactivate=[],
        user_token=user_token,
        org_id=org_id
    )

    assert "activated_count" in delta_result
    assert delta_result["activated_count"] == 1

    # Verify Artist is now active
    after_delta = get_full_schema_paginated(
        data_source_id=data_source["id"],
        user_token=user_token,
        org_id=org_id,
        page=1,
        page_size=100,
        search="Artist"
    )

    artist_tables = [t for t in after_delta["tables"] if t["name"] == "Artist"]
    assert len(artist_tables) == 1
    assert artist_tables[0]["is_active"] is True

    # Cleanup
    delete_data_source(
        data_source_id=data_source["id"],
        user_token=user_token,
        org_id=org_id
    )


@pytest.mark.e2e
def test_selected_state_filter(
    create_data_source,
    delete_data_source,
    refresh_schema,
    get_full_schema_paginated,
    bulk_update_tables,
    create_user,
    login_user,
    whoami
):
    """Test filtering by selected/unselected state."""
    if not DATA_SOURCE_TEST_DB_PATH.exists():
        pytest.skip(f"SQLite test database missing at {DATA_SOURCE_TEST_DB_PATH}")

    # Setup
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)['organizations'][0]['id']

    data_source = create_data_source(
        name="Filter State Test DB",
        type="sqlite",
        config={"database": str(DATA_SOURCE_TEST_DB_PATH)},
        credentials={},
        user_token=user_token,
        org_id=org_id
    )
    
    refresh_schema(
        data_source_id=data_source["id"],
        user_token=user_token,
        org_id=org_id
    )

    # Deactivate all first
    bulk_update_tables(
        data_source_id=data_source["id"],
        action="deactivate",
        filter=None,
        user_token=user_token,
        org_id=org_id
    )

    # Activate only Album
    bulk_update_tables(
        data_source_id=data_source["id"],
        action="activate",
        filter={"search": "Album"},
        user_token=user_token,
        org_id=org_id
    )

    # Test selected_state=selected filter
    selected = get_full_schema_paginated(
        data_source_id=data_source["id"],
        user_token=user_token,
        org_id=org_id,
        page=1,
        page_size=100,
        selected_state="selected"
    )

    assert selected["total"] >= 1
    for table in selected["tables"]:
        assert table["is_active"] is True

    # Test selected_state=unselected filter
    unselected = get_full_schema_paginated(
        data_source_id=data_source["id"],
        user_token=user_token,
        org_id=org_id,
        page=1,
        page_size=100,
        selected_state="unselected"
    )

    assert unselected["total"] >= 1
    for table in unselected["tables"]:
        assert table["is_active"] is False

    # Cleanup
    delete_data_source(
        data_source_id=data_source["id"],
        user_token=user_token,
        org_id=org_id
    )


@pytest.mark.e2e
def test_available_data_sources_includes_requires_license(
    test_client,
    create_user,
    login_user,
    whoami
):
    """Available data sources should include requires_license field."""
    # Setup user
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)['organizations'][0]['id']

    response = test_client.get(
        "/api/available_data_sources",
        headers={
            "Authorization": f"Bearer {user_token}",
            "X-Organization-Id": org_id,
        },
    )

    assert response.status_code == 200
    data_sources = response.json()

    # Find powerbi - should require enterprise license
    powerbi = next((ds for ds in data_sources if ds["type"] == "powerbi"), None)
    assert powerbi is not None, "PowerBI should be in available data sources"
    assert powerbi.get("requires_license") == "enterprise"

    # Find qvd - should require enterprise license
    qvd = next((ds for ds in data_sources if ds["type"] == "qvd"), None)
    assert qvd is not None, "QVD should be in available data sources"
    assert qvd.get("requires_license") == "enterprise"

    # PostgreSQL should not require license
    postgres = next((ds for ds in data_sources if ds["type"] == "postgresql"), None)
    if postgres:
        assert postgres.get("requires_license") is None