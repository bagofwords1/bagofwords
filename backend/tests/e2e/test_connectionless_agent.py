import pytest


@pytest.mark.e2e
def test_create_agent_with_no_connections(
    test_client,
    create_user,
    login_user,
    whoami,
):
    """An agent (DataSource) can be created with zero connections.

    This covers the "connectionless agent" mode used for
    instruction/context-only agents created via /agents/new.
    """
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)["organizations"][0]["id"]

    headers = {
        "Authorization": f"Bearer {user_token}",
        "X-Organization-Id": str(org_id),
    }

    # No type/config and no connection_id(s) -> connectionless agent.
    payload = {
        "name": "Context Only Agent",
        "is_public": True,
        "use_llm_sync": False,
    }

    response = test_client.post("/api/data_sources", json=payload, headers=headers)
    assert response.status_code == 200, response.json()

    body = response.json()
    assert body["name"] == "Context Only Agent"
    assert "id" in body
    # No connections should be attached.
    assert body.get("connections", []) == []

    # It shows up in the listing.
    list_resp = test_client.get("/api/data_sources", headers=headers)
    assert list_resp.status_code == 200, list_resp.json()
    names = [ds["name"] for ds in list_resp.json()]
    assert "Context Only Agent" in names


@pytest.mark.e2e
def test_create_agent_with_empty_connection_ids(
    test_client,
    create_user,
    login_user,
    whoami,
):
    """Passing an explicit empty connection_ids list is also connectionless."""
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)["organizations"][0]["id"]

    headers = {
        "Authorization": f"Bearer {user_token}",
        "X-Organization-Id": str(org_id),
    }

    payload = {
        "name": "Empty Connections Agent",
        "connection_ids": [],
        "is_public": True,
    }

    response = test_client.post("/api/data_sources", json=payload, headers=headers)
    assert response.status_code == 200, response.json()
    body = response.json()
    assert body["name"] == "Empty Connections Agent"
    assert body.get("connections", []) == []


@pytest.mark.e2e
def test_create_agent_half_configured_connection_is_rejected(
    test_client,
    create_user,
    login_user,
    whoami,
):
    """Providing a type without a config is still rejected (half-configured)."""
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)["organizations"][0]["id"]

    headers = {
        "Authorization": f"Bearer {user_token}",
        "X-Organization-Id": str(org_id),
    }

    payload = {
        "name": "Broken Agent",
        "type": "postgresql",
        # config intentionally omitted
    }

    response = test_client.post("/api/data_sources", json=payload, headers=headers)
    assert response.status_code == 422, response.json()
