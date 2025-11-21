import pytest
from pathlib import Path


DATA_SOURCE_TEST_DB_PATH = (Path(__file__).resolve().parent.parent / "config" / "chinook.sqlite").resolve()
if not DATA_SOURCE_TEST_DB_PATH.exists():
    pytest.skip(f"SQLite test database missing at {DATA_SOURCE_TEST_DB_PATH}")

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

    schema_tables = get_schema(
        data_source_id=data_source["id"],
        user_token=user_token,
        org_id=org_id
    )

    assert isinstance(schema_tables, list)
    assert len(schema_tables) > 0
    table_names = [ row["name"] for row in schema_tables]
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