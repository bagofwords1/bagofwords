import os

import pytest


@pytest.mark.e2e
def test_completion_streaming_vertex(
    create_completion_stream,
    create_report,
    create_user,
    login_user,
    whoami,
    create_vertex_provider_and_models,
    get_default_model,
    test_client,
):
    """A real streamed completion on a Vertex-hosted model.

    Mirrors the Bedrock e2e test. Skipped unless a project is configured, so
    CI without GCP credentials stays green.
    """
    if not os.getenv("GOOGLE_VERTEX_PROJECT_ID"):
        pytest.skip("GOOGLE_VERTEX_PROJECT_ID environment variable not set")

    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)["organizations"][0]["id"]

    create_vertex_provider_and_models(user_token, org_id)
    default_model = get_default_model(user_token, org_id)
    assert len(default_model) == 1

    report = create_report(
        title="Vertex Stream Report",
        user_token=user_token,
        org_id=org_id,
        data_sources=[],
    )

    lines = create_completion_stream(
        report_id=report["id"],
        prompt="Stream with Vertex",
        user_token=user_token,
        org_id=org_id,
    )

    saw_started = False
    saw_finished = False
    for raw in lines:
        if not raw:
            continue
        line = raw.decode() if isinstance(raw, (bytes, bytearray)) else raw
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


@pytest.mark.e2e
def test_vertex_provider_persists_config_without_secrets_in_plaintext(
    create_user,
    login_user,
    whoami,
    test_client,
):
    """project_id/location/auth_mode are non-secret config; the key is not.

    Guards the split in LLMService._set_provider_credentials: the identifiers
    must land in additional_config (so the form can render them back), while
    the service-account key must only ever exist encrypted.
    """
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)["organizations"][0]["id"]
    headers = {
        "Authorization": f"Bearer {user_token}",
        "X-Organization-Id": str(org_id),
    }

    response = test_client.post(
        "/api/llm/providers",
        json={
            "name": "vertex adc provider",
            "provider_type": "vertex",
            "credentials": {
                "project_id": "unit-test-project",
                "location": "us-east5",
                "auth_mode": "adc",
            },
            "models": [],
        },
        headers=headers,
    )
    assert response.status_code in (200, 201), response.text
    body = response.json()
    config = body.get("additional_config") or {}
    assert config.get("project_id") == "unit-test-project"
    assert config.get("location") == "us-east5"
    assert config.get("auth_mode") == "adc"
    assert "service_account_json" not in config


@pytest.mark.e2e
def test_vertex_provider_rejects_malformed_service_account_key(
    create_user,
    login_user,
    whoami,
    test_client,
):
    """A truncated or wrong paste is the likeliest setup mistake, so it must
    fail at save time with a clear message rather than at first inference."""
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)["organizations"][0]["id"]
    headers = {
        "Authorization": f"Bearer {user_token}",
        "X-Organization-Id": str(org_id),
    }

    response = test_client.post(
        "/api/llm/providers",
        json={
            "name": "vertex bad key",
            "provider_type": "vertex",
            "credentials": {
                "project_id": "unit-test-project",
                "auth_mode": "service_account",
                "service_account_json": "-----BEGIN PRIVATE KEY-----",
            },
            "models": [],
        },
        headers=headers,
    )
    assert response.status_code == 400
    assert "json" in response.text.lower()
