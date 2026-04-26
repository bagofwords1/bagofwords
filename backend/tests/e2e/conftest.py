"""
e2e-suite conftest.

Forces an active enterprise license for the entire e2e session so that
RBAC tests covering custom roles, groups, LDAP, audit logs etc. exercise
the real enterprise codepaths instead of skipping. Without this, every
test that calls ``_requires_enterprise()`` (or hits a route guarded by
``@require_enterprise``) skips because the dev environment has no
license key configured.

The license layer caches its result in module-level globals
``_cached_license`` / ``_cache_initialized`` (see app/ee/license.py).
Setting them once at session start makes ``get_license_info()``,
``has_feature()`` and ``is_enterprise_licensed()`` all report enterprise
without us having to monkey-patch each entry point individually.

Tests that intentionally exercise the community-mode branch (e.g.
test_rbac.py::test_create_custom_role_enterprise_gate) still work — the
``if is_enterprise_licensed()`` branches just always pick the licensed
arm, which is the more thorough one.
"""
import pytest

from app.ee import license as ee_license
from app.services import connection_indexing_service as _cis


@pytest.fixture(scope="session", autouse=True)
def _e2e_force_enterprise_license():
    """Activate a fake enterprise license for the whole e2e session."""
    fake = ee_license.LicenseInfo(
        licensed=True,
        tier="enterprise",
        org_name="e2e-tests",
        features=[
            "audit_logs",
            "step_retention_config",
            "scim",
            "custom_roles",
            "ldap",
        ],
        license_id="e2e-fake",
    )
    saved_cached = ee_license._cached_license
    saved_initialized = ee_license._cache_initialized
    ee_license._cached_license = fake
    ee_license._cache_initialized = True
    try:
        yield fake
    finally:
        ee_license._cached_license = saved_cached
        ee_license._cache_initialized = saved_initialized


@pytest.fixture(autouse=True)
def _drain_connection_indexing_loop():
    """Drain the connection-indexing background loop after each test.

    PR #225 introduced a process-wide daemon-thread event loop that runs
    fire-and-forget indexing jobs. Without an explicit drain, a job can be
    mid-flight when the test ends, leaving an `idle in transaction` Postgres
    session that blocks the next test's `DROP SCHEMA` for the connection's
    full lifetime — the source of the 6h CI timeouts.
    """
    yield
    _cis.shutdown_background_loop()
