"""Sandbox launcher: same fake enterprise license as tests/e2e/conftest.py
(pii_protection is license-gated), then uvicorn on :8010 (override with
BOW_SANDBOX_PORT)."""
import os
import sys

import uvicorn

sys.path.insert(0, ".")
from app.ee import license as ee_license  # noqa: E402

ee_license._cached_license = ee_license.LicenseInfo(
    licensed=True, tier="enterprise", org_name="sandbox",
    features=["pii_protection", "rls", "audit_logs"], license_id="sandbox-fake",
)
ee_license._cache_initialized = True

import main  # noqa: E402

uvicorn.run(main.app, host="127.0.0.1", port=int(os.environ.get("BOW_SANDBOX_PORT", "8010")))
