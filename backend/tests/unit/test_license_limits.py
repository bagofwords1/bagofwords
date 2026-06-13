"""Unit tests for license quota parsing (max_users / max_agents).

These cover the pure helpers without touching the DB or the signing key:
 - _coerce_limit normalization rules
 - LicenseInfo defaults to unlimited (-1)
 - get_max_users / get_max_agents read the live cache
"""
import pytest

import app.ee.license as license_mod
from app.ee.license import LicenseInfo, _coerce_limit, get_max_users, get_max_agents


@pytest.mark.parametrize("value,expected", [
    (None, -1),       # missing → unlimited
    (-1, -1),         # explicit unlimited
    (-5, -1),         # any negative → unlimited
    (0, 0),           # a deliberate cap of zero is preserved
    (10, 10),         # normal cap
    ("25", 25),       # numeric string coerced
    ("nope", -1),     # non-numeric → unlimited
    (3.0, 3),         # float coerced to int
])
def test_coerce_limit(value, expected):
    assert _coerce_limit(value) == expected


def test_license_info_defaults_to_unlimited():
    info = LicenseInfo()
    assert info.max_users == -1
    assert info.max_agents == -1


@pytest.fixture
def restore_license_cache():
    prev_cache = license_mod._cached_license
    prev_init = license_mod._cache_initialized
    yield
    license_mod._cached_license = prev_cache
    license_mod._cache_initialized = prev_init


def test_getters_read_active_license(restore_license_cache):
    license_mod._cached_license = LicenseInfo(
        licensed=True, tier="enterprise", max_users=7, max_agents=3
    )
    license_mod._cache_initialized = True
    assert get_max_users() == 7
    assert get_max_agents() == 3


def test_getters_unlimited_when_unlicensed(restore_license_cache):
    license_mod._cached_license = LicenseInfo(licensed=False, tier="community")
    license_mod._cache_initialized = True
    assert get_max_users() == -1
    assert get_max_agents() == -1
