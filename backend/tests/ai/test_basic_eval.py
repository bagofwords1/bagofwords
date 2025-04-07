import pytest
from fastapi.testclient import TestClient
from main import app
from tests.utils.user_creds import main_user
import os
import time

@pytest.mark.ai
def test_basic_eval(
    create_completion,
    get_completions,
    create_report,
    create_user,
    login_user,
    create_organization,
    create_llm_provider_and_models,
    get_default_model,
    create_data_source,
    test_connection
):
    # Setup user and organization
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = create_organization(user_token=user_token)

    # Skip if OPENAI_API_KEY_TEST is not set
    if not os.getenv("OPENAI_API_KEY_TEST"):
        pytest.skip("OPENAI_API_KEY_TEST is not set")

    provider_id = create_llm_provider_and_models(user_token, org_id)
    default_model = get_default_model(user_token, org_id)

    assert len(default_model) == 1

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

    # Create a report first (needed for completions)
    report = create_report(
        title="Test Report",
        user_token=user_token,
        org_id=org_id,
        data_sources=[data_source["id"]]
    )

    time_start = time.time()
    # Create a completion
    completion = create_completion(report_id=report["id"], prompt="List of customers in dvdrental", user_token=user_token, org_id=org_id)
    # Verify completion structure
    assert completion is not None
    assert "id" in completion
    assert "status" in completion
    assert completion["role"] == "system"
    assert len(completion['completion']['content']) > 0
    assert len(completion['completion']['reasoning']) > 0

    # Widget validation
    assert completion['widget']['title'] is not None

    # Step validation
    assert completion['step']['data_model'] is not None
    assert len(completion['step']['data_model']['columns']) > 0
    assert len(completion['step']['code']) > 10


    completion_bar_chart = create_completion(report_id=report["id"], prompt="Top 10 films by revenue, bar chart", user_token=user_token, org_id=org_id)
    assert completion_bar_chart is not None
    assert "id" in completion_bar_chart
    assert "status" in completion_bar_chart
    assert completion_bar_chart["role"] == "system"
    assert len(completion_bar_chart['completion']['content']) > 0
    assert len(completion_bar_chart['completion']['reasoning']) > 0

    # Widget validation
    assert completion_bar_chart['widget']['title'] is not None

    # Step validation
    assert completion_bar_chart['step']['data_model'] is not None
    assert len(completion_bar_chart['step']['data_model']['columns']) > 0
    assert len(completion_bar_chart['step']['code']) > 10

    time_end = time.time()
    print(f"Time taken: {time_end - time_start} seconds")