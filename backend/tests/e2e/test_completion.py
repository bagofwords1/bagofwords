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
    whoami,
    create_llm_provider_and_models,
    get_default_model
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

    # Create a report first (needed for completions)
    report = create_report(
        title="Test Report",
        user_token=user_token,
        org_id=org_id,
        data_sources=[]
    )

    # Create a completion (foreground synchronous)
    completions = create_completion(
        report_id=report["id"],
        prompt="Tell me about this report",
        user_token=user_token,
        org_id=org_id,
        background=False,
    )

    # Verify v2 completion structure
    assert isinstance(completions, list)
    assert len(completions) >= 1
    system_items = [c for c in completions if c.get("role") == "system"]
    assert len(system_items) >= 1
    completion = system_items[-1]
    assert completion is not None
    assert "id" in completion
    assert completion.get("report_id") == report["id"]
    assert completion.get("model")
    assert isinstance(completion.get("completion_blocks", []), list)

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


@pytest.mark.e2e
def test_completion_background(
    create_completion,
    get_completions,
    create_report,
    create_user,
    login_user,
    whoami,
    create_llm_provider_and_models,
    get_default_model
):
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)['organizations'][0]['id']

    provider_id = create_llm_provider_and_models(user_token, org_id)
    default_model = get_default_model(user_token, org_id)
    assert len(default_model) == 1

    report = create_report(
        title="Test Report BG",
        user_token=user_token,
        org_id=org_id,
        data_sources=[]
    )

    # Create a completion in background
    completions = create_completion(
        report_id=report["id"],
        prompt="Background please",
        user_token=user_token,
        org_id=org_id,
        background=True,
    )

    # Should return at least the placeholder system entry
    assert isinstance(completions, list)
    assert any(c.get("role") == "system" for c in completions)


@pytest.mark.e2e
def test_completion_streaming(
    create_completion_stream,
    create_report,
    create_user,
    login_user,
    whoami,
    create_llm_provider_and_models,
    get_default_model
):
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)['organizations'][0]['id']

    provider_id = create_llm_provider_and_models(user_token, org_id)
    default_model = get_default_model(user_token, org_id)
    assert len(default_model) == 1

    report = create_report(
        title="Test Report Stream",
        user_token=user_token,
        org_id=org_id,
        data_sources=[]
    )

    lines = create_completion_stream(
        report_id=report["id"],
        prompt="Stream this",
        user_token=user_token,
        org_id=org_id,
    )

    saw_started = False
    saw_finished = False
    for raw in lines:
        if not raw:
            continue
        line = raw.decode() if isinstance(raw, (bytes, bytearray)) else raw
        if line.startswith("event: "):
            event = line.split(":", 1)[1].strip()
            if event == "completion.started":
                saw_started = True
            if event == "completion.finished":
                saw_finished = True
        if line.strip() == "data: [DONE]":
            break

    assert saw_started
    assert saw_finished