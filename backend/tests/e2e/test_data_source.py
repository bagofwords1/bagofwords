import pytest
import os

@pytest.mark.e2e
def test_data_source_creation(
    create_data_source,
    get_data_sources,
    test_connection,
    create_user,
    login_user,
    whoami
):
    # Setup user and organization
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)['organizations'][0]['id']

    # Skip if environment variables are not set
    if not all([
        os.getenv("TEST_POSTGRES_DB"),
        os.getenv("TEST_POSTGRES_HOST"),
        os.getenv("TEST_POSTGRES_PORT"),
        os.getenv("TEST_POSTGRES_USER"),
        os.getenv("TEST_POSTGRES_PASSWORD")
    ]):
        pytest.skip("Required TEST_POSTGRES_* environment variables are not set")

    # Create a basic PostgreSQL data source
    data_source = create_data_source(
        name="Test PostgreSQL DB",
        type="postgresql",
        config={
            "host": os.getenv("TEST_POSTGRES_HOST"),
            "port": int(os.getenv("TEST_POSTGRES_PORT")),
            "database": os.getenv("TEST_POSTGRES_DB")
        },
        credentials={
            "user": os.getenv("TEST_POSTGRES_USER"),
            "password": os.getenv("TEST_POSTGRES_PASSWORD")
        },
        user_token=user_token,
        org_id=org_id
    )
    # Basic assertions
    assert data_source is not None
    assert data_source["name"] == "Test PostgreSQL DB"
    assert data_source["type"] == "postgresql"
    assert "id" in data_source
    assert "created_at" in data_source
    assert "updated_at" in data_source
    assert data_source["is_active"] is not None

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