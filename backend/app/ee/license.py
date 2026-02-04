# Enterprise License Validation
# Licensed under the Business Source License 1.1
# See ENTERPRISE_LICENSE for details

import jwt
import logging
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
    ],
}

# Public key for license verification (RS256)
# This key is used to verify license signatures without exposing the private key
LICENSE_PUBLIC_KEY = """-----BEGIN PUBLIC KEY-----
MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAxmmTEMSxkicmKkQY1fmR
reBzCRZqxD6Pj2B4f33yo0zIOZ/uZy+hbN2MX1HN/NSErgxeBc2Cc4V0jVn3ElZY
xqbJ0s+Yx86ycwczkn2Lv7PTxBDjcMKI9duBjrR6A2C38PI1ij1LwRLbERtpCJ0I
8+B8/Act9FTVM/xs/L4ZFiT4gM9C04QBWB5pBsxXkrIp2kPEU2Djfx7U8KlwBsCC
/A4ARnvXMlXvakY7m3D377ZMFKQ+qjgn6ILGwpYPq2LtH2/V6/sHTzu0Q8Xv8PH4
VLPjN9mwZy0QlE26rUXElbaPSMmlhk3RPXCmmwHRrPFZXQmPV4HBJAleA7U9MswG
PwIDAQAB
-----END PUBLIC KEY-----"""


class LicenseInfo(BaseModel):
    """Information about the current license"""
    licensed: bool = False
    tier: str = "community"
    org_name: Optional[str] = None
    expires_at: Optional[datetime] = None
    features: List[str] = []
    license_id: Optional[str] = None


# Cached license info (validated once at startup/first access)
_cached_license: Optional[LicenseInfo] = None
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
            license_id=payload.get("sub")
        )

    except jwt.InvalidTokenError as e:
        logger.warning(f"Invalid license key: {e}")
        return LicenseInfo(licensed=False, tier="community")
    except Exception as e:
        logger.error(f"Error validating license: {e}")
        return LicenseInfo(licensed=False, tier="community")


def get_license_info(force_refresh: bool = False) -> LicenseInfo:
    """
    Get current license information.
    Results are cached after first validation.
    """
    global _cached_license, _cache_initialized

    if _cache_initialized and not force_refresh:
        return _cached_license

    key = _get_license_key()
    if not key:
        _cached_license = LicenseInfo(licensed=False, tier="community")
    else:
        _cached_license = _validate_license_key(key)

    _cache_initialized = True

    if _cached_license.licensed:
        logger.info(f"Enterprise license active: {_cached_license.org_name}, tier: {_cached_license.tier}")
    else:
        logger.info(f"Running in community mode (tier: {_cached_license.tier})")

    return _cached_license


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
    global _cached_license, _cache_initialized
    _cached_license = None
    _cache_initialized = False
