import pytest
from fastapi.testclient import TestClient
from main import app
import os

@pytest.mark.e2e
def test_data_source_creation(
    create_data_source,
    get_data_sources,
    create_user,
    login_user,
    create_organization,
    test_connection
):
    # Setup user and organization
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = create_organization(user_token=user_token)

    if not os.getenv("POSTGRES_DB"):
        pytest.skip("POSTGRES_DB is not set")

    # Create a basic PostgreSQL data source
    data_source = create_data_source(
        name="Test PostgreSQL DB",
        type="postgresql",
        config={
            "host": "localhost",
            "port": 5432,
            "database": "dvdrental"
        },
        credentials={
            "user": "yochze",
            "password": "yochze"
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

    data_source_id = data_source["id"]
    test_connection = test_connection(data_source_id=data_source_id, user_token=user_token, org_id=org_id)

    breakpoint()
    assert test_connection is not None
    assert test_connection["status"] == "success"



    # Verify data source appears in list
    data_sources = get_data_sources(
        user_token=user_token,
        org_id=org_id
    )
    
    assert isinstance(data_sources, list)
    assert len(data_sources) >= 1
    assert any(ds["id"] == data_source["id"] for ds in data_sources) 