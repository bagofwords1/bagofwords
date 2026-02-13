import pytest
from pathlib import Path


# Path to demo database (relative to backend folder when running)
DEMO_CHINOOK_PATH = Path(__file__).resolve().parent.parent.parent / "demo-datasources" / "chinook.sqlite"


@pytest.mark.e2e
def test_get_available_mentions_sanity(get_available_mentions,
                                       create_user,
                                       login_user,
                                       whoami):

    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)['organizations'][0]['id']

    result = get_available_mentions(user_token=user_token, org_id=org_id)

    assert isinstance(result, dict)
    # Ensure all keys exist with list values
    assert "data_sources" in result
    assert "tables" in result
    assert "files" in result
    assert "entities" in result

    assert isinstance(result["data_sources"], list)
    assert isinstance(result["tables"], list)
    assert isinstance(result["files"], list)
    assert isinstance(result["entities"], list)


@pytest.mark.e2e
def test_mentions_include_data_sources_and_tables_after_demo_install(
    get_available_mentions,
    install_demo_data_source,
    delete_data_source,
    create_user,
    login_user,
    whoami,
):
    """
    Regression test: Verify data sources and tables appear in mentions after installing a demo.
    
    This test catches the bug where Table.id was missing, causing all tables to be silently
    skipped in the mention service.
    """
    if not DEMO_CHINOOK_PATH.exists():
        pytest.skip(f"Demo database missing at {DEMO_CHINOOK_PATH}")

    # Setup user and organization
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)['organizations'][0]['id']

    # Install chinook demo data source
    result = install_demo_data_source(
        demo_id="chinook",
        user_token=user_token,
        org_id=org_id,
    )
    assert result["success"] is True, f"Failed to install demo: {result}"
    data_source_id = result["data_source_id"]

    try:
        # Get available mentions
        mentions = get_available_mentions(user_token=user_token, org_id=org_id)

        # --- Verify DATA SOURCES appear in mentions ---
        assert len(mentions["data_sources"]) > 0, "Data sources should not be empty after installing demo"
        
        # Find our installed data source in mentions
        ds_in_mentions = [ds for ds in mentions["data_sources"] if ds["id"] == data_source_id]
        assert len(ds_in_mentions) == 1, f"Installed data source {data_source_id} should appear in mentions"
        
        installed_ds = ds_in_mentions[0]
        assert installed_ds.get("name") == "Music Store"
        assert installed_ds.get("type") == "data_source"
        assert "data_source_type" in installed_ds  # Should have the db type (sqlite)

        # --- Verify TABLES appear in mentions ---
        assert len(mentions["tables"]) > 0, "Tables should not be empty after installing demo with tables"
        
        # Find tables belonging to our data source
        demo_tables = [t for t in mentions["tables"] if t.get("datasource_id") == data_source_id]
        assert len(demo_tables) > 0, f"Tables from data source {data_source_id} should appear in mentions"

        # Verify table structure (regression test for missing id field)
        for table in demo_tables:
            assert table.get("id") is not None, "Table must have an id field"
            assert table.get("id") != "None", "Table id must not be string 'None'"
            assert isinstance(table.get("id"), str), "Table id must be a string"
            assert table.get("name"), "Table must have a name"
            assert table.get("type") == "datasource_table", "Table type should be 'datasource_table'"
            assert table.get("datasource_id") == data_source_id, "Table should reference correct data source"

        # Chinook has known tables - verify at least some exist
        table_names = {t["name"] for t in demo_tables}
        # Chinook should have tables like albums, artists, tracks, etc.
        assert len(table_names) >= 5, f"Chinook should have multiple tables, found: {table_names}"

    finally:
        # Cleanup
        delete_data_source(
            data_source_id=data_source_id,
            user_token=user_token,
            org_id=org_id,
        )


@pytest.mark.e2e  
def test_table_mentions_have_valid_ids(
    get_available_mentions,
    install_demo_data_source,
    delete_data_source,
    create_user,
    login_user,
    whoami,
):
    """
    Specific regression test for the Table.id bug.
    
    The bug: mention_service tried to access table.id but Table Pydantic model
    had no id field, causing AttributeError that was silently caught.
    """
    if not DEMO_CHINOOK_PATH.exists():
        pytest.skip(f"Demo database missing at {DEMO_CHINOOK_PATH}")

    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)['organizations'][0]['id']

    result = install_demo_data_source(demo_id="chinook", user_token=user_token, org_id=org_id)
    assert result["success"] is True
    data_source_id = result["data_source_id"]

    try:
        mentions = get_available_mentions(user_token=user_token, org_id=org_id)
        
        # This is the key assertion - tables must exist and have valid IDs
        tables = mentions.get("tables", [])
        assert len(tables) > 0, "Bug regression: tables list is empty (likely due to missing Table.id)"
        
        for table in tables:
            table_id = table.get("id")
            # These assertions would have caught the original bug
            assert table_id is not None, "Table id is None - Table model missing id field?"
            assert str(table_id) != "None", "Table id serialized as 'None' string"
            assert len(str(table_id)) > 0, "Table id is empty string"

    finally:
        delete_data_source(data_source_id=data_source_id, user_token=user_token, org_id=org_id)


