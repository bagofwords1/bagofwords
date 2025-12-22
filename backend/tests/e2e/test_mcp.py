"""End-to-end tests for MCP (Model Context Protocol) API routes."""

import pytest
from pathlib import Path


# Path to demo database for tests requiring data sources
CHINOOK_DB_PATH = (Path(__file__).resolve().parent.parent.parent / "demo-datasources" / "chinook.sqlite").resolve()


# ============================================================================
# Authentication and Authorization Tests
# ============================================================================

@pytest.mark.e2e
def test_mcp_requires_api_key(test_client, create_user, login_user):
    """Verify MCP endpoints reject requests without valid API key."""
    # Setup user to ensure DB is populated
    user = create_user()
    login_user(user["email"], user["password"])

    # Without any API key, get 400 (org ID missing - checked before auth)
    response = test_client.get("/api/mcp")
    assert response.status_code == 400
    assert "organization" in response.json()["detail"].lower()

    # POST endpoint without API key
    response = test_client.post(
        "/api/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "initialize"}
    )
    assert response.status_code == 400

    # REST tools endpoint without API key
    response = test_client.get("/api/mcp/tools")
    assert response.status_code == 400


@pytest.mark.e2e
def test_mcp_rejects_invalid_api_key(test_client, create_user, login_user):
    """Invalid API keys starting with bow_ are rejected with 401."""
    # Setup user to ensure DB is populated
    user = create_user()
    login_user(user["email"], user["password"])

    # Try with invalid API key that starts with bow_
    response = test_client.get(
        "/api/mcp",
        headers={"X-API-Key": "bow_invalid_key_that_does_not_exist"}
    )
    assert response.status_code == 401
    assert "invalid" in response.json()["detail"].lower() or "expired" in response.json()["detail"].lower()


@pytest.mark.e2e
def test_mcp_disabled_returns_403(
    disable_mcp,
    test_client,
    create_api_key,
    create_user,
    login_user,
    whoami
):
    """When MCP is disabled, endpoints return 403."""
    # Setup
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)['organizations'][0]['id']
    api_key = create_api_key(user_token=user_token, org_id=org_id)["key"]
    
    # Verify the API key starts with bow_
    assert api_key.startswith("bow_"), f"API key should start with bow_, got: {api_key[:20]}..."

    # Disable MCP
    disable_mcp(user_token=user_token, org_id=org_id)

    # Verify GET returns 403
    response = test_client.get(
        "/api/mcp",
        headers={"X-API-Key": api_key}
    )
    assert response.status_code == 403, f"Expected 403, got {response.status_code}: {response.json()}"
    assert "not enabled" in response.json()["detail"].lower()

    # Verify POST returns 403
    response = test_client.post(
        "/api/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers={"X-API-Key": api_key}
    )
    assert response.status_code == 403


# ============================================================================
# MCP Enable/Disable Settings Tests
# ============================================================================

@pytest.mark.e2e
def test_mcp_enable_disable_toggle(
    enable_mcp,
    disable_mcp,
    test_client,
    create_api_key,
    create_user,
    login_user,
    whoami
):
    """Test that MCP can be enabled and disabled via organization settings."""
    # Setup
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)['organizations'][0]['id']
    api_key = create_api_key(user_token=user_token, org_id=org_id)["key"]
    
    # Verify API key format
    assert api_key.startswith("bow_"), f"API key should start with bow_, got: {api_key[:20]}..."

    # Enable MCP and verify it works
    enable_mcp(user_token=user_token, org_id=org_id)
    response = test_client.post(
        "/api/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers={"X-API-Key": api_key}
    )
    assert response.status_code == 200, f"Expected 200, got {response.status_code}: {response.json()}"

    # Disable MCP and verify 403
    disable_mcp(user_token=user_token, org_id=org_id)
    response = test_client.post(
        "/api/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers={"X-API-Key": api_key}
    )
    assert response.status_code == 403


@pytest.mark.e2e
def test_mcp_reenable_after_disable(
    enable_mcp,
    disable_mcp,
    test_client,
    create_api_key,
    create_user,
    login_user,
    whoami
):
    """Disable then re-enable MCP, verify it works again."""
    # Setup
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)['organizations'][0]['id']
    api_key = create_api_key(user_token=user_token, org_id=org_id)["key"]

    def mcp_post(method):
        return test_client.post(
            "/api/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": method},
            headers={"X-API-Key": api_key}
        )

    # Enable MCP initially
    enable_mcp(user_token=user_token, org_id=org_id)
    response = mcp_post("tools/list")
    assert response.status_code == 200, f"Expected 200, got {response.status_code}: {response.json()}"

    # Disable MCP
    disable_mcp(user_token=user_token, org_id=org_id)
    response = mcp_post("tools/list")
    assert response.status_code == 403

    # Re-enable MCP
    enable_mcp(user_token=user_token, org_id=org_id)
    response = mcp_post("tools/list")
    assert response.status_code == 200

    # Verify response is valid
    data = response.json()
    assert data["jsonrpc"] == "2.0"
    assert "result" in data
    assert "tools" in data["result"]


# ============================================================================
# MCP Protocol Tests
# ============================================================================

@pytest.mark.e2e
def test_mcp_get_server_info(
    enable_mcp,
    test_client,
    create_api_key,
    create_user,
    login_user,
    whoami
):
    """GET /mcp returns server info (protocol version, capabilities)."""
    # Setup
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)['organizations'][0]['id']
    api_key = create_api_key(user_token=user_token, org_id=org_id)["key"]

    # Enable MCP
    enable_mcp(user_token=user_token, org_id=org_id)

    # Test GET endpoint
    response = test_client.get("/api/mcp", headers={"X-API-Key": api_key})
    assert response.status_code == 200, f"Expected 200, got {response.status_code}: {response.json()}"

    data = response.json()
    assert data["jsonrpc"] == "2.0"
    assert "result" in data
    result = data["result"]
    assert result["protocolVersion"] == "2024-11-05"
    assert result["serverInfo"]["name"] == "bagofwords"
    assert result["serverInfo"]["version"] == "1.0.0"
    assert "capabilities" in result
    assert "tools" in result["capabilities"]


@pytest.mark.e2e
def test_mcp_initialize(
    enable_mcp,
    test_client,
    create_api_key,
    create_user,
    login_user,
    whoami
):
    """POST with `initialize` method returns handshake response."""
    # Setup
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)['organizations'][0]['id']
    api_key = create_api_key(user_token=user_token, org_id=org_id)["key"]

    # Enable MCP
    enable_mcp(user_token=user_token, org_id=org_id)

    # Test initialize method
    response = test_client.post(
        "/api/mcp",
        json={"jsonrpc": "2.0", "id": 42, "method": "initialize"},
        headers={"X-API-Key": api_key}
    )
    assert response.status_code == 200, f"Expected 200, got {response.status_code}: {response.json()}"

    data = response.json()
    assert data["jsonrpc"] == "2.0"
    assert data["id"] == 42
    assert "result" in data
    result = data["result"]
    assert result["protocolVersion"] == "2024-11-05"
    assert result["serverInfo"]["name"] == "bagofwords"
    assert "capabilities" in result


@pytest.mark.e2e
def test_mcp_tools_list(
    enable_mcp,
    test_client,
    create_api_key,
    create_user,
    login_user,
    whoami
):
    """POST with `tools/list` returns available tools with correct schema."""
    # Setup
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)['organizations'][0]['id']
    api_key = create_api_key(user_token=user_token, org_id=org_id)["key"]

    # Enable MCP
    enable_mcp(user_token=user_token, org_id=org_id)

    # Test tools/list method
    response = test_client.post(
        "/api/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers={"X-API-Key": api_key}
    )
    assert response.status_code == 200, f"Expected 200, got {response.status_code}: {response.json()}"

    data = response.json()
    assert data["jsonrpc"] == "2.0"
    assert "result" in data
    assert "tools" in data["result"]

    tools = data["result"]["tools"]
    assert len(tools) == 4

    tool_names = [t["name"] for t in tools]
    assert "create_report" in tool_names
    assert "get_context" in tool_names
    assert "inspect_data" in tool_names
    assert "create_data" in tool_names

    # Verify each tool has required fields
    for tool in tools:
        assert "name" in tool
        assert "description" in tool
        assert "inputSchema" in tool
        assert isinstance(tool["inputSchema"], dict)


@pytest.mark.e2e
def test_mcp_invalid_method(
    enable_mcp,
    test_client,
    create_api_key,
    create_user,
    login_user,
    whoami
):
    """Unknown methods return -32601 error."""
    # Setup
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)['organizations'][0]['id']
    api_key = create_api_key(user_token=user_token, org_id=org_id)["key"]

    # Enable MCP
    enable_mcp(user_token=user_token, org_id=org_id)

    # Test unknown method
    response = test_client.post(
        "/api/mcp",
        json={"jsonrpc": "2.0", "id": 99, "method": "unknown/method"},
        headers={"X-API-Key": api_key}
    )
    assert response.status_code == 200  # JSON-RPC returns 200 with error in body

    data = response.json()
    assert data["jsonrpc"] == "2.0"
    assert data["id"] == 99
    assert "error" in data
    assert data["error"]["code"] == -32601
    assert "not found" in data["error"]["message"].lower()


@pytest.mark.e2e
def test_mcp_invalid_json(
    enable_mcp,
    test_client,
    create_api_key,
    create_user,
    login_user,
    whoami
):
    """Malformed JSON returns -32700 parse error."""
    # Setup
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)['organizations'][0]['id']
    api_key = create_api_key(user_token=user_token, org_id=org_id)["key"]

    # Enable MCP
    enable_mcp(user_token=user_token, org_id=org_id)

    # Send invalid JSON
    response = test_client.post(
        "/api/mcp",
        content="not valid json {{{",
        headers={
            "X-API-Key": api_key,
            "Content-Type": "application/json"
        }
    )
    assert response.status_code == 200  # JSON-RPC returns 200 with error

    data = response.json()
    assert data["jsonrpc"] == "2.0"
    assert "error" in data
    assert data["error"]["code"] == -32700


@pytest.mark.e2e
def test_mcp_rest_tools_endpoint(
    enable_mcp,
    test_client,
    create_api_key,
    create_user,
    login_user,
    whoami
):
    """GET /mcp/tools REST endpoint works."""
    # Setup
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)['organizations'][0]['id']
    api_key = create_api_key(user_token=user_token, org_id=org_id)["key"]

    # Enable MCP
    enable_mcp(user_token=user_token, org_id=org_id)

    # Test REST endpoint
    response = test_client.get("/api/mcp/tools", headers={"X-API-Key": api_key})
    assert response.status_code == 200, f"Expected 200, got {response.status_code}: {response.json()}"

    data = response.json()
    assert "tools" in data

    tools = data["tools"]
    assert len(tools) == 4

    tool_names = [t["name"] for t in tools]
    assert "create_report" in tool_names
    assert "get_context" in tool_names
    assert "inspect_data" in tool_names
    assert "create_data" in tool_names


# ============================================================================
# Tool Tests - create_report
# ============================================================================

@pytest.mark.e2e
def test_mcp_create_report(
    enable_mcp,
    test_client,
    create_api_key,
    create_user,
    login_user,
    whoami
):
    """Creates a report and returns report_id, url, data_sources."""
    # Setup
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)['organizations'][0]['id']
    api_key = create_api_key(user_token=user_token, org_id=org_id)["key"]

    # Enable MCP
    enable_mcp(user_token=user_token, org_id=org_id)

    # Call create_report tool
    response = test_client.post(
        "/api/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "create_report", "arguments": {"title": "Test MCP Report"}}
        },
        headers={"X-API-Key": api_key}
    )
    assert response.status_code == 200, f"Expected 200, got {response.status_code}: {response.json()}"

    data = response.json()
    assert data["jsonrpc"] == "2.0"
    assert "result" in data
    assert data["result"]["isError"] is False

    # Parse the content (it's returned as text)
    content = data["result"]["content"]
    assert len(content) == 1
    assert content[0]["type"] == "text"

    # The result is a string representation of a dict
    result_text = content[0]["text"]
    assert "report_id" in result_text
    assert "title" in result_text
    assert "Test MCP Report" in result_text
    assert "url" in result_text


@pytest.mark.e2e
def test_mcp_create_report_with_custom_title(
    enable_mcp,
    test_client,
    create_api_key,
    create_user,
    login_user,
    whoami
):
    """Custom title is used in the created report."""
    # Setup
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)['organizations'][0]['id']
    api_key = create_api_key(user_token=user_token, org_id=org_id)["key"]

    # Enable MCP
    enable_mcp(user_token=user_token, org_id=org_id)

    # Call create_report with specific title
    custom_title = "Q4 Revenue Analysis Dashboard"
    response = test_client.post(
        "/api/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "create_report", "arguments": {"title": custom_title}}
        },
        headers={"X-API-Key": api_key}
    )
    assert response.status_code == 200, f"Expected 200, got {response.status_code}: {response.json()}"

    data = response.json()
    assert data["result"]["isError"] is False
    result_text = data["result"]["content"][0]["text"]
    assert custom_title in result_text


@pytest.mark.e2e
def test_mcp_tools_call_missing_tool_name(
    enable_mcp,
    test_client,
    create_api_key,
    create_user,
    login_user,
    whoami
):
    """tools/call without tool name returns error."""
    # Setup
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)['organizations'][0]['id']
    api_key = create_api_key(user_token=user_token, org_id=org_id)["key"]

    # Enable MCP
    enable_mcp(user_token=user_token, org_id=org_id)

    # Call tools/call without name
    response = test_client.post(
        "/api/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {}},
        headers={"X-API-Key": api_key}
    )
    assert response.status_code == 200

    data = response.json()
    assert "error" in data
    assert data["error"]["code"] == -32602
    assert "missing" in data["error"]["message"].lower()


@pytest.mark.e2e
def test_mcp_tools_call_unknown_tool(
    enable_mcp,
    test_client,
    create_api_key,
    create_user,
    login_user,
    whoami
):
    """tools/call with unknown tool name returns error."""
    # Setup
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)['organizations'][0]['id']
    api_key = create_api_key(user_token=user_token, org_id=org_id)["key"]

    # Enable MCP
    enable_mcp(user_token=user_token, org_id=org_id)

    # Call unknown tool
    response = test_client.post(
        "/api/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "unknown_tool", "arguments": {}}
        },
        headers={"X-API-Key": api_key}
    )
    assert response.status_code == 200

    data = response.json()
    assert "error" in data
    assert data["error"]["code"] == -32602
    assert "unknown" in data["error"]["message"].lower()


# ============================================================================
# Tool Tests - get_context
# ============================================================================

@pytest.mark.e2e
def test_mcp_get_context(
    enable_mcp,
    test_client,
    create_api_key,
    install_demo_data_source,
    create_user,
    login_user,
    whoami
):
    """get_context returns data sources and tables for a report."""
    if not CHINOOK_DB_PATH.exists():
        pytest.skip(f"Chinook demo database missing at {CHINOOK_DB_PATH}")

    # Setup
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)['organizations'][0]['id']
    api_key = create_api_key(user_token=user_token, org_id=org_id)["key"]

    def mcp_tool_call(tool_name, arguments):
        return test_client.post(
            "/api/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": tool_name, "arguments": arguments}
            },
            headers={"X-API-Key": api_key}
        )

    # Enable MCP
    enable_mcp(user_token=user_token, org_id=org_id)

    # Install demo data source
    install_demo_data_source(
        demo_id="chinook",
        user_token=user_token,
        org_id=org_id,
    )
    # No need to track data_source_id - cleanup handled by run_migrations fixture

    # Create a report first
    report_response = mcp_tool_call("create_report", {"title": "Context Test Report"})
    assert report_response.status_code == 200, f"Expected 200, got {report_response.status_code}: {report_response.json()}"

    # Extract report_id from the response
    report_result_text = report_response.json()["result"]["content"][0]["text"]
    # Parse the report_id from the string representation
    import ast
    report_result = ast.literal_eval(report_result_text)
    report_id = report_result["report_id"]

    # Call get_context
    context_response = mcp_tool_call("get_context", {"report_id": report_id})
    assert context_response.status_code == 200

    data = context_response.json()
    assert data["result"]["isError"] is False

    context_text = data["result"]["content"][0]["text"]
    # Verify context contains expected data
    assert "data_sources" in context_text
    assert "Chinook" in context_text or "chinook" in context_text.lower()
    # No manual cleanup needed - database reset by run_migrations fixture


@pytest.mark.e2e
def test_mcp_get_context_with_patterns(
    enable_mcp,
    test_client,
    create_api_key,
    install_demo_data_source,
    create_user,
    login_user,
    whoami
):
    """get_context with regex patterns filters results."""
    if not CHINOOK_DB_PATH.exists():
        pytest.skip(f"Chinook demo database missing at {CHINOOK_DB_PATH}")

    # Setup
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)['organizations'][0]['id']
    api_key = create_api_key(user_token=user_token, org_id=org_id)["key"]

    def mcp_tool_call(tool_name, arguments):
        return test_client.post(
            "/api/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": tool_name, "arguments": arguments}
            },
            headers={"X-API-Key": api_key}
        )

    # Enable MCP
    enable_mcp(user_token=user_token, org_id=org_id)

    # Install demo data source
    install_demo_data_source(
        demo_id="chinook",
        user_token=user_token,
        org_id=org_id,
    )
    # No need to track data_source_id - cleanup handled by run_migrations fixture

    # Create a report first
    report_response = mcp_tool_call("create_report", {"title": "Pattern Test Report"})
    assert report_response.status_code == 200, f"Expected 200, got {report_response.status_code}: {report_response.json()}"

    # Extract report_id
    import ast
    report_result_text = report_response.json()["result"]["content"][0]["text"]
    report_result = ast.literal_eval(report_result_text)
    report_id = report_result["report_id"]

    # Call get_context with pattern filter
    context_response = mcp_tool_call("get_context", {"report_id": report_id, "patterns": ["album", "artist"]})
    assert context_response.status_code == 200

    data = context_response.json()
    assert data["result"]["isError"] is False
    # No manual cleanup needed - database reset by run_migrations fixture


# ============================================================================
# Tool Tests - inspect_data (error case only)
# ============================================================================

@pytest.mark.e2e
def test_mcp_inspect_data_no_llm(
    enable_mcp,
    test_client,
    create_api_key,
    install_demo_data_source,
    create_user,
    login_user,
    whoami
):
    """inspect_data returns error when no LLM is configured."""
    if not CHINOOK_DB_PATH.exists():
        pytest.skip(f"Chinook demo database missing at {CHINOOK_DB_PATH}")

    # Setup
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)['organizations'][0]['id']
    api_key = create_api_key(user_token=user_token, org_id=org_id)["key"]

    def mcp_tool_call(tool_name, arguments):
        return test_client.post(
            "/api/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": tool_name, "arguments": arguments}
            },
            headers={"X-API-Key": api_key}
        )

    # Enable MCP
    enable_mcp(user_token=user_token, org_id=org_id)

    # Install demo data source
    install_demo_data_source(
        demo_id="chinook",
        user_token=user_token,
        org_id=org_id,
    )
    # No need to track data_source_id - cleanup handled by run_migrations fixture

    # Create a report first
    report_response = mcp_tool_call("create_report", {"title": "Inspect Test Report"})
    assert report_response.status_code == 200, f"Expected 200, got {report_response.status_code}: {report_response.json()}"

    # Extract report_id
    import ast
    report_result_text = report_response.json()["result"]["content"][0]["text"]
    report_result = ast.literal_eval(report_result_text)
    report_id = report_result["report_id"]

    # Call inspect_data - should fail due to no LLM
    inspect_response = mcp_tool_call("inspect_data", {"report_id": report_id, "prompt": "Show me all albums"})
    assert inspect_response.status_code == 200

    data = inspect_response.json()
    # The tool should return an error about no LLM configured
    # It may be in isError or in the text content
    content_text = data["result"]["content"][0]["text"]
    # Either isError is True or the content indicates an error
    assert data["result"]["isError"] is True or "error" in content_text.lower() or "no default" in content_text.lower() or "llm" in content_text.lower()
    # No manual cleanup needed - database reset by run_migrations fixture


# ============================================================================
# Tool Tests - create_data (error case only)
# ============================================================================

@pytest.mark.e2e
def test_mcp_create_data_no_llm(
    enable_mcp,
    test_client,
    create_api_key,
    install_demo_data_source,
    create_user,
    login_user,
    whoami
):
    """create_data returns error when no LLM is configured."""
    if not CHINOOK_DB_PATH.exists():
        pytest.skip(f"Chinook demo database missing at {CHINOOK_DB_PATH}")

    # Setup
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)['organizations'][0]['id']
    api_key = create_api_key(user_token=user_token, org_id=org_id)["key"]

    def mcp_tool_call(tool_name, arguments):
        return test_client.post(
            "/api/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": tool_name, "arguments": arguments}
            },
            headers={"X-API-Key": api_key}
        )

    # Enable MCP
    enable_mcp(user_token=user_token, org_id=org_id)

    # Install demo data source
    install_demo_data_source(
        demo_id="chinook",
        user_token=user_token,
        org_id=org_id,
    )
    # No need to track data_source_id - cleanup handled by run_migrations fixture

    # Create a report first
    report_response = mcp_tool_call("create_report", {"title": "Create Data Test Report"})
    assert report_response.status_code == 200, f"Expected 200, got {report_response.status_code}: {report_response.json()}"

    # Extract report_id
    import ast
    report_result_text = report_response.json()["result"]["content"][0]["text"]
    report_result = ast.literal_eval(report_result_text)
    report_id = report_result["report_id"]

    # Call create_data - should fail due to no LLM
    create_response = mcp_tool_call("create_data", {"report_id": report_id, "prompt": "Show me total sales by artist"})
    assert create_response.status_code == 200

    data = create_response.json()
    # The tool should return an error about no LLM configured
    content_text = data["result"]["content"][0]["text"]
    # Either isError is True or the content indicates an error
    assert data["result"]["isError"] is True or "error" in content_text.lower() or "no default" in content_text.lower() or "llm" in content_text.lower()
    # No manual cleanup needed - database reset by run_migrations fixture
