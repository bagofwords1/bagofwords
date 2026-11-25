#!/usr/bin/env python3
"""Run the BOW backend with a *sandbox-only* enterprise license, so the
licensed sign-up paths (domain sign-up, groups, custom roles) can be driven
end to end on a throwaway database. Same fake the e2e suite installs
(tests/e2e/rbac/conftest.py); never for anything but a local sandbox.

    cd backend && TESTING=true TEST_DATABASE_URL=sqlite:///db/agent.db \\
        uv run python ../tools/agent/app_users/backend_enterprise.py
"""
import os
import sys

sys.path.insert(0, os.getcwd())

from app.ee import license as ee_license  # noqa: E402

FAKE = ee_license.LicenseInfo(
    licensed=True, tier="enterprise", org_name="app-users-sandbox", license_id="sandbox-fake",
    features=["audit_logs", "step_retention_config", "scim", "custom_roles", "ldap", "usage_limits",
              "domain_signup"],
)
ee_license.get_license_info = lambda force_refresh=False: FAKE
ee_license.is_enterprise_licensed = lambda: True
ee_license._cached_license = FAKE
ee_license._cache_initialized = True

import uvicorn  # noqa: E402

import main  # noqa: E402,F401  (registers routes with the patched license)

if __name__ == "__main__":
    uvicorn.run(main.app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
