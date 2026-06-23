"""Unit tests for the Integrations surface axis (`connect_audience` / `is_integration`)
and the user-facing catalog filter."""
from app.schemas.data_source_registry import (
    get_entry,
    list_integration_entries,
    list_available_data_sources,
)


def test_gmail_is_a_self_serve_integration():
    e = get_entry("gmail")
    assert e.ui_form == "integration"
    assert e.effective_connect_audience == "user"
    assert e.is_integration is True


def test_data_source_connectors_are_admin_only():
    # Snowflake/Fabric/QVD-style: admin-first AND admin-only — never self-serve,
    # even though they may support per-user auth.
    for t in ("snowflake", "postgresql", "ms_fabric"):
        e = get_entry(t)
        assert e.ui_form == "data_source"
        assert e.effective_connect_audience == "admin"
        assert e.is_integration is False


def test_integration_catalog_excludes_admin_only_connectors():
    types = {x["type"] for x in list_integration_entries()}
    assert "gmail" in types
    assert "google_drive" in types
    # Admin data sources must never appear in the user Integrations catalog.
    assert "snowflake" not in types
    assert "postgresql" not in types
    assert "ms_fabric" not in types


def test_list_available_data_sources_exposes_surface_axis():
    rows = {x["type"]: x for x in list_available_data_sources()}
    assert rows["gmail"]["connect_audience"] == "user"
    assert rows["gmail"]["is_integration"] is True
    assert rows["snowflake"]["connect_audience"] == "admin"
    assert rows["snowflake"]["is_integration"] is False
