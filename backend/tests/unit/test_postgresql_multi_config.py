"""The postgresql_multi connection type is registered and its form schema rejects configs that cannot work."""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.data_sources.clients.postgresql_multi_client import PostgresqlMultiClient
from app.schemas.data_source_registry import get_entry, resolve_client_class
from app.schemas.data_sources.configs import PostgreSQLMultiConfig

BASE = {"host": "db.example.com", "databases": "tenant_*, !tenant_test"}


def test_registry_resolves_the_type_to_the_multi_client():
    assert resolve_client_class("postgresql_multi") is PostgresqlMultiClient


def test_registry_entry_is_visible_and_system_scoped():
    entry = get_entry("postgresql_multi")
    assert not entry.dev_only
    assert entry.config_schema is PostgreSQLMultiConfig
    assert {name: variant.scopes for name, variant in entry.credentials_auth.by_auth.items()} == {"userpass": ["system"]}


def test_validated_config_and_login_construct_the_client():
    entry = get_entry("postgresql_multi")
    config = PostgreSQLMultiConfig(**BASE, schema="public", table_exclude_pattern="^tmp_").model_dump()
    login = entry.credentials_auth.by_auth["userpass"].schema(user="reader", password="pw").model_dump()
    client = resolve_client_class("postgresql_multi")(**config, **login)
    assert client.host == "db.example.com"
    assert client.port == 5432
    assert client.user == "reader"
    assert client.discovery_database == "postgres"


def test_defaults_match_the_single_database_form():
    config = PostgreSQLMultiConfig(**BASE)
    assert (config.port, config.sslmode, config.schema, config.discovery_database, config.table_exclude_pattern) == (
        5432, "prefer", None, "postgres", None)


@pytest.mark.parametrize("databases", ["", "  ", "a,,b", "!only_excluded"])
def test_databases_spec_that_selects_nothing_is_rejected(databases):
    with pytest.raises(ValidationError):
        PostgreSQLMultiConfig(host="h", databases=databases)


def test_databases_is_required():
    with pytest.raises(ValidationError):
        PostgreSQLMultiConfig(host="h")


@pytest.mark.parametrize("pattern", ["(unclosed", "(a+)+$"])
def test_unusable_or_costly_exclude_pattern_is_rejected(pattern):
    with pytest.raises(ValidationError):
        PostgreSQLMultiConfig(**BASE, table_exclude_pattern=pattern)


def test_valid_exclude_pattern_is_accepted():
    assert PostgreSQLMultiConfig(**BASE, table_exclude_pattern=r"^day_\d{2}").table_exclude_pattern == r"^day_\d{2}"


LOCALES_DIR = Path(__file__).resolve().parents[3] / "locales"


def _ui_keys(model) -> dict[str, dict[str, str]]:
    return {
        name: {k: v for k, v in (field.json_schema_extra or {}).items() if k.endswith("Key")}
        for name, field in model.model_fields.items()
    }


def _lookup(catalog: dict, dotted: str):
    node = catalog
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def test_databases_field_has_a_translated_title():
    assert "ui:titleKey" in _ui_keys(PostgreSQLMultiConfig)["databases"]


@pytest.mark.parametrize("locale", sorted(p.stem for p in LOCALES_DIR.glob("*.json")))
def test_every_form_label_resolves_in_every_locale(locale):
    catalog = json.loads((LOCALES_DIR / f"{locale}.json").read_text(encoding="utf-8"))
    keys = [key for field in _ui_keys(PostgreSQLMultiConfig).values() for key in field.values()]
    assert keys
    missing = [key for key in keys if not isinstance(_lookup(catalog, key), str) or not _lookup(catalog, key).strip()]
    assert missing == []
