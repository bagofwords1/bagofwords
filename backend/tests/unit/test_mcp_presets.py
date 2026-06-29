"""Unit tests for the MCP connector presets, data_shape license gate, and DCR SSRF guard."""
import pytest

from app.schemas.data_source_registry import (
    mcp_presets, mcp_preset, allowed_dcr_hosts,
)
from app.services.connection_service import _user_auth_needs_enterprise


def test_presets_include_default_dcr_set():
    keys = {p["key"] for p in mcp_presets()}
    assert {"monday", "notion", "atlassian", "linear", "sentry"} <= keys


def test_dcr_presets_are_oauth():
    # The zero-setup (DCR) set connects via per-user OAuth.
    for key in ("monday", "notion", "atlassian", "linear", "sentry"):
        assert mcp_preset(key).auth == "oauth"
    # GitHub/Gmail need an OAuth app; Supabase needs a token.
    assert mcp_preset("github").auth == "oauth_app"
    assert mcp_preset("gmail").auth == "oauth_app"
    assert mcp_preset("supabase").auth == "bearer"


def test_license_gate_is_data_shape_scoped():
    # Integrations (tools/files/objects) → per-user auth is free.
    assert _user_auth_needs_enterprise("mcp") is False
    assert _user_auth_needs_enterprise("onedrive") is False
    # Warehouses/databases (tables) → Enterprise.
    assert _user_auth_needs_enterprise("postgresql") is True
    # Unknown type → conservative (gated).
    assert _user_auth_needs_enterprise("totally_unknown_type") is True


def test_dcr_allowlist_includes_preset_hosts_only():
    hosts = allowed_dcr_hosts()
    assert "mcp.notion.com" in hosts and "mcp.monday.com" in hosts
    assert "auth.atlassian.com" in hosts  # AS host differs from resource host
    assert "evil.example.com" not in hosts
