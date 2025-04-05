import pytest
from fastapi.testclient import TestClient
from main import app
from tests.utils.user_creds import main_user
import os

@pytest.mark.e2e
def test_completion_creation(
    create_completion,
    get_completions,
    create_report,
    create_user,
    login_user,
    create_organization,
    create_llm_provider_and_models,
    get_default_model
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

    # Create a report first (needed for completions)
    report = create_report(
        title="Test Report",
        user_token=user_token,
        org_id=org_id,
        data_sources=[]
    )

    # Create a completion
    completion = create_completion(report_id=report["id"], prompt="Tell me about this report", user_token=user_token, org_id=org_id)

    # Verify completion structure
    assert completion is not None
    assert "id" in completion
    assert "status" in completion
    assert completion["role"] == "system"  # The response should be from the system
    assert completion["report_id"] == report["id"]
    assert "model" in completion

    # Get all completions for the report
    completions = get_completions(
        report_id=report["id"],
        user_token=user_token,
        org_id=org_id
    )

    # Verify completions list
    assert isinstance(completions, list)
    assert len(completions) >= 1  # Should have at least our created completion
    assert any(c["id"] == completion["id"] for c in completions) 