import asyncio

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
def test_mentions_available_for_connectionless_agent(
    test_client,
    create_user,
    login_user,
    whoami,
):
    """The mentions endpoint must not 500 for a connectionless agent.

    A connectionless agent has no connection, so its data_source_type and
    auth_policy are None — the response schema must tolerate that.
    """
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)["organizations"][0]["id"]

    headers = {
        "Authorization": f"Bearer {user_token}",
        "X-Organization-Id": str(org_id),
    }

    create = test_client.post(
        "/api/data_sources",
        json={"name": "Mentionable Agent", "is_public": True},
        headers=headers,
    )
    assert create.status_code == 200, create.json()
    ds_id = create.json()["id"]

    resp = test_client.get(
        f"/api/mentions/available?data_source_ids={ds_id}", headers=headers
    )
    assert resp.status_code == 200, resp.json()
    body = resp.json()
    ds_mentions = [d for d in body.get("data_sources", []) if d["id"] == ds_id]
    assert len(ds_mentions) == 1
    # Connectionless -> no type / no auth policy, but the call still succeeds.
    assert ds_mentions[0]["data_source_type"] is None
    assert ds_mentions[0]["auth_policy"] is None


@pytest.mark.e2e
def test_llm_sync_skipped_for_connectionless_agent(
    test_client,
    create_user,
    login_user,
    whoami,
):
    """llm_sync must be a no-op for a connectionless agent, even if use_llm_sync=True."""
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)["organizations"][0]["id"]

    headers = {
        "Authorization": f"Bearer {user_token}",
        "X-Organization-Id": str(org_id),
    }

    # Explicitly request use_llm_sync=True — the agent is still connectionless.
    create = test_client.post(
        "/api/data_sources",
        json={"name": "No LLM Agent", "is_public": True, "use_llm_sync": True},
        headers=headers,
    )
    assert create.status_code == 200, create.json()
    ds_id = create.json()["id"]

    resp = test_client.post(f"/api/data_sources/{ds_id}/llm_sync", headers=headers)
    assert resp.status_code == 200, resp.json()
    body = resp.json()
    assert body.get("skipped") is True
    # No generated onboarding artifacts.
    assert "onboarding_instruction" not in body


@pytest.mark.e2e
def test_construct_clients_empty_for_connectionless_agent(
    test_client,
    create_user,
    login_user,
    whoami,
):
    """construct_clients returns {} (no raise) for a connectionless agent, so a
    completion can still run with a tools-only / context-only agent."""
    from sqlalchemy import select
    from app.dependencies import async_session_maker
    from app.models.data_source import DataSource
    from app.services.data_source_service import DataSourceService

    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)["organizations"][0]["id"]
    headers = {
        "Authorization": f"Bearer {user_token}",
        "X-Organization-Id": str(org_id),
    }

    create = test_client.post(
        "/api/data_sources",
        json={"name": "Tools Only Agent", "is_public": True},
        headers=headers,
    )
    assert create.status_code == 200, create.json()
    ds_id = create.json()["id"]

    async def _check():
        async with async_session_maker() as db:
            ds = (
                await db.execute(select(DataSource).where(DataSource.id == ds_id))
            ).scalar_one()
            return await DataSourceService().construct_clients(db, ds, None)

    clients = asyncio.run(_check())
    assert clients == {}


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
