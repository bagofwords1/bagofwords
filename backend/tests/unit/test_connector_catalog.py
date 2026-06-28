"""Unit tests for the connector catalog, data_shape license gate, and DCR SSRF guard."""
import pytest

from app.schemas.connector_catalog import (
    list_catalog, get_catalog_entry, auto_seed_entries, allowed_dcr_hosts,
)
from app.services.connection_service import _user_auth_needs_enterprise


def test_catalog_has_default_dcr_set():
    keys = {e["key"] for e in list_catalog()}
    assert {"monday", "notion", "atlassian", "linear", "sentry"} <= keys


def test_auto_seed_is_the_dcr_set():
    seeded = {e.key for e in auto_seed_entries()}
    assert {"monday", "notion", "atlassian", "linear", "sentry"} == seeded
    # GitHub/Gmail need a client; Supabase needs a token — not auto-seeded.
    assert "github" not in seeded and "gmail" not in seeded and "supabase" not in seeded


def test_default_dcr_entries_are_oauth_tools():
    for key in ("monday", "notion", "linear", "sentry"):
        e = get_catalog_entry(key)
        assert e.auth == "oauth" and e.data_shape == "tools" and e.ready_out_of_box


def test_license_gate_is_data_shape_scoped():
    # Integrations (tools/files/objects) → per-user auth is free.
    assert _user_auth_needs_enterprise("mcp") is False
    assert _user_auth_needs_enterprise("onedrive") is False
    # Warehouses/databases (tables) → Enterprise.
    assert _user_auth_needs_enterprise("postgresql") is True
    # Unknown type → conservative (gated).
    assert _user_auth_needs_enterprise("totally_unknown_type") is True


def test_dcr_allowlist_includes_catalog_hosts_only():
    hosts = allowed_dcr_hosts()
    assert "mcp.notion.com" in hosts and "mcp.monday.com" in hosts
    assert "auth.atlassian.com" in hosts  # AS host differs from resource host
    assert "evil.example.com" not in hosts
