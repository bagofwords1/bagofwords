# Enterprise License Validation
# Licensed under the Business Source License 1.1
# See ENTERPRISE_LICENSE for details

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
    ],
}

# Data sources that require an enterprise license
ENTERPRISE_DATASOURCES = ["powerbi", "qvd", "sybase", "tableau"]

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


# Cached license state.
#
# Signature verification is an expensive cryptographic operation, so we cache the
# *verified JWT claims* once (or a sentinel LicenseInfo for community/invalid keys).
# The time-based expiry decision, however, is re-evaluated on every call so a license
# that expires while the process is running is reflected immediately — without needing
# a pod/container restart.
_cached_payload: Optional[dict] = None
_cached_unlicensed: Optional[LicenseInfo] = None
_cache_initialized: bool = False


def _get_license_key() -> Optional[str]:
    """Get license key from configuration"""
    from app.settings.config import settings

    license_config = getattr(settings.bow_config, 'license', None)
    if license_config and license_config.key:
        key = license_config.key
        # Handle unresolved env var placeholder
        if key.startswith("${") and key.endswith("}"):
            return None
        return key
    return None


def _verify_license_key(key: str) -> Optional[dict]:
    """
    Verify a license key's signature and issuer.

    Returns the verified JWT claims (without applying the expiry check), or None if the
    key is missing, malformed, signed by the wrong key, or issued by the wrong issuer.
    The time-based expiry decision is deliberately left to the caller so it can be
    re-evaluated on every access rather than frozen at verification time.
    """
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
            return None

        return payload

    except jwt.InvalidTokenError as e:
        logger.warning(f"Invalid license key: {e}")
        return None
    except Exception as e:
        logger.error(f"Error validating license: {e}")
        return None


def _license_info_from_payload(payload: dict) -> LicenseInfo:
    """
    Build LicenseInfo from verified JWT claims, applying a *current-time* expiry check.

    This is called on every get_license_info() access so an expired license is reflected
    immediately, even if it was still valid when its signature was first verified.
    """
    # Check expiration (so we can preserve org_name for expired licenses)
    exp = payload.get("exp")
    expires_at = None
    if exp:
        expires_at = datetime.fromtimestamp(exp, tz=timezone.utc)
        if expires_at < datetime.now(timezone.utc):
            return LicenseInfo(
                licensed=False,
                tier="expired",
                org_name=payload.get("org_name"),
                expires_at=expires_at,
                license_id=payload.get("sub")
            )

    # Valid (not yet expired) license
    return LicenseInfo(
        licensed=True,
        tier=payload.get("tier", "enterprise"),
        org_name=payload.get("org_name"),
        expires_at=expires_at,
        features=payload.get("features", []),
        license_id=payload.get("sub")
    )


def get_license_info(force_refresh: bool = False) -> LicenseInfo:
    """
    Get current license information.

    The license key's signature is verified once and the resulting claims are cached,
    but the expiry check is re-evaluated on every call. This means a license that
    expires while the process is running takes effect immediately, rather than only
    after a pod/container restart.
    """
    global _cached_payload, _cached_unlicensed, _cache_initialized

    if not _cache_initialized or force_refresh:
        key = _get_license_key()
        if not key:
            _cached_payload = None
            _cached_unlicensed = LicenseInfo(licensed=False, tier="community")
        else:
            payload = _verify_license_key(key)
            if payload is None:
                _cached_payload = None
                _cached_unlicensed = LicenseInfo(licensed=False, tier="community")
            else:
                _cached_payload = payload
                _cached_unlicensed = None

        _cache_initialized = True

        # Log the resolved status once, at (re)initialization, to avoid per-call noise.
        _initial = _license_info_from_payload(_cached_payload) if _cached_payload else _cached_unlicensed
        if _initial.licensed:
            logger.info(f"Enterprise license active: {_initial.org_name}, tier: {_initial.tier}")
        else:
            logger.info(f"Running in community mode (tier: {_initial.tier})")

    # Re-derive the time-sensitive status on every call from the cached claims.
    if _cached_payload is not None:
        return _license_info_from_payload(_cached_payload)
    return _cached_unlicensed


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
                    detail="This feature requires an enterprise license. Set BOW_LICENSE_KEY to enable."
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
    """Clear the license cache (useful for testing or config reload)"""
    global _cached_payload, _cached_unlicensed, _cache_initialized
    _cached_payload = None
    _cached_unlicensed = None
    _cache_initialized = False
