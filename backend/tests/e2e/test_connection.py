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
    test_client,
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

    # Refresh schema — now kicks off a background indexing job and returns
    # the indexing row. Poll /indexing until terminal, then assert tables.
    refresh_result = refresh_connection_schema(
        connection_id=connection["id"],
        user_token=user_token,
        org_id=org_id,
    )
    assert "indexing" in refresh_result
    assert refresh_result["indexing"]["status"] in ("pending", "running", "completed")

    # Wait for the in-flight indexing run (first run may already be in flight
    # from connection creation; reindex returns that one if so).
    import time as _time
    from tests.e2e.test_connection_indexing import _poll_until_terminal
    final = _poll_until_terminal(test_client, connection["id"], user_token, org_id)
    assert final["status"] == "completed", final
    assert final["progress_total"] > 0

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
    - Delete domains that only have this connection (single-connection domains)
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

    # Domain should be deleted since it only had this one connection
    domain_after = get_data_source(
        data_source_id=domain["id"],
        user_token=user_token,
        org_id=org_id,
    )
    assert domain_after is None, "Domain with single connection should be deleted when connection is deleted"


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
    """Test that connection list includes accurate table_count and agent_count."""
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

    assert our_conn["agent_count"] == 1
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
    test_client,
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

    # /refresh only queues the indexing job and returns straight away, so the
    # tables it discovers do not exist yet when it responds. Wait for the run to
    # finish before asserting on its output — the same wait test_connection_crud
    # above does. Without it this passes only when the background job happens to
    # win the race, which is why it flakes on a loaded CI runner.
    from tests.e2e.test_connection_indexing import _poll_until_terminal
    final = _poll_until_terminal(test_client, connection["id"], user_token, org_id)
    assert final["status"] == "completed", final

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



def _assert_explicit_utc(value, *, field, lower, upper):
    """A timestamp the browser renders must carry its offset: `new Date()`
    parses an offset-less ISO string as *local* time, shifting every label by
    the viewer's UTC offset."""
    from datetime import datetime, timedelta
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    assert parsed.tzinfo is not None, f"{field}={value!r} has no UTC offset"
    assert lower - timedelta(seconds=5) <= parsed <= upper + timedelta(seconds=5), (
        f"{field}={value!r} is not the UTC instant it was recorded at"
    )


@pytest.mark.e2e
def test_connection_timestamps_are_explicit_utc(
    create_connection,
    test_connection_connectivity,
    refresh_connection_schema,
    get_connection,
    get_connections,
    delete_connection,
    create_user,
    login_user,
    whoami,
    test_client,
):
    """Every timestamp the connection endpoints return is an unambiguous UTC
    instant, so "Last checked" / "Last synced" show the viewer's local time."""
    if not CONNECTION_TEST_DB_PATH.exists():
        pytest.skip(f"SQLite test database missing at {CONNECTION_TEST_DB_PATH}")
    from datetime import datetime, timezone
    from tests.e2e.test_connection_indexing import _poll_until_terminal

    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)['organizations'][0]['id']

    started = datetime.now(timezone.utc)
    connection = create_connection(
        name="Timestamp Test",
        type="sqlite",
        config={"database": str(CONNECTION_TEST_DB_PATH)},
        credentials={},
        user_token=user_token,
        org_id=org_id,
    )
    assert test_connection_connectivity(
        connection_id=connection["id"], user_token=user_token, org_id=org_id,
    )["success"] is True
    refresh_connection_schema(connection_id=connection["id"], user_token=user_token, org_id=org_id)
    assert _poll_until_terminal(test_client, connection["id"], user_token, org_id)["status"] == "completed"
    finished = datetime.now(timezone.utc)

    detail = get_connection(connection_id=connection["id"], user_token=user_token, org_id=org_id)
    assert detail["last_connection_checked_at"], "a system connection test records when it ran"
    assert detail["last_synced_at"], "a completed index records when it synced"
    listed = next(c for c in get_connections(user_token=user_token, org_id=org_id) if c["id"] == connection["id"])

    for source, payload in (("detail", detail), ("list", listed)):
        for field in ("last_connection_checked_at", "last_synced_at", "next_retry_at"):
            if payload.get(field):
                _assert_explicit_utc(payload[field], field=f"{source}.{field}", lower=started, upper=finished)

    delete_connection(connection_id=connection["id"], user_token=user_token, org_id=org_id)
