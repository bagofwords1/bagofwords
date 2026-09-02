# Enterprise License Validation
# Licensed under the Bag of Words Enterprise License
# See backend/app/ee/LICENSE for details

import jwt
import logging
import os
from datetime import datetime, timezone
from functools import wraps
from inspect import signature
from typing import Optional, List
from pydantic import BaseModel
from fastapi import HTTPException

logger = logging.getLogger(__name__)

# Features included in each tier
# When adding new enterprise features, add them here - no license regeneration needed
TIER_FEATURES = {
    "team": [
        "audit_logs",
    ],
    "enterprise": [
        "audit_logs",
        "step_retention_config",
        "scim",
        "custom_roles",
        "ldap",
        "domain_signup",
        "usage_limits",
        "scheduled_reindex",
        "cost_dashboard",
        "llm_access_control",
        "connection_rate_limit",
        "model_routing",
        "llm_fallback",
        "pii_protection",
        "rls",
        "data_encryption",
    ],
}

# Data sources that require an enterprise license
ENTERPRISE_DATASOURCES = ["powerbi", "qvd", "sybase", "tableau", "zabbix", "splunk"]

# Public key for license verification (RS256).
#
# This is an asymmetric *public* key — it only verifies license signatures and
# is safe to distribute (the private signing key never leaves Bag of Words).
# It is loaded from an adjacent .pem file rather than inlined so the key can be
# rotated without code changes and so static analysis does not mistake a public
# verification key for a hardcoded secret.
_LICENSE_PUBLIC_KEY_PATH = os.path.join(os.path.dirname(__file__), "license_public_key.pem")
with open(_LICENSE_PUBLIC_KEY_PATH, "r", encoding="utf-8") as _key_file:
    LICENSE_PUBLIC_KEY = _key_file.read()


class LicenseInfo(BaseModel):
    """Information about the current license"""
    licensed: bool = False
    tier: str = "community"
    org_name: Optional[str] = None
    expires_at: Optional[datetime] = None
    features: List[str] = []
    license_id: Optional[str] = None
    # Per-organization quotas. -1 means "no limit" (the default when the license
    # omits them or the instance is unlicensed/expired). A value >= 0 is a hard cap
    # enforced on member invites and data source ("agent") creation.
    max_users: int = -1
    max_agents: int = -1


# Cached license info (the license key's signature is verified and decoded once).
#
# The expensive part — verifying the RS256 signature and decoding the JWT — runs only
# at first access (or on force_refresh). The *expiry* decision, however, is re-evaluated
# on every get_license_info() call (see _apply_live_expiry), so a license that lapses
# while the process is running is reflected immediately, without a pod/container restart.
_cached_license: Optional[LicenseInfo] = None
_cache_initialized: bool = False

# License key stored in the database (Settings → License) for this instance.
#
# get_license_info()/has_feature() are *synchronous* and are called from sync code
# paths, so they can never await a database read. Instead the DB value is mirrored
# into this module-level variable from async code — at startup, on save/remove, and
# by the periodic refresher (app.services.license_service) — and the sync path just
# reads the mirror. None means "nothing stored"; the config/env key is then used.
_db_license_key: Optional[str] = None


def _get_config_license_key() -> Optional[str]:
    """The license key from bow-config / the BOW_LICENSE_KEY env var, if any."""
    from app.settings.config import settings

    license_config = getattr(settings.bow_config, 'license', None)
    if license_config and license_config.key:
        key = license_config.key
        # Handle unresolved env var placeholder
        if key.startswith("${") and key.endswith("}"):
            return None
        return key
    return None


def _get_license_key() -> Optional[str]:
    """The license key in effect, database first.

    A key saved in the UI deliberately overrides the deployment's
    BOW_LICENSE_KEY. Deployments commonly ship a demo/evaluation key in
    docker-compose or the Helm chart; an admin pasting a purchased key must be
    able to upgrade off it without a redeploy. Removing the stored key (DELETE
    /api/license/key) falls back to the config value again.
    """
    if _db_license_key:
        return _db_license_key
    return _get_config_license_key()


def get_key_source() -> str:
    """Where the active license key comes from: 'database', 'config', or 'none'."""
    if _db_license_key:
        return "database"
    if _get_config_license_key():
        return "config"
    return "none"


def has_config_license_key() -> bool:
    """True when bow-config / BOW_LICENSE_KEY supplies a key (active or overridden)."""
    return bool(_get_config_license_key())


def mask_license_key(key: Optional[str]) -> Optional[str]:
    """Render a key for display without disclosing it.

    The key is a bearer entitlement — anyone holding it can license another
    instance — so it is never returned in full over the API.
    """
    if not key:
        return None
    tail = key[-6:] if len(key) > 6 else key
    return f"…{tail}"


def set_db_license_key(key: Optional[str]) -> None:
    """Install (or drop) the database-stored key and invalidate the decoded cache.

    Called only from async code that has just read or written the DB row.
    """
    global _db_license_key, _cached_license, _cache_initialized
    _db_license_key = key or None
    _cached_license = None
    _cache_initialized = False


def get_db_license_key() -> Optional[str]:
    """The database-stored key currently mirrored into this process, if any."""
    return _db_license_key


def validate_license_key(key: str) -> LicenseInfo:
    """Verify a license key without installing it — used to reject bad keys on save."""
    return _validate_license_key(key)


def _coerce_limit(value) -> int:
    """Normalize a quota claim from the JWT into an int limit.

    Anything missing, non-numeric, or negative is treated as "no limit" (-1).
    A value of 0 is preserved (a deliberate cap of zero), so only >= 0 caps bite.
    """
    if value is None:
        return -1
    try:
        limit = int(value)
    except (TypeError, ValueError):
        return -1
    return limit if limit >= 0 else -1


def _validate_license_key(key: str) -> LicenseInfo:
    """Validate a license key and return license info"""
    try:
        # Remove bow_lic_ prefix if present
        if key.startswith("bow_lic_"):
            key = key[8:]

        # Decode and verify JWT (disable exp validation to check manually)
        payload = jwt.decode(
            key,
            LICENSE_PUBLIC_KEY,
            algorithms=["RS256"],
            options={
                "require": ["exp", "sub", "iss"],
                "verify_exp": False,  # We'll check manually to preserve org_name
            }
        )

        # Check issuer
        if payload.get("iss") != "bagofwords.com":
            logger.warning("Invalid license issuer")
            return LicenseInfo(licensed=False, tier="community")

        # Check expiration manually (so we can preserve org_name for expired licenses)
        exp = payload.get("exp")
        expires_at = None
        if exp:
            expires_at = datetime.fromtimestamp(exp, tz=timezone.utc)
            if expires_at < datetime.now(timezone.utc):
                logger.warning("License has expired")
                return LicenseInfo(
                    licensed=False,
                    tier="expired",
                    org_name=payload.get("org_name"),
                    expires_at=expires_at,
                    license_id=payload.get("sub")
                )

        # Valid license
        return LicenseInfo(
            licensed=True,
            tier=payload.get("tier", "enterprise"),
            org_name=payload.get("org_name"),
            expires_at=expires_at,
            features=payload.get("features", []),
            license_id=payload.get("sub"),
            # Quotas only apply to an active license. Missing/negative → unlimited.
            max_users=_coerce_limit(payload.get("max_users")),
            max_agents=_coerce_limit(payload.get("max_agents")),
        )

    except jwt.InvalidTokenError as e:
        logger.warning(f"Invalid license key: {e}")
        return LicenseInfo(licensed=False, tier="community")
    except Exception as e:
        logger.error(f"Error validating license: {e}")
        return LicenseInfo(licensed=False, tier="community")


def _apply_live_expiry(info: LicenseInfo) -> LicenseInfo:
    """
    Re-evaluate a cached LicenseInfo against the current time.

    Signature verification happens once and is cached, but a license can lapse while the
    process keeps running. If the cached info carries an expiry that is now in the past,
    downgrade it to the "expired" state on the fly — so enforcement no longer waits for a
    pod/container restart. Community/invalid licenses (no expiry) and already-expired ones
    pass through unchanged.
    """
    if (
        info.expires_at is not None
        and info.expires_at < datetime.now(timezone.utc)
        and (info.licensed or info.tier != "expired")
    ):
        return LicenseInfo(
            licensed=False,
            tier="expired",
            org_name=info.org_name,
            expires_at=info.expires_at,
            license_id=info.license_id,
        )
    return info


def get_license_info(force_refresh: bool = False) -> LicenseInfo:
    """
    Get current license information.

    The license key is verified and decoded once (cached), but the expiry check is
    re-applied on every call. A license that expires while the process is running takes
    effect immediately, rather than only after a pod/container restart.
    """
    global _cached_license, _cache_initialized

    if not _cache_initialized or force_refresh:
        key = _get_license_key()
        if not key:
            _cached_license = LicenseInfo(licensed=False, tier="community")
        else:
            _cached_license = _validate_license_key(key)

        _cache_initialized = True

        # Log the resolved status once, at (re)initialization, to avoid per-call noise.
        if _cached_license.licensed:
            logger.info(f"Enterprise license active: {_cached_license.org_name}, tier: {_cached_license.tier}")
        else:
            logger.info(f"Running in community mode (tier: {_cached_license.tier})")

    # Re-derive the time-sensitive status on every call from the cached info.
    return _apply_live_expiry(_cached_license)


def is_enterprise_licensed() -> bool:
    """Check if the instance has an active enterprise license"""
    return get_license_info().licensed


def has_feature(feature: str) -> bool:
    """
    Check if a specific enterprise feature is enabled.

    Logic:
    - If license has explicit features list → use that (custom deals)
    - Otherwise → use tier defaults from TIER_FEATURES

    This allows adding new features to tiers without regenerating licenses.
    """
    license_info = get_license_info()
    if not license_info.licensed:
        return False

    # If explicit features in license, use those (for custom/restricted licenses)
    if license_info.features:
        return feature in license_info.features

    # Otherwise, use tier defaults
    tier_features = TIER_FEATURES.get(license_info.tier, [])
    return feature in tier_features


def is_datasource_allowed(ds_type: str) -> bool:
    """
    Check if a data source type is allowed under current license.

    Logic:
    - Non-enterprise data sources → always allowed
    - Enterprise data sources → require enterprise license
    - If license has explicit ds_ features → check that list
    - Otherwise enterprise tier → all enterprise DS allowed
    """
    if ds_type not in ENTERPRISE_DATASOURCES:
        return True

    license_info = get_license_info()
    if not license_info.licensed:
        return False

    # If license has explicit ds_ features, check that (for custom/restricted licenses)
    if license_info.features and any(f.startswith("ds_") for f in license_info.features):
        return f"ds_{ds_type}" in license_info.features

    # Only enterprise tier gets access to enterprise data sources
    return license_info.tier == "enterprise"


def get_max_users() -> int:
    """Max members (active + pending invites) allowed per organization.

    Returns -1 (unlimited) unless an *active* license sets an explicit cap.
    """
    return get_license_info().max_users


def get_max_agents() -> int:
    """Max data sources ("agents") allowed per organization.

    Returns -1 (unlimited) unless an *active* license sets an explicit cap.
    """
    return get_license_info().max_agents


def require_enterprise(feature: Optional[str] = None):
    """
    Decorator that requires an active enterprise license.
    Optionally checks for a specific feature.

    Usage:
    @require_enterprise()  # Requires any enterprise license
    @require_enterprise(feature="audit_logs")  # Requires audit_logs feature
    """
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            license_info = get_license_info()

            if not license_info.licensed:
                if license_info.tier == "expired":
                    raise HTTPException(
                        status_code=402,
                        detail="Your enterprise license has expired. Please renew to access this feature."
                    )
                raise HTTPException(
                    status_code=402,
                    detail="This feature requires an enterprise license. Add a license key in Settings → License (or set BOW_LICENSE_KEY)."
                )

            if feature and not has_feature(feature):
                raise HTTPException(
                    status_code=402,
                    detail=f"This feature ({feature}) is not included in your license."
                )

            return await func(*args, **kwargs)
        return wrapper
    return decorator


def clear_license_cache():
    """Full reset of license state (useful for testing or config reload).

    Drops the mirrored database key as well as the decoded cache, so a test that
    activates a key through the API cannot leak an enterprise license into every
    later test in the same process. Use ``set_db_license_key`` when you want to
    invalidate only the decoded cache.
    """
    global _cached_license, _cache_initialized, _db_license_key
    _cached_license = None
    _cache_initialized = False
    _db_license_key = None
