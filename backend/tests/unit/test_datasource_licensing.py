"""Unit tests for which connectors the enterprise license gates.

Two surfaces decide whether a connector is paid, and they must agree:

 - ``DataSourceRegistryEntry.requires_license`` — served to the connector
   picker, which draws a padlock on the card.
 - ``ENTERPRISE_DATASOURCES`` — enforced on create (402) by
   ``connection_service`` / ``data_source_service`` / ``agent_yaml_service``.

A type enforced here but unmarked in the registry is the bad direction: the
picker offers it as free and the API then refuses it.
"""
import pytest

import app.ee.license as license_mod
from app.ee.license import ENTERPRISE_DATASOURCES, LicenseInfo, is_datasource_allowed
from app.schemas.data_source_registry import REGISTRY


@pytest.fixture
def unlicensed():
    """Pin the license cache to community for the duration of the test."""
    prev_cache, prev_init = license_mod._cached_license, license_mod._cache_initialized
    license_mod._cached_license = LicenseInfo(licensed=False, tier="community")
    license_mod._cache_initialized = True
    yield
    license_mod._cached_license, license_mod._cache_initialized = prev_cache, prev_init


@pytest.fixture
def licensed():
    """Pin the license cache to an active enterprise license."""
    prev_cache, prev_init = license_mod._cached_license, license_mod._cache_initialized
    license_mod._cached_license = LicenseInfo(licensed=True, tier="enterprise")
    license_mod._cache_initialized = True
    yield
    license_mod._cached_license, license_mod._cache_initialized = prev_cache, prev_init


@pytest.mark.parametrize("ds_type", ENTERPRISE_DATASOURCES)
def test_enforced_type_is_also_padlocked_in_the_picker(ds_type):
    assert ds_type in REGISTRY, f"{ds_type} is enforced but not a registry type"
    assert REGISTRY[ds_type].requires_license == "enterprise"


@pytest.mark.parametrize("ds_type", ["splunk", "zabbix", "kubernetes"])
def test_community_connector_is_free_on_both_surfaces(ds_type, unlicensed):
    assert REGISTRY[ds_type].requires_license is None
    assert ds_type not in ENTERPRISE_DATASOURCES
    assert is_datasource_allowed(ds_type) is True


@pytest.mark.parametrize("ds_type", ["documentum", "sharepoint_onprem"])
def test_paid_connector_blocked_without_license(ds_type, unlicensed):
    assert REGISTRY[ds_type].requires_license == "enterprise"
    assert ds_type in ENTERPRISE_DATASOURCES
    assert is_datasource_allowed(ds_type) is False


@pytest.mark.parametrize("ds_type", ["documentum", "sharepoint_onprem"])
def test_paid_connector_allowed_with_license(ds_type, licensed):
    assert is_datasource_allowed(ds_type) is True
