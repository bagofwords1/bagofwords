"""MCP (Model Context Protocol) test fixtures."""

import pytest


@pytest.fixture
def enable_mcp(update_organization_settings):
    """Enable MCP for an organization."""
    def _enable_mcp(user_token, org_id):
        return update_organization_settings(
            config={"mcp_enabled": {"value": True}},
            user_token=user_token,
            org_id=org_id
        )
    return _enable_mcp


@pytest.fixture
def disable_mcp(update_organization_settings):
    """Disable MCP for an organization."""
    def _disable_mcp(user_token, org_id):
        return update_organization_settings(
            config={"mcp_enabled": {"value": False}},
            user_token=user_token,
            org_id=org_id
        )
    return _disable_mcp
