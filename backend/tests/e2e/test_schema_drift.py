"""
E2E tests for schema drift/synchronization.
Tests that schema changes in the underlying database are properly detected and handled.
"""
import pytest
import sqlite3


@pytest.mark.e2e
def test_schema_refresh_discovers_new_tables(
    dynamic_sqlite_db,
    create_data_source,
    get_schema,
    refresh_schema,
    delete_data_source,
    create_user,
    login_user,
    whoami,
):
    """Test that refreshing schema discovers newly added tables."""
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)['organizations'][0]['id']

    # 1. Create data source with initial schema (2 tables: users, orders)
    domain = create_data_source(
        name="Schema Drift Test - New Tables",
        type="sqlite",
        config={"database": dynamic_sqlite_db},
        credentials={},
        user_token=user_token,
        org_id=org_id,
    )

    # 2. Get initial schema
    initial_schema = get_schema(
        data_source_id=domain["id"],
        user_token=user_token,
        org_id=org_id,
    )
    initial_table_names = {t["name"] for t in initial_schema}
    assert "users" in initial_table_names
    assert "orders" in initial_table_names
    initial_count = len(initial_schema)

    # 3. Add a new table to the database
    conn = sqlite3.connect(dynamic_sqlite_db)
    conn.execute("CREATE TABLE products (id INTEGER PRIMARY KEY, name TEXT, price REAL)")
    conn.commit()
    conn.close()

    # 4. Refresh schema
    refresh_schema(
        data_source_id=domain["id"],
        user_token=user_token,
        org_id=org_id,
    )

    # 5. Verify new table is discovered
    updated_schema = get_schema(
        data_source_id=domain["id"],
        user_token=user_token,
        org_id=org_id,
    )
    updated_table_names = {t["name"] for t in updated_schema}

    assert "products" in updated_table_names
    assert len(updated_schema) == initial_count + 1

    # Cleanup
    delete_data_source(
        data_source_id=domain["id"],
        user_token=user_token,
        org_id=org_id,
    )


@pytest.mark.e2e
def test_schema_refresh_handles_deleted_tables(
    dynamic_sqlite_db,
    create_data_source,
    get_full_schema_paginated,
    refresh_schema,
    update_tables_status_delta,
    delete_data_source,
    create_user,
    login_user,
    whoami,
):
    """Test that refreshing schema handles tables deleted from the database."""
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)['organizations'][0]['id']

    # 1. Create data source
    domain = create_data_source(
        name="Schema Drift Test - Deleted Tables",
        type="sqlite",
        config={"database": dynamic_sqlite_db},
        credentials={},
        user_token=user_token,
        org_id=org_id,
    )

    # 2. Refresh to load tables
    refresh_schema(
        data_source_id=domain["id"],
        user_token=user_token,
        org_id=org_id,
    )

    # 3. Activate both tables
    update_tables_status_delta(
        data_source_id=domain["id"],
        activate=["users", "orders"],
        user_token=user_token,
        org_id=org_id,
    )

    # 4. Verify both tables exist
    before = get_full_schema_paginated(
        data_source_id=domain["id"],
        user_token=user_token,
        org_id=org_id,
        page_size=100,
    )
    before_names = {t["name"] for t in before["tables"]}
    assert "orders" in before_names

    # 5. Drop the orders table from the database
    conn = sqlite3.connect(dynamic_sqlite_db)
    conn.execute("DROP TABLE orders")
    conn.commit()
    conn.close()

    # 6. Refresh schema
    refresh_schema(
        data_source_id=domain["id"],
        user_token=user_token,
        org_id=org_id,
    )

    # 7. Verify deleted table handling
    after = get_full_schema_paginated(
        data_source_id=domain["id"],
        user_token=user_token,
        org_id=org_id,
        page_size=100,
    )

    after_names = {t["name"] for t in after["tables"]}

    # The orders table should either be removed or deactivated
    # Check that users is still there
    assert "users" in after_names

    # If orders is still there, it should be inactive
    if "orders" in after_names:
        orders_table = next(t for t in after["tables"] if t["name"] == "orders")
        # Table that no longer exists in DB should be marked inactive
        assert orders_table["is_active"] is False

    # Cleanup
    delete_data_source(
        data_source_id=domain["id"],
        user_token=user_token,
        org_id=org_id,
    )


@pytest.mark.e2e
def test_schema_refresh_updates_column_changes(
    dynamic_sqlite_db,
    create_data_source,
    get_schema,
    refresh_schema,
    delete_data_source,
    create_user,
    login_user,
    whoami,
):
    """Test that refreshing schema detects column additions."""
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)['organizations'][0]['id']

    # 1. Create data source
    domain = create_data_source(
        name="Schema Drift Test - Column Changes",
        type="sqlite",
        config={"database": dynamic_sqlite_db},
        credentials={},
        user_token=user_token,
        org_id=org_id,
    )

    # 2. Refresh to load tables
    refresh_schema(
        data_source_id=domain["id"],
        user_token=user_token,
        org_id=org_id,
    )

    # 3. Get initial schema and count columns in users table
    initial_schema = get_schema(
        data_source_id=domain["id"],
        user_token=user_token,
        org_id=org_id,
    )
    users_table = next((t for t in initial_schema if t["name"] == "users"), None)
    assert users_table is not None
    initial_column_count = len(users_table.get("columns", []))

    # 4. Add a new column to users table
    conn = sqlite3.connect(dynamic_sqlite_db)
    conn.execute("ALTER TABLE users ADD COLUMN email TEXT")
    conn.commit()
    conn.close()

    # 5. Refresh schema
    refresh_schema(
        data_source_id=domain["id"],
        user_token=user_token,
        org_id=org_id,
    )

    # 6. Verify new column is detected
    updated_schema = get_schema(
        data_source_id=domain["id"],
        user_token=user_token,
        org_id=org_id,
    )
    users_table_updated = next((t for t in updated_schema if t["name"] == "users"), None)
    assert users_table_updated is not None
    updated_column_count = len(users_table_updated.get("columns", []))

    assert updated_column_count == initial_column_count + 1
    column_names = {c["name"] for c in users_table_updated.get("columns", [])}
    assert "email" in column_names

    # Cleanup
    delete_data_source(
        data_source_id=domain["id"],
        user_token=user_token,
        org_id=org_id,
    )


@pytest.mark.e2e
def test_schema_refresh_preserves_table_selection(
    dynamic_sqlite_db,
    create_data_source,
    get_full_schema_paginated,
    refresh_schema,
    bulk_update_tables,
    update_tables_status_delta,
    delete_data_source,
    create_user,
    login_user,
    whoami,
):
    """Test that refreshing schema preserves which tables were selected."""
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)['organizations'][0]['id']

    # 1. Create data source
    domain = create_data_source(
        name="Schema Drift Test - Preserve Selection",
        type="sqlite",
        config={"database": dynamic_sqlite_db},
        credentials={},
        user_token=user_token,
        org_id=org_id,
    )

    # 2. Refresh to load tables
    refresh_schema(
        data_source_id=domain["id"],
        user_token=user_token,
        org_id=org_id,
    )

    # 3. Deactivate all, then activate only 'users'
    bulk_update_tables(
        data_source_id=domain["id"],
        action="deactivate",
        filter=None,
        user_token=user_token,
        org_id=org_id,
    )
    update_tables_status_delta(
        data_source_id=domain["id"],
        activate=["users"],
        user_token=user_token,
        org_id=org_id,
    )

    # 4. Verify only users is active
    before = get_full_schema_paginated(
        data_source_id=domain["id"],
        selected_state="selected",
        user_token=user_token,
        org_id=org_id,
    )
    assert before["selected_count"] == 1
    assert before["tables"][0]["name"] == "users"

    # 5. Add a new table to the database
    conn = sqlite3.connect(dynamic_sqlite_db)
    conn.execute("CREATE TABLE products (id INTEGER PRIMARY KEY, name TEXT)")
    conn.commit()
    conn.close()

    # 6. Refresh schema
    refresh_schema(
        data_source_id=domain["id"],
        user_token=user_token,
        org_id=org_id,
    )

    # 7. Verify 'users' is still active, new table is inactive
    after = get_full_schema_paginated(
        data_source_id=domain["id"],
        user_token=user_token,
        org_id=org_id,
        page_size=100,
    )

    users_after = next((t for t in after["tables"] if t["name"] == "users"), None)
    products_after = next((t for t in after["tables"] if t["name"] == "products"), None)

    assert users_after is not None
    assert users_after["is_active"] is True  # Selection preserved

    assert products_after is not None
    assert products_after["is_active"] is False  # New table inactive by default

    # Cleanup
    delete_data_source(
        data_source_id=domain["id"],
        user_token=user_token,
        org_id=org_id,
    )


@pytest.mark.e2e
def test_schema_refresh_with_empty_database(
    empty_sqlite_db,
    create_data_source,
    get_schema,
    refresh_schema,
    delete_data_source,
    create_user,
    login_user,
    whoami,
):
    """Test schema refresh when database starts empty then gets tables."""
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)['organizations'][0]['id']

    # 1. Create data source with empty DB
    domain = create_data_source(
        name="Empty DB Test",
        type="sqlite",
        config={"database": empty_sqlite_db},
        credentials={},
        user_token=user_token,
        org_id=org_id,
    )

    # 2. Schema should be empty
    initial_schema = get_schema(
        data_source_id=domain["id"],
        user_token=user_token,
        org_id=org_id,
    )
    assert len(initial_schema) == 0

    # 3. Add a table to the empty database
    conn = sqlite3.connect(empty_sqlite_db)
    conn.execute("CREATE TABLE first_table (id INTEGER PRIMARY KEY, name TEXT)")
    conn.commit()
    conn.close()

    # 4. Refresh schema
    refresh_schema(
        data_source_id=domain["id"],
        user_token=user_token,
        org_id=org_id,
    )

    # 5. Verify table is discovered
    updated_schema = get_schema(
        data_source_id=domain["id"],
        user_token=user_token,
        org_id=org_id,
    )

    assert len(updated_schema) == 1
    assert updated_schema[0]["name"] == "first_table"

    # Cleanup
    delete_data_source(
        data_source_id=domain["id"],
        user_token=user_token,
        org_id=org_id,
    )


@pytest.mark.e2e
def test_connection_schema_sync_to_domains(
    dynamic_sqlite_db,
    create_connection,
    refresh_connection_schema,
    create_domain_from_connection,
    get_schema,
    refresh_schema,
    delete_data_source,
    delete_connection,
    create_user,
    login_user,
    whoami,
):
    """Test that refreshing a connection's schema propagates to linked domains."""
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)['organizations'][0]['id']

    # 1. Create connection and refresh schema
    connection = create_connection(
        name="Connection For Sync Test",
        type="sqlite",
        config={"database": dynamic_sqlite_db},
        credentials={},
        user_token=user_token,
        org_id=org_id,
    )

    refresh_connection_schema(
        connection_id=connection["id"],
        user_token=user_token,
        org_id=org_id,
    )

    # 2. Create domain from connection
    domain = create_domain_from_connection(
        name="Domain For Sync Test",
        connection_id=connection["id"],
        user_token=user_token,
        org_id=org_id,
    )

    # 3. Refresh domain to get tables
    refresh_schema(
        data_source_id=domain["id"],
        user_token=user_token,
        org_id=org_id,
    )

    # 4. Get initial domain schema
    initial_schema = get_schema(
        data_source_id=domain["id"],
        user_token=user_token,
        org_id=org_id,
    )
    initial_count = len(initial_schema)

    # 5. Add a new table to the database
    conn = sqlite3.connect(dynamic_sqlite_db)
    conn.execute("CREATE TABLE new_table (id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()

    # 6. Refresh connection schema
    refresh_connection_schema(
        connection_id=connection["id"],
        user_token=user_token,
        org_id=org_id,
    )

    # 7. Refresh domain schema
    refresh_schema(
        data_source_id=domain["id"],
        user_token=user_token,
        org_id=org_id,
    )

    # 8. Verify domain has the new table
    updated_schema = get_schema(
        data_source_id=domain["id"],
        user_token=user_token,
        org_id=org_id,
    )
    updated_names = {t["name"] for t in updated_schema}

    assert "new_table" in updated_names
    assert len(updated_schema) == initial_count + 1

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

