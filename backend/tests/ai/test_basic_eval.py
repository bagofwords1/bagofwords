import pytest
import os
import time

def validate_response_completions(completions):
    """
    Validates v2 completions (CompletionV2Schema list).
    Returns (bool, dict) tuple with validation result and statistics.
    """
    assert isinstance(completions, list), "Completions must be a list"
    assert len(completions) > 0, "Completions list cannot be empty"

    stats = {
        "total_completions": len(completions),
        "completions_with_code": 0,
        "completions_with_data_model": 0,
        "completions_with_widget": 0,
    }

    for completion in completions:
        # Basic fields in v2
        assert "id" in completion, "Completion missing required 'id' field"
        assert "status" in completion, "Completion missing required 'status' field"
        assert "role" in completion, "Completion missing required 'role' field"
        assert "report_id" in completion, "Completion missing required 'report_id' field"

        # Inspect blocks for outputs (reasoning/content and tool outputs)
        blocks = completion.get("completion_blocks") or []
        assert isinstance(blocks, list), "completion_blocks must be a list"

        for b in blocks:
            te = b.get("tool_execution") or {}
            result_json = te.get("result_json") or {}

            # Data model presence via result_json
            data_model = result_json.get("data_model")
            if isinstance(data_model, dict) and data_model.get("columns"):
                stats["completions_with_data_model"] += 1

            # Code presence via result_json (heuristics)
            code_val = result_json.get("code") or result_json.get("python") or result_json.get("sql")
            if code_val and len(str(code_val)) > 5:
                stats["completions_with_code"] += 1

            # Widget created
            created_widget = te.get("created_widget")
            if created_widget or te.get("created_widget_id"):
                stats["completions_with_widget"] += 1

    return True, stats

@pytest.mark.ai
def test_basic_eval(
    create_completion,
    get_completions,
    create_report,
    create_user,
    login_user,
    whoami,
    create_llm_provider_and_models,
    get_default_model,
    create_data_source,
    test_connection
):
    # Setup user and organization
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)['organizations'][0]['id']

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
    completions = create_completion(report_id=report["id"], prompt="List of customers in dvdrental", user_token=user_token, org_id=org_id)
    
    # Validate completions and get statistics
    is_valid, completion_stats = validate_response_completions(completions)
    assert is_valid, "Completions validation failed"
    assert completion_stats["completions_with_code"] > 0, "No completions found with valid code"
    assert completion_stats["completions_with_data_model"] > 0, "No completions found with valid data model"
    
    print(f"Completion Statistics: {completion_stats}")
    
    time_end = time.time()
    print(f"Time taken: {time_end - time_start} seconds")
    time_start = time.time()
    # Create a completion
    completions = create_completion(report_id=report["id"], prompt="Top 10 films by revenue, bar chart", user_token=user_token, org_id=org_id)
    
    # Validate completions and get statistics
    is_valid, completion_stats = validate_response_completions(completions)
    assert is_valid, "Completions validation failed"
    
    # Verify at least one completion has code and data model
    assert completion_stats["completions_with_code"] > 0, "No completions found with valid code"
    assert completion_stats["completions_with_data_model"] > 0, "No completions found with valid data model"
    
    # Optional: Print statistics for debugging
    print(f"Completion Statistics: {completion_stats}")
    
    time_end = time.time()
    print(f"Time taken: {time_end - time_start} seconds")