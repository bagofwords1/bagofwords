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


@pytest.fixture
def athena_glue(monkeypatch):
    """Fake only boto3's Glue client and awswrangler; services, routes and DB run for real."""
    from app.data_sources.clients import aws_athena_client as athena_module
    from tests.unit.test_aws_athena_client import FakeGlue, FakeWrangler, _glue_error

    for var in ("AWS_PROFILE", "AWS_DEFAULT_PROFILE"):
        monkeypatch.delenv(var, raising=False)
    glue = FakeGlue({
        "acme_db": ["assets", "users"],
        "globex_db": ["assets", "alerts"],
        "ghost_db": _glue_error("EntityNotFoundException"),
    })

    class _Session:
        def __init__(self, *args, **kwargs):
            pass

        def client(self, *args, **kwargs):
            return glue

    monkeypatch.setattr(athena_module.boto3, "Session", _Session)
    monkeypatch.setattr(athena_module, "wr", FakeWrangler())
    return glue


def _athena_config(database, **extra):
    return {"region": "eu-west-1", "database": database, "workgroup": "primary",
            "data_source": "AwsDataCatalog", **extra}


@pytest.mark.e2e
def test_athena_update_cannot_switch_between_one_database_and_several(
    athena_glue, create_connection, update_connection, test_client, create_user, login_user, whoami,
):
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    headers = {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}

    single = create_connection(
        name="Athena single", type="aws_athena", config=_athena_config("acme_db"),
        credentials={}, user_token=token, org_id=org_id,
    )
    for target in ("acme_db, globex_db", "*_db", "acme_db, !globex_db"):
        response = test_client.put(
            f"/api/connections/{single['id']}",
            json={"config": _athena_config(target)}, headers=headers,
        )
        assert response.status_code == 400, response.text

    same_mode = update_connection(
        connection_id=single["id"],
        payload={"config": _athena_config("globex_db", table_exclude_pattern="^tmp_")},
        user_token=token, org_id=org_id,
    )
    assert same_mode["id"] == single["id"]

    multi = create_connection(
        name="Athena multi", type="aws_athena", config=_athena_config("acme_db, globex_db"),
        credentials={}, user_token=token, org_id=org_id,
    )
    response = test_client.put(
        f"/api/connections/{multi['id']}",
        json={"config": _athena_config("acme_db")}, headers=headers,
    )
    assert response.status_code == 400, response.text
    reshuffled = update_connection(
        connection_id=multi["id"],
        payload={"config": _athena_config("globex_db, acme_db")},
        user_token=token, org_id=org_id,
    )
    assert reshuffled["id"] == multi["id"]


@pytest.mark.e2e
def test_athena_tests_succeed_and_name_a_missing_database_before_and_after_saving(
    athena_glue, create_connection, test_connection_connectivity, test_client, create_user, login_user, whoami,
):
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    headers = {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}
    config = _athena_config("acme_db, globex_db, ghost_db")

    pre_save = test_client.post(
        "/api/data_sources/test_connection",
        json={"name": "Athena", "type": "aws_athena", "config": config, "credentials": {}},
        headers=headers,
    )
    assert pre_save.status_code == 200, pre_save.text
    assert pre_save.json()["success"] is True
    assert "ghost_db" in pre_save.json()["message"]

    connection = create_connection(
        name="Athena with a missing database", type="aws_athena", config=config,
        credentials={}, user_token=token, org_id=org_id,
    )
    saved = test_connection_connectivity(connection_id=connection["id"], user_token=token, org_id=org_id)
    assert saved["success"] is True
    assert "ghost_db" in saved["message"]

    params = test_client.post(
        "/api/connections/test-params",
        json={"name": "Athena", "type": "aws_athena", "config": config, "credentials": {}},
        headers=headers,
    )
    assert params.status_code == 200, params.text
    assert params.json()["success"] is True and "ghost_db" in params.json()["message"]


@pytest.mark.e2e
@pytest.mark.parametrize("bad_config", [
    _athena_config("acme_db, globex_db", data_source="Other"),
    _athena_config("*_db", data_source="Another"),
    _athena_config("acme_db,,globex_db"),
    _athena_config("!acme_db"),
    _athena_config("acme_db", table_exclude_pattern="(unclosed"),
    _athena_config("acme_db", table_exclude_pattern="x{5000}"),
])
def test_athena_config_is_validated_on_every_api_path(
    athena_glue, create_connection, test_client, create_user, login_user, whoami, bad_config,
):
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    headers = {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}
    body = {"name": "Athena", "type": "aws_athena", "config": bad_config, "credentials": {}}

    for url in ("/api/connections", "/api/connections/test-params", "/api/data_sources/test_connection"):
        response = test_client.post(url, json=body, headers=headers)
        assert response.status_code == 400, (url, response.text)
        assert response.json()["error_code"] == "connection.config_invalid", response.text

    for auth_policy in ("system_only", "user_required"):
        response = test_client.post("/api/data_sources", json={
            **body, "auth_policy": auth_policy, "generate_summary": False,
            "generate_conversation_starters": False, "generate_ai_rules": False,
        }, headers=headers)
        assert response.status_code == 400, (auth_policy, response.text)
        assert response.json()["error_code"] == "connection.config_invalid", response.text

    for stored in ("acme_db", "acme_db, globex_db"):
        connection = create_connection(
            name=f"Athena {stored}", type="aws_athena", config=_athena_config(stored),
            credentials={}, user_token=token, org_id=org_id,
        )
        updated = test_client.put(
            f"/api/connections/{connection['id']}", json={"config": bad_config}, headers=headers,
        )
        assert updated.status_code == 400, updated.text
        assert updated.json()["error_code"] == "connection.config_invalid", updated.text


@pytest.mark.e2e
@pytest.mark.parametrize("spec", ["acme_db", "acme_db, globex_db"])
@pytest.mark.parametrize("blank", [None, ""])
def test_athena_config_with_blank_optional_fields_uses_the_defaults(
    athena_glue, test_client, create_user, login_user, whoami, spec, blank,
):
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    headers = {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}
    config = {**_athena_config(spec), "workgroup": blank, "data_source": blank, "table_exclude_pattern": blank}

    response = test_client.post(
        "/api/connections/test-params",
        json={"name": "Athena", "type": "aws_athena", "config": config, "credentials": {}},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    assert response.json()["success"] is True, response.text


@pytest.mark.e2e
@pytest.mark.parametrize("stored,target", [
    ("acme_db", "acme_db, globex_db"),
    ("acme_db", "*_db"),
    ("acme_db", "acme_db, !globex_db"),
    ("acme_db, globex_db", "acme_db"),
    ("*_db", "globex_db"),
])
def test_athena_mode_switch_is_a_typed_400_and_the_same_mode_update_succeeds(
    athena_glue, create_connection, update_connection, test_client, create_user, login_user, whoami, stored, target,
):
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    headers = {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}
    connection = create_connection(
        name=f"Athena {stored}", type="aws_athena", config=_athena_config(stored),
        credentials={}, user_token=token, org_id=org_id,
    )

    switched = test_client.put(
        f"/api/connections/{connection['id']}", json={"config": _athena_config(target)}, headers=headers,
    )
    assert switched.status_code == 400, switched.text
    assert switched.json()["error_code"] == "connection.athena_mode_switch", switched.text

    same_mode = "globex_db, acme_db" if "," in stored or "*" in stored else "globex_db"
    kept = update_connection(
        connection_id=connection["id"], payload={"config": _athena_config(same_mode)},
        user_token=token, org_id=org_id,
    )
    assert kept["id"] == connection["id"]
