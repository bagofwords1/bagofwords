import os
import pytest


@pytest.mark.e2e
def test_completion_streaming_azure(
    create_completion_stream,
    create_report,
    create_user,
    login_user,
    create_organization,
    create_llm_provider_and_models,
    get_default_model,
    test_client,
):
    # Skip if Azure env not present
    required_env = [
        "AZURE_OPENAI_API_KEY",
        "AZURE_OPENAI_ENDPOINT",
        "AZURE_OPENAI_DEPLOYMENT",
    ]
    if any(not os.getenv(k) for k in required_env):
        pytest.skip("Azure environment variables not set")

    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = create_organization(user_token=user_token)

    # Use existing fixture to create providers/models; test env should configure Azure as default
    create_llm_provider_and_models(user_token, org_id)
    default_model = get_default_model(user_token, org_id)
    assert len(default_model) == 1

    report = create_report(
        title="Azure Stream Report",
        user_token=user_token,
        org_id=org_id,
        data_sources=[],
    )

    # Start streaming; ensure we handle heartbeats/empty frames gracefully
    lines = create_completion_stream(
        report_id=report["id"],
        prompt="Stream with Azure",
        user_token=user_token,
        org_id=org_id,
    )

    saw_started = False
    saw_finished = False
    for raw in lines:
        if not raw:
            continue
        line = raw.decode() if isinstance(raw, (bytes, bytearray)) else raw
        # Allow heartbeats or empty data lines to pass without failure
        if line.startswith(":"):
            continue
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


