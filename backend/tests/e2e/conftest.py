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
import pathlib

import pytest

from app.ee import license as ee_license
from app.services import connection_indexing_service as _cis

_E2E_DIR = pathlib.Path(__file__).parent


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(config, items):
    """Everything under tests/e2e is an e2e test.

    CI selects this suite with ``-m e2e``, so a test that forgot the marker was
    deselected without a trace — 135 tests (12 whole files) had never run in CI,
    and one of those files only passed on sqlite. Marking here, before the
    ``-m`` filter runs, means a missing decorator can no longer hide a test.
    """
    for item in items:
        if _E2E_DIR in pathlib.Path(str(item.path)).parents:
            item.add_marker(pytest.mark.e2e)


@pytest.fixture(scope="session", autouse=True)
def _e2e_force_enterprise_license():
    """Activate a fake enterprise license for the whole e2e session."""
    fake = ee_license.LicenseInfo(
        licensed=True,
        tier="enterprise",
        org_name="e2e-tests",
        features=[
            "audit_logs",
            "audit_log_streams",
            "step_retention_config",
            "scim",
            "custom_roles",
            "ldap",
            "usage_limits",
            "pii_protection",
            "rls",
            "multi_org",
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
def _e2e_reapply_enterprise_license(_e2e_force_enterprise_license):
    """Re-apply the session license before each test.

    License tests call ``clear_license_cache()`` on teardown, which drops the
    session fake for every later test on the same worker, so enterprise-gated
    connectors would 402 depending on test order. Tests that need community
    mode still clear or patch the cache inside their own fixtures/body.
    """
    ee_license._cached_license = _e2e_force_enterprise_license
    ee_license._cache_initialized = True
    ee_license._db_license_key = None
    yield


@pytest.fixture
def enforce_org_creation_policy():
    """Request this to run with the real rule for creating an additional
    organization (instance administrator + multi-org flag + license)."""
    yield


@pytest.fixture(autouse=True)
def _e2e_any_user_may_create_additional_organizations(request, monkeypatch):
    """Let any signed-in user create a second organization when multi-org is on.

    Two-organization fixtures across this suite build the second organization
    the way a multi-org deployment used to: its admin signs up and creates it.
    That is now reserved for instance administrators, so those fixtures would
    all fail at setup. Until they are moved to "an instance administrator
    creates the organization and invites its admin", the superadmin and license
    conditions are waived here; the multi-org flag is still required. Tests of
    the rule itself request ``enforce_org_creation_policy`` and get the real one.
    """
    if "enforce_org_creation_policy" in request.fixturenames:
        yield
        return
    from app.core import instance_admin
    from app.settings.config import settings

    real = instance_admin.additional_organization_refusal

    def _flag_only(user):
        if not settings.bow_config.features.allow_multiple_organizations:
            return real(user)
        return None

    monkeypatch.setattr(instance_admin, "additional_organization_refusal", _flag_only)
    yield


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


@pytest.fixture(scope="session", autouse=True)
def _e2e_org_scope_guard():
    """Run the organization-scope query guard in warn mode for the e2e suite.

    It is off by default in production config; here it only logs (once per
    distinct statement and call site) and records findings, it never fails a
    test. ``BOW_ORG_SCOPE_GUARD=off|warn|enforce`` overrides the mode;
    ``BOW_ORG_SCOPE_GUARD_REPORT=<path>`` writes the session's findings there
    as markdown (one file per xdist worker).
    """
    import os

    from app.core import org_scope_guard

    previous = org_scope_guard.get_mode()
    mode = (os.environ.get("BOW_ORG_SCOPE_GUARD") or "warn").strip().lower()
    org_scope_guard.install(mode if mode in org_scope_guard.MODES else "warn")
    yield
    report_path = os.environ.get("BOW_ORG_SCOPE_GUARD_REPORT")
    if report_path and org_scope_guard.get_mode() != "off":
        worker = os.environ.get("PYTEST_XDIST_WORKER")
        if worker:
            report_path = f"{report_path}.{worker}"
        with open(report_path, "w") as handle:
            handle.write(org_scope_guard.findings_markdown())
    org_scope_guard.install(previous)
