"""
E2E tests for MCP tool connections, discovery, and management.
Uses MockToolProviderClient to avoid real network calls.
"""
import pytest
from unittest.mock import patch, AsyncMock
from tests.mocks.mock_mcp_server import MockToolProviderClient, DEFAULT_MOCK_TOOLS


def _setup_user(create_user, login_user, whoami):
    """Helper to create a user and get auth context."""
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    return token, org_id


def _patch_construct_client(mock_client=None):
    """Patch ConnectionService.construct_client to return a mock tool provider."""
    client = mock_client or MockToolProviderClient()

    async def _mock_construct(self, db, connection, current_user=None, **kwargs):
        return client

    return patch(
        "app.services.connection_service.ConnectionService.construct_client",
        _mock_construct,
    ), client


# ── Connection CRUD ──────────────────────────────────────────────────

@pytest.mark.e2e
def test_create_mcp_connection(
    create_mcp_connection, create_user, login_user, whoami
):
    """Test creating an MCP connection."""
    token, org_id = _setup_user(create_user, login_user, whoami)

    conn = create_mcp_connection(
        name="Notion MCP",
        server_url="http://localhost:3000/mcp",
        user_token=token,
        org_id=org_id,
    )

    assert conn["name"] == "Notion MCP"
    assert conn["type"] == "mcp"
    assert conn["is_active"] is True


@pytest.mark.e2e
def test_create_custom_api_connection(
    create_custom_api_connection, create_user, login_user, whoami
):
    """Test creating a custom API connection."""
    token, org_id = _setup_user(create_user, login_user, whoami)

    conn = create_custom_api_connection(
        name="Internal API",
        base_url="https://api.internal.com/v1",
        user_token=token,
        org_id=org_id,
    )

    assert conn["name"] == "Internal API"
    assert conn["type"] == "custom_api"


# ── Tool Discovery ──────────────────────────────────────────────────

@pytest.mark.e2e
def test_refresh_tools_discovery(
    create_mcp_connection, refresh_connection_tools,
    create_user, login_user, whoami
):
    """Test that refresh-tools discovers tools from the mock provider."""
    token, org_id = _setup_user(create_user, login_user, whoami)
    conn = create_mcp_connection(user_token=token, org_id=org_id)

    patcher, mock_client = _patch_construct_client()
    with patcher:
        tools = refresh_connection_tools(
            connection_id=conn["id"],
            user_token=token,
            org_id=org_id,
        )

    assert len(tools) == len(DEFAULT_MOCK_TOOLS)
    tool_names = {t["name"] for t in tools}
    assert "echo" in tool_names
    assert "get_records" in tool_names
    assert "search_docs" in tool_names
    assert "failing_tool" in tool_names

    # All tools should be enabled by default
    for t in tools:
        assert t["is_enabled"] is True
        assert t["policy"] == "allow"


@pytest.mark.e2e
def test_refresh_tools_upsert(
    create_mcp_connection, refresh_connection_tools, get_connection_tools,
    create_user, login_user, whoami
):
    """Test that refresh-tools correctly creates, updates, and deletes tools."""
    token, org_id = _setup_user(create_user, login_user, whoami)
    conn = create_mcp_connection(user_token=token, org_id=org_id)

    mock_client = MockToolProviderClient()

    # First refresh: discover all tools
    patcher, _ = _patch_construct_client(mock_client)
    with patcher:
        tools_v1 = refresh_connection_tools(
            connection_id=conn["id"], user_token=token, org_id=org_id
        )
    assert len(tools_v1) == 4

    # Modify mock: remove "failing_tool", add "new_tool"
    new_tools = [t for t in DEFAULT_MOCK_TOOLS if t["name"] != "failing_tool"]
    new_tools.append({
        "name": "new_tool",
        "description": "A newly added tool",
        "input_schema": {"type": "object", "properties": {}},
        "output_schema": {},
    })
    mock_client.set_tools(new_tools)

    # Second refresh: should upsert
    patcher2, _ = _patch_construct_client(mock_client)
    with patcher2:
        tools_v2 = refresh_connection_tools(
            connection_id=conn["id"], user_token=token, org_id=org_id
        )

    tool_names_v2 = {t["name"] for t in tools_v2}
    assert "failing_tool" not in tool_names_v2  # deleted
    assert "new_tool" in tool_names_v2  # created
    assert "echo" in tool_names_v2  # retained
    assert len(tools_v2) == 4  # 3 original - 1 deleted + 1 new


# ── Tool Management ─────────────────────────────────────────────────

@pytest.mark.e2e
def test_enable_disable_tool(
    create_mcp_connection, refresh_connection_tools,
    update_connection_tool,
    create_user, login_user, whoami
):
    """Test enabling/disabling a single tool."""
    token, org_id = _setup_user(create_user, login_user, whoami)
    conn = create_mcp_connection(user_token=token, org_id=org_id)

    patcher, _ = _patch_construct_client()
    with patcher:
        tools = refresh_connection_tools(
            connection_id=conn["id"], user_token=token, org_id=org_id
        )

    echo_tool = next(t for t in tools if t["name"] == "echo")

    # Disable
    updated = update_connection_tool(
        connection_id=conn["id"],
        tool_id=echo_tool["id"],
        payload={"is_enabled": False},
        user_token=token,
        org_id=org_id,
    )
    assert updated["is_enabled"] is False

    # Re-enable
    updated2 = update_connection_tool(
        connection_id=conn["id"],
        tool_id=echo_tool["id"],
        payload={"is_enabled": True},
        user_token=token,
        org_id=org_id,
    )
    assert updated2["is_enabled"] is True


@pytest.mark.e2e
def test_batch_update_tools(
    create_mcp_connection, refresh_connection_tools,
    batch_update_connection_tools, get_connection_tools,
    create_user, login_user, whoami
):
    """Test batch enabling/disabling tools."""
    token, org_id = _setup_user(create_user, login_user, whoami)
    conn = create_mcp_connection(user_token=token, org_id=org_id)

    patcher, _ = _patch_construct_client()
    with patcher:
        tools = refresh_connection_tools(
            connection_id=conn["id"], user_token=token, org_id=org_id
        )

    tool_ids = [t["id"] for t in tools]

    # Disable all
    result = batch_update_connection_tools(
        connection_id=conn["id"],
        tool_ids=tool_ids,
        is_enabled=False,
        user_token=token,
        org_id=org_id,
    )
    assert all(t["is_enabled"] is False for t in result)

    # Re-enable all
    result2 = batch_update_connection_tools(
        connection_id=conn["id"],
        tool_ids=tool_ids,
        is_enabled=True,
        user_token=token,
        org_id=org_id,
    )
    assert all(t["is_enabled"] is True for t in result2)


@pytest.mark.e2e
def test_get_connection_tools_list(
    create_mcp_connection, refresh_connection_tools, get_connection_tools,
    create_user, login_user, whoami
):
    """Test listing tools for a connection."""
    token, org_id = _setup_user(create_user, login_user, whoami)
    conn = create_mcp_connection(user_token=token, org_id=org_id)

    patcher, _ = _patch_construct_client()
    with patcher:
        refresh_connection_tools(
            connection_id=conn["id"], user_token=token, org_id=org_id
        )

    tools = get_connection_tools(
        connection_id=conn["id"], user_token=token, org_id=org_id
    )

    assert len(tools) == 4
    for t in tools:
        assert "id" in t
        assert "name" in t
        assert "description" in t
        assert "is_enabled" in t
        assert "connection_id" in t


@pytest.mark.e2e
def test_tool_has_input_schema(
    create_mcp_connection, refresh_connection_tools, get_connection_tools,
    create_user, login_user, whoami
):
    """Test that discovered tools include their input schema."""
    token, org_id = _setup_user(create_user, login_user, whoami)
    conn = create_mcp_connection(user_token=token, org_id=org_id)

    patcher, _ = _patch_construct_client()
    with patcher:
        refresh_connection_tools(
            connection_id=conn["id"], user_token=token, org_id=org_id
        )

    tools = get_connection_tools(
        connection_id=conn["id"], user_token=token, org_id=org_id
    )

    echo_tool = next(t for t in tools if t["name"] == "echo")
    assert echo_tool["input_schema"] is not None
    assert echo_tool["input_schema"]["type"] == "object"
    assert "message" in echo_tool["input_schema"]["properties"]


@pytest.mark.e2e
def test_refresh_tools_non_mcp_connection_fails(
    create_connection, refresh_connection_tools,
    create_user, login_user, whoami, test_client,
):
    """Test that refresh-tools fails for non-MCP connections."""
    token, org_id = _setup_user(create_user, login_user, whoami)

    # Create a regular SQLite connection (no external server needed)
    conn = create_connection(
        name="Regular DB",
        type="sqlite",
        config={"database": "tests/config/chinook.sqlite"},
        credentials={},
        user_token=token,
        org_id=org_id,
    )

    # refresh-tools should fail with 400
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Organization-Id": str(org_id),
    }
    response = test_client.post(
        f"/api/connections/{conn['id']}/refresh-tools",
        headers=headers,
    )
    assert response.status_code == 400
