"""Phase 0 runtime: native integration tool dispatch + tool-provider typing."""
import pytest

from app.data_sources.clients.gmail_client import GmailClient
from app.schemas.data_source_registry import tool_provider_types


def test_gmail_is_a_tool_provider_type():
    types = tool_provider_types()
    assert "gmail" in types
    assert "mcp" in types
    assert "custom_api" in types
    # data sources are not tool providers
    assert "postgresql" not in types
    assert "snowflake" not in types


@pytest.mark.asyncio
async def test_gmail_acall_tool_dispatches_to_method():
    c = GmailClient(access_token="fake")
    c._get = lambda path, params=None: (
        {"messages": [{"id": "m1"}, {"id": "m2"}]} if "messages" in path
        else {"labels": [{"name": "INBOX"}]}
    )
    r = await c.acall_tool("search_messages", {"query": "is:unread"})
    assert r["success"] is True
    assert r["content_type"] == "json"
    assert r["data"] == [{"id": "m1"}, {"id": "m2"}]

    labels = await c.acall_tool("list_labels", {})
    assert labels["success"] is True
    assert labels["data"] == [{"name": "INBOX"}]


@pytest.mark.asyncio
async def test_gmail_acall_tool_rejects_unknown_tool():
    c = GmailClient(access_token="fake")
    r = await c.acall_tool("definitely_not_a_tool", {})
    assert r["success"] is False
    assert "Unknown Gmail tool" in r["error"]
