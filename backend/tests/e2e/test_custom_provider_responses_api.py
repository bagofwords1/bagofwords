import pytest


def _headers(create_user, login_user, whoami):
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


@pytest.mark.e2e
@pytest.mark.parametrize("enabled", [True, False])
def test_custom_provider_persists_responses_api_opt_in(enabled, create_user, login_user, whoami, test_client):
    """The opt-in must survive create and an edit that flips it, without
    disturbing the base_url saved alongside it."""
    headers = _headers(create_user, login_user, whoami)
    base_url = "https://nim.example.com/v1"

    created = test_client.post(
        "/api/llm/providers",
        json={
            "name": "nim gpt-oss",
            "provider_type": "custom",
            "credentials": {"base_url": base_url, "use_responses_api": enabled},
            "models": [],
        },
        headers=headers,
    )
    assert created.status_code in (200, 201), created.text
    config = created.json().get("additional_config") or {}
    assert config.get("use_responses_api") is enabled
    assert config.get("base_url") == base_url

    updated = test_client.put(
        f"/api/llm/providers/{created.json()['id']}",
        json={"credentials": {"base_url": base_url, "use_responses_api": not enabled}},
        headers=headers,
    )
    assert updated.status_code == 200, updated.text
    config = updated.json().get("additional_config") or {}
    assert config.get("use_responses_api") is (not enabled)
    assert config.get("base_url") == base_url
