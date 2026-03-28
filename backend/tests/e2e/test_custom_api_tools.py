"""
E2E tests for custom API tool connections and management.
Uses MockToolProviderClient to avoid real network calls.
"""
import pytest
from unittest.mock import patch
from tests.mocks.mock_mcp_server import MockToolProviderClient


def _setup_user(create_user, login_user, whoami):
    """Helper to create a user and get auth context."""
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    return token, org_id


CUSTOM_API_TOOLS = [
    {
        "name": "get_customers",
        "description": "Fetch customers with optional filters",
        "input_schema": {
            "type": "object",
            "properties": {
                "status": {"type": "string", "description": "Filter by status"},
                "limit": {"type": "integer"},
            },
        },
        "output_schema": {},
    },
    {
        "name": "get_order",
        "description": "Get order details by ID",
        "input_schema": {
            "type": "object",
            "properties": {
                "order_id": {"type": "string"},
            },
            "required": ["order_id"],
        },
        "output_schema": {},
    },
]


def _patch_construct_client(tools=None):
    """Patch ConnectionService.construct_client to return a mock."""
    client = MockToolProviderClient(tools=tools or CUSTOM_API_TOOLS)

    async def _mock_construct(self, db, connection, current_user=None, **kwargs):
        return client

    return patch(
        "app.services.connection_service.ConnectionService.construct_client",
        _mock_construct,
    ), client


@pytest.mark.e2e
def test_create_custom_api_connection_with_endpoints(
    create_custom_api_connection, create_user, login_user, whoami
):
    """Test creating a custom API connection with endpoint definitions."""
    token, org_id = _setup_user(create_user, login_user, whoami)

    endpoints = [
        {
            "name": "get_customers",
            "method": "GET",
            "path": "/customers",
            "description": "Fetch customers",
            "parameters": [
                {"name": "status", "in": "query", "type": "string", "required": False},
            ],
        },
        {
            "name": "get_order",
            "method": "GET",
            "path": "/orders/{order_id}",
            "description": "Get order by ID",
            "parameters": [
                {"name": "order_id", "in": "path", "type": "string", "required": True},
            ],
        },
    ]

    conn = create_custom_api_connection(
        name="Customer API",
        base_url="https://api.example.com/v1",
        endpoints=endpoints,
        user_token=token,
        org_id=org_id,
    )

    assert conn["type"] == "custom_api"
    assert conn["name"] == "Customer API"


@pytest.mark.e2e
def test_custom_api_refresh_tools(
    create_custom_api_connection, refresh_connection_tools,
    create_user, login_user, whoami
):
    """Test tool discovery for custom API connections."""
    token, org_id = _setup_user(create_user, login_user, whoami)
    conn = create_custom_api_connection(user_token=token, org_id=org_id)

    patcher, _ = _patch_construct_client()
    with patcher:
        tools = refresh_connection_tools(
            connection_id=conn["id"],
            user_token=token,
            org_id=org_id,
        )

    assert len(tools) == 2
    tool_names = {t["name"] for t in tools}
    assert "get_customers" in tool_names
    assert "get_order" in tool_names


@pytest.mark.e2e
def test_custom_api_tool_enable_disable(
    create_custom_api_connection, refresh_connection_tools,
    update_connection_tool, get_connection_tools,
    create_user, login_user, whoami
):
    """Test enabling/disabling custom API tools."""
    token, org_id = _setup_user(create_user, login_user, whoami)
    conn = create_custom_api_connection(user_token=token, org_id=org_id)

    patcher, _ = _patch_construct_client()
    with patcher:
        tools = refresh_connection_tools(
            connection_id=conn["id"], user_token=token, org_id=org_id
        )

    # Disable get_customers
    cust_tool = next(t for t in tools if t["name"] == "get_customers")
    updated = update_connection_tool(
        connection_id=conn["id"],
        tool_id=cust_tool["id"],
        payload={"is_enabled": False},
        user_token=token,
        org_id=org_id,
    )
    assert updated["is_enabled"] is False

    # Verify via list
    all_tools = get_connection_tools(
        connection_id=conn["id"], user_token=token, org_id=org_id
    )
    cust_in_list = next(t for t in all_tools if t["name"] == "get_customers")
    assert cust_in_list["is_enabled"] is False

    # get_order should still be enabled
    order_in_list = next(t for t in all_tools if t["name"] == "get_order")
    assert order_in_list["is_enabled"] is True


@pytest.mark.e2e
def test_custom_api_tool_policy_update(
    create_custom_api_connection, refresh_connection_tools,
    update_connection_tool,
    create_user, login_user, whoami
):
    """Test updating a tool's policy."""
    token, org_id = _setup_user(create_user, login_user, whoami)
    conn = create_custom_api_connection(user_token=token, org_id=org_id)

    patcher, _ = _patch_construct_client()
    with patcher:
        tools = refresh_connection_tools(
            connection_id=conn["id"], user_token=token, org_id=org_id
        )

    tool = tools[0]
    updated = update_connection_tool(
        connection_id=conn["id"],
        tool_id=tool["id"],
        payload={"policy": "confirm"},
        user_token=token,
        org_id=org_id,
    )
    assert updated["policy"] == "confirm"


@pytest.mark.e2e
def test_custom_api_tools_persist_across_list_calls(
    create_custom_api_connection, refresh_connection_tools,
    get_connection_tools,
    create_user, login_user, whoami
):
    """Test that tools persist in the database and survive multiple list calls."""
    token, org_id = _setup_user(create_user, login_user, whoami)
    conn = create_custom_api_connection(user_token=token, org_id=org_id)

    patcher, _ = _patch_construct_client()
    with patcher:
        refresh_connection_tools(
            connection_id=conn["id"], user_token=token, org_id=org_id
        )

    # List multiple times — should return same results
    tools1 = get_connection_tools(
        connection_id=conn["id"], user_token=token, org_id=org_id
    )
    tools2 = get_connection_tools(
        connection_id=conn["id"], user_token=token, org_id=org_id
    )

    assert len(tools1) == len(tools2)
    assert {t["id"] for t in tools1} == {t["id"] for t in tools2}
