"""
Tests for Enterprise License functionality.
"""
import pytest
import jwt
import os
from datetime import datetime, timezone, timedelta
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.backends import default_backend


# Generate a test RSA key pair for testing
def _generate_test_keys():
    """Generate a test RSA key pair for license testing."""
    private_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
        backend=default_backend()
    )
    public_key = private_key.public_key()

    # Serialize keys
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption()
    ).decode('utf-8')

    public_pem = public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode('utf-8')

    return private_pem, public_pem


# Test keys (generated once for consistency)
TEST_PRIVATE_KEY, TEST_PUBLIC_KEY = _generate_test_keys()


def _create_test_license(
    org_name: str = "Test Corp",
    tier: str = "enterprise",
    features: list = None,
    expires_in_days: int = 365,
    expired: bool = False,
    private_key: str = TEST_PRIVATE_KEY,
):
    """Create a test license JWT."""
    now = datetime.now(timezone.utc)

    if expired:
        exp = now - timedelta(days=1)
    else:
        exp = now + timedelta(days=expires_in_days)

    payload = {
        "iss": "bagofwords.com",
        "sub": f"lic_test_{org_name.lower().replace(' ', '_')}",
        "iat": int(now.timestamp()),
        "exp": int(exp.timestamp()),
        "tier": tier,
        "org_name": org_name,
        "features": features or [],
    }

    token = jwt.encode(payload, private_key, algorithm="RS256")
    return f"bow_lic_{token}"


@pytest.fixture
def license_env_cleanup():
    """Cleanup license environment and cache after test."""
    from app.ee.license import clear_license_cache

    # Store original value
    original = os.environ.get("BOW_LICENSE_KEY")

    yield

    # Restore original value
    if original:
        os.environ["BOW_LICENSE_KEY"] = original
    elif "BOW_LICENSE_KEY" in os.environ:
        del os.environ["BOW_LICENSE_KEY"]

    # Clear cache
    clear_license_cache()


@pytest.fixture
def patch_license_key(license_env_cleanup):
    """Fixture to patch license public key for testing."""
    import app.ee.license as license_module

    # Save original key
    original_key = license_module.LICENSE_PUBLIC_KEY

    # Patch with test key
    license_module.LICENSE_PUBLIC_KEY = TEST_PUBLIC_KEY

    yield

    # Restore original key
    license_module.LICENSE_PUBLIC_KEY = original_key


@pytest.mark.e2e
class TestLicenseValidation:
    """Unit tests for license validation logic."""

    def test_community_mode_no_license(self, test_client, license_env_cleanup):
        """Test that no license key returns community mode."""
        from app.ee.license import get_license_info, clear_license_cache

        # Ensure no license key
        if "BOW_LICENSE_KEY" in os.environ:
            del os.environ["BOW_LICENSE_KEY"]
        clear_license_cache()

        # Also clear bow_config license
        from app.settings.config import settings
        if hasattr(settings.bow_config, 'license') and settings.bow_config.license:
            settings.bow_config.license.key = None

        clear_license_cache()

        info = get_license_info(force_refresh=True)
        assert info.licensed is False
        assert info.tier == "community"
        assert info.org_name is None
        assert info.features == []

    def test_valid_license(self, test_client, patch_license_key):
        """Test valid license validation."""
        from app.ee.license import get_license_info, clear_license_cache
        from app.settings.config import settings

        # Set test license
        test_license = _create_test_license(
            org_name="Acme Corp",
            tier="enterprise",
            features=["audit_logs", "sso"],
        )

        # Set in bow_config
        if not hasattr(settings.bow_config, 'license') or not settings.bow_config.license:
            from app.settings.bow_config import LicenseConfig
            settings.bow_config.license = LicenseConfig(key=test_license)
        else:
            settings.bow_config.license.key = test_license

        clear_license_cache()
        info = get_license_info(force_refresh=True)

        assert info.licensed is True
        assert info.tier == "enterprise"
        assert info.org_name == "Acme Corp"
        assert "audit_logs" in info.features
        assert "sso" in info.features

    def test_expired_license(self, test_client, patch_license_key):
        """Test expired license returns not licensed."""
        from app.ee.license import get_license_info, clear_license_cache
        from app.settings.config import settings

        # Set expired test license
        test_license = _create_test_license(
            org_name="Expired Corp",
            expired=True,
        )

        if not hasattr(settings.bow_config, 'license') or not settings.bow_config.license:
            from app.settings.bow_config import LicenseConfig
            settings.bow_config.license = LicenseConfig(key=test_license)
        else:
            settings.bow_config.license.key = test_license

        clear_license_cache()
        info = get_license_info(force_refresh=True)

        assert info.licensed is False
        assert info.tier == "expired"
        assert info.org_name == "Expired Corp"

    def test_invalid_license_signature(self, test_client, license_env_cleanup):
        """Test invalid license signature returns community mode."""
        from app.ee.license import get_license_info, clear_license_cache
        from app.settings.config import settings

        # Create license with different key (won't validate against public key)
        different_private, _ = _generate_test_keys()
        test_license = _create_test_license(
            org_name="Invalid Corp",
            private_key=different_private,
        )

        if not hasattr(settings.bow_config, 'license') or not settings.bow_config.license:
            from app.settings.bow_config import LicenseConfig
            settings.bow_config.license = LicenseConfig(key=test_license)
        else:
            settings.bow_config.license.key = test_license

        clear_license_cache()
        info = get_license_info(force_refresh=True)

        # Invalid signature should result in community mode
        assert info.licensed is False
        assert info.tier == "community"

    def test_malformed_license(self, test_client, license_env_cleanup):
        """Test malformed license returns community mode."""
        from app.ee.license import get_license_info, clear_license_cache
        from app.settings.config import settings

        # Set malformed license
        if not hasattr(settings.bow_config, 'license') or not settings.bow_config.license:
            from app.settings.bow_config import LicenseConfig
            settings.bow_config.license = LicenseConfig(key="bow_lic_not_a_valid_jwt")
        else:
            settings.bow_config.license.key = "bow_lic_not_a_valid_jwt"

        clear_license_cache()
        info = get_license_info(force_refresh=True)

        assert info.licensed is False
        assert info.tier == "community"


@pytest.mark.e2e
class TestHasFeature:
    """Tests for has_feature function with tier-based features."""

    def test_has_feature_with_explicit_features(self, test_client, patch_license_key):
        """Test has_feature when license has explicit features list."""
        from app.ee.license import has_feature, clear_license_cache
        from app.settings.config import settings

        # License with explicit features
        test_license = _create_test_license(
            org_name="Feature Corp",
            tier="enterprise",
            features=["audit_logs"],  # Only audit_logs, not sso
        )

        if not hasattr(settings.bow_config, 'license') or not settings.bow_config.license:
            from app.settings.bow_config import LicenseConfig
            settings.bow_config.license = LicenseConfig(key=test_license)
        else:
            settings.bow_config.license.key = test_license

        clear_license_cache()

        assert has_feature("audit_logs") is True
        assert has_feature("sso") is False  # Not in explicit features

    def test_has_feature_uses_tier_defaults(self, test_client, patch_license_key):
        """Test has_feature uses tier defaults when no explicit features."""
        from app.ee.license import has_feature, clear_license_cache, TIER_FEATURES
        from app.settings.config import settings

        # License WITHOUT explicit features (empty list = use tier defaults)
        test_license = _create_test_license(
            org_name="Tier Corp",
            tier="enterprise",
            features=[],  # Empty = use tier defaults
        )

        if not hasattr(settings.bow_config, 'license') or not settings.bow_config.license:
            from app.settings.bow_config import LicenseConfig
            settings.bow_config.license = LicenseConfig(key=test_license)
        else:
            settings.bow_config.license.key = test_license

        clear_license_cache()

        # Should have features from TIER_FEATURES["enterprise"]
        for feature in TIER_FEATURES.get("enterprise", []):
            assert has_feature(feature) is True

    def test_has_feature_community_returns_false(self, test_client, license_env_cleanup):
        """Test has_feature returns False for community mode."""
        from app.ee.license import has_feature, clear_license_cache
        from app.settings.config import settings

        # No license
        if hasattr(settings.bow_config, 'license') and settings.bow_config.license:
            settings.bow_config.license.key = None

        clear_license_cache()

        assert has_feature("audit_logs") is False
        assert has_feature("sso") is False


@pytest.mark.e2e
class TestLicenseAPIEndpoint:
    """Tests for the /api/license endpoint."""

    def test_license_endpoint_community(self, test_client, license_env_cleanup):
        """Test license endpoint returns community info when no license."""
        from app.ee.license import clear_license_cache
        from app.settings.config import settings

        # Ensure no license
        if hasattr(settings.bow_config, 'license') and settings.bow_config.license:
            settings.bow_config.license.key = None

        clear_license_cache()

        response = test_client.get("/api/license")
        assert response.status_code == 200

        data = response.json()
        assert data["licensed"] is False
        assert data["tier"] == "community"

    def test_license_endpoint_enterprise(self, test_client, patch_license_key):
        """Test license endpoint returns enterprise info with valid license."""
        from app.ee.license import clear_license_cache
        from app.settings.config import settings

        test_license = _create_test_license(
            org_name="API Test Corp",
            tier="enterprise",
        )

        if not hasattr(settings.bow_config, 'license') or not settings.bow_config.license:
            from app.settings.bow_config import LicenseConfig
            settings.bow_config.license = LicenseConfig(key=test_license)
        else:
            settings.bow_config.license.key = test_license

        clear_license_cache()

        response = test_client.get("/api/license")
        assert response.status_code == 200

        data = response.json()
        assert data["licensed"] is True
        assert data["tier"] == "enterprise"
        assert data["org_name"] == "API Test Corp"


@pytest.mark.e2e
class TestAuditLogsGating:
    """Tests for audit logs enterprise feature gating."""

    def test_audit_logs_requires_license(
        self,
        test_client,
        create_user,
        login_user,
        whoami,
        license_env_cleanup,
    ):
        """Test audit logs endpoint requires enterprise license."""
        from app.ee.license import clear_license_cache
        from app.settings.config import settings

        # Create user and login
        user = create_user()
        token = login_user(user["email"], user["password"])
        org_id = whoami(token)['organizations'][0]['id']

        # Ensure no license
        if hasattr(settings.bow_config, 'license') and settings.bow_config.license:
            settings.bow_config.license.key = None

        clear_license_cache()

        # Try to access audit logs
        response = test_client.get(
            "/api/enterprise/audit",
            headers={
                "Authorization": f"Bearer {token}",
                "X-Organization-Id": org_id,
            }
        )

        # Should be denied (402 Payment Required)
        assert response.status_code == 402
        assert "enterprise license" in response.json()["detail"].lower()

    def test_audit_logs_accessible_with_license(
        self,
        test_client,
        create_user,
        login_user,
        whoami,
        patch_license_key,
    ):
        """Test audit logs endpoint accessible with enterprise license."""
        from app.ee.license import clear_license_cache
        from app.settings.config import settings

        # Create user and login
        user = create_user()
        token = login_user(user["email"], user["password"])
        org_id = whoami(token)['organizations'][0]['id']

        # Set valid license with audit_logs feature
        test_license = _create_test_license(
            org_name="Audit Test Corp",
            tier="enterprise",
            features=["audit_logs"],
        )

        if not hasattr(settings.bow_config, 'license') or not settings.bow_config.license:
            from app.settings.bow_config import LicenseConfig
            settings.bow_config.license = LicenseConfig(key=test_license)
        else:
            settings.bow_config.license.key = test_license

        clear_license_cache()

        # Try to access audit logs
        response = test_client.get(
            "/api/enterprise/audit",
            headers={
                "Authorization": f"Bearer {token}",
                "X-Organization-Id": org_id,
            }
        )

        # Should be accessible
        assert response.status_code == 200
        data = response.json()
        assert "items" in data
        assert "total" in data


@pytest.mark.e2e
class TestDataSourceLicensing:
    """Tests for enterprise data source licensing."""

    def test_community_datasource_always_allowed(self, test_client, license_env_cleanup):
        """Community data sources (postgres, mysql, etc.) always allowed."""
        from app.ee.license import is_datasource_allowed, clear_license_cache
        from app.settings.config import settings

        # Ensure no license
        if "BOW_LICENSE_KEY" in os.environ:
            del os.environ["BOW_LICENSE_KEY"]
        if hasattr(settings.bow_config, 'license') and settings.bow_config.license:
            settings.bow_config.license.key = None
        clear_license_cache()

        assert is_datasource_allowed("postgresql") is True
        assert is_datasource_allowed("mysql") is True
        assert is_datasource_allowed("sqlite") is True

    def test_enterprise_datasource_blocked_without_license(self, test_client, license_env_cleanup):
        """Enterprise data sources blocked without license."""
        from app.ee.license import is_datasource_allowed, clear_license_cache
        from app.settings.config import settings

        # Ensure no license
        if "BOW_LICENSE_KEY" in os.environ:
            del os.environ["BOW_LICENSE_KEY"]
        if hasattr(settings.bow_config, 'license') and settings.bow_config.license:
            settings.bow_config.license.key = None
        clear_license_cache()

        assert is_datasource_allowed("powerbi") is False
        assert is_datasource_allowed("qvd") is False

    def test_enterprise_datasource_allowed_with_license(self, test_client, patch_license_key):
        """Enterprise data sources allowed with valid license."""
        from app.ee.license import is_datasource_allowed, clear_license_cache
        from app.settings.config import settings
        from app.settings.bow_config import LicenseConfig

        test_license = _create_test_license(
            org_name="Enterprise Corp",
            tier="enterprise",
        )

        if not hasattr(settings.bow_config, 'license') or not settings.bow_config.license:
            settings.bow_config.license = LicenseConfig(key=test_license)
        else:
            settings.bow_config.license.key = test_license

        clear_license_cache()

        assert is_datasource_allowed("powerbi") is True
        assert is_datasource_allowed("qvd") is True

    def test_enterprise_datasource_with_explicit_features(self, test_client, patch_license_key):
        """License with explicit ds_ features restricts to those only."""
        from app.ee.license import is_datasource_allowed, clear_license_cache
        from app.settings.config import settings
        from app.settings.bow_config import LicenseConfig

        # License with only ds_powerbi feature
        test_license = _create_test_license(
            org_name="Restricted Corp",
            tier="enterprise",
            features=["ds_powerbi"],  # Only PowerBI allowed
        )

        if not hasattr(settings.bow_config, 'license') or not settings.bow_config.license:
            settings.bow_config.license = LicenseConfig(key=test_license)
        else:
            settings.bow_config.license.key = test_license

        clear_license_cache()

        assert is_datasource_allowed("powerbi") is True
        assert is_datasource_allowed("qvd") is False  # Not in features


@pytest.mark.e2e
class TestUserAuthPolicyLicensing:
    """Tests for user_required auth policy enterprise licensing."""

    def test_user_required_auth_blocked_without_license(
        self,
        test_client,
        create_user,
        login_user,
        whoami,
        license_env_cleanup,
    ):
        """Creating connection with auth_policy=user_required blocked without license."""
        from app.ee.license import clear_license_cache
        from app.settings.config import settings

        # Create user and login
        user = create_user()
        token = login_user(user["email"], user["password"])
        org_id = whoami(token)['organizations'][0]['id']

        # Ensure no license
        if hasattr(settings.bow_config, 'license') and settings.bow_config.license:
            settings.bow_config.license.key = None
        clear_license_cache()

        # Try to create connection with user_required auth policy
        response = test_client.post(
            "/api/connections",
            json={
                "name": "Test Connection",
                "type": "postgresql",
                "config": {"host": "localhost", "port": 5432, "database": "test"},
                "credentials": {"username": "test", "password": "test"},
                "auth_policy": "user_required",
            },
            headers={
                "Authorization": f"Bearer {token}",
                "X-Organization-Id": org_id,
            }
        )

        # Should be denied (402 Payment Required)
        assert response.status_code == 402
        assert "enterprise license" in response.json()["detail"].lower()

    def test_user_required_auth_allowed_with_license(
        self,
        test_client,
        create_user,
        login_user,
        whoami,
        patch_license_key,
    ):
        """Creating connection with auth_policy=user_required allowed with license."""
        from app.ee.license import clear_license_cache
        from app.settings.config import settings
        from app.settings.bow_config import LicenseConfig

        # Create user and login
        user = create_user()
        token = login_user(user["email"], user["password"])
        org_id = whoami(token)['organizations'][0]['id']

        # Set valid enterprise license
        test_license = _create_test_license(
            org_name="User Auth Test Corp",
            tier="enterprise",
        )

        if not hasattr(settings.bow_config, 'license') or not settings.bow_config.license:
            settings.bow_config.license = LicenseConfig(key=test_license)
        else:
            settings.bow_config.license.key = test_license
        clear_license_cache()

        # Try to create connection with user_required auth policy
        # Note: This will pass the license check but may fail connection validation
        # (which is expected since we don't have a real database)
        response = test_client.post(
            "/api/connections",
            json={
                "name": "Test Connection",
                "type": "postgresql",
                "config": {"host": "localhost", "port": 5432, "database": "test"},
                "credentials": {"username": "test", "password": "test"},
                "auth_policy": "user_required",
            },
            headers={
                "Authorization": f"Bearer {token}",
                "X-Organization-Id": org_id,
            }
        )

        # Should NOT be 402 (license check passed)
        # May be 400 (connection validation failed) or 200 (success)
        assert response.status_code != 402

    def test_system_only_auth_allowed_without_license(
        self,
        test_client,
        create_user,
        login_user,
        whoami,
        license_env_cleanup,
    ):
        """Creating connection with auth_policy=system_only allowed without license."""
        from app.ee.license import clear_license_cache
        from app.settings.config import settings

        # Create user and login
        user = create_user()
        token = login_user(user["email"], user["password"])
        org_id = whoami(token)['organizations'][0]['id']

        # Ensure no license
        if hasattr(settings.bow_config, 'license') and settings.bow_config.license:
            settings.bow_config.license.key = None
        clear_license_cache()

        # Try to create connection with system_only auth policy (default)
        response = test_client.post(
            "/api/connections",
            json={
                "name": "Test Connection",
                "type": "postgresql",
                "config": {"host": "localhost", "port": 5432, "database": "test"},
                "credentials": {"username": "test", "password": "test"},
                "auth_policy": "system_only",
            },
            headers={
                "Authorization": f"Bearer {token}",
                "X-Organization-Id": org_id,
            }
        )

        # Should NOT be 402 (no license needed for system_only)
        # May be 400 (connection validation failed) or 200 (success)
        assert response.status_code != 402
