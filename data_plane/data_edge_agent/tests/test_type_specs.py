"""Type-spec bundle loader — drives the admin UI's type-specific forms."""

from __future__ import annotations

from ..data_sources import type_specs


def test_catalog_is_nonempty_and_sorted_by_category_then_title():
    cat = type_specs.catalog()
    assert len(cat) > 20
    keys = [(r["category"], r["title"].lower()) for r in cat]
    assert keys == sorted(keys)
    # every row carries what the picker grid needs
    for r in cat:
        assert set(r) == {"type", "title", "category"}


def test_postgresql_spec_has_expected_config_and_credentials():
    spec = type_specs.get_spec("postgresql")
    assert spec is not None
    cfg = spec["config"]["properties"]
    assert "host" in cfg and "database" in cfg and "port" in cfg
    assert cfg["port"].get("ui:type") == "number"
    assert cfg["password" if False else "host"].get("ui:type") == "string"
    default_mode = spec["auth"]["default"]
    creds = spec["credentials_by_auth"][default_mode]["properties"]
    assert "user" in creds and creds["password"]["ui:type"] == "password"


def test_file_based_type_has_file_paths_and_no_credentials():
    spec = type_specs.get_spec("qvd")
    assert spec is not None
    assert "file_paths" in spec["config"]["properties"]
    assert spec["config"]["properties"]["file_paths"]["ui:type"] == "textarea"
    # a no-auth variant with no credential fields
    mode = spec["auth"]["default"]
    assert spec["credentials_by_auth"][mode].get("properties", {}) == {}


def test_type_with_select_field_carries_options():
    spec = type_specs.get_spec("MSSQL")
    odbc = spec["config"]["properties"]["odbc_driver"]
    assert odbc["ui:type"] == "select"
    assert odbc.get("ui:options") or odbc.get("enum")  # options for the dropdown


def test_unknown_type_returns_none():
    assert type_specs.get_spec("does_not_exist") is None


def test_custom_category_excluded():
    # MCP / Custom API use a different connection flow; not in the picker.
    types = set(type_specs.spec_types())
    assert "mcp" not in types and "custom_api" not in types
