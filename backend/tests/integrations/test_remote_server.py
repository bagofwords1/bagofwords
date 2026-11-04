"""Opt-in customer environment tests; credentials supplied only through env JSON."""

import json
import os

import pytest

from app.schemas.data_source_registry import resolve_client_class


@pytest.mark.parametrize("kind", ["remote_ssh", "remote_smb"])
def test_remote_inspection_contract(kind):
    raw = os.environ.get(kind.upper() + "_TEST_CONFIG")
    if not raw:
        pytest.skip("Set " + kind.upper() + "_TEST_CONFIG with connection JSON")
    client = resolve_client_class(kind)(**json.loads(raw))
    result = client.test_connection()
    assert result["success"], result.get("message")
    tools = client.list_tools()
    assert tools and all("input_schema" in t for t in tools)
    # Real read, no arbitrary commands or changes to the customer environment.
    if client.config.allowed_paths and "list_directory" in client.config.allowed_operations:
        result = client.call_tool("list_directory", {"path": client.config.allowed_paths[0], "limit": 1})
        assert result["success"], result.get("error")
    result = client.call_tool("execute_shell", {"command": "exit 0", "working_directory": "/"})
    assert not result["success"]
