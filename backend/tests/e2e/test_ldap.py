"""
Tests for LDAP directory sync and admin endpoints.

Uses mocked LDAP responses — does not require a real LDAP server.
"""
import pytest
import jwt
from unittest.mock import patch, MagicMock, AsyncMock
from datetime import datetime, timezone, timedelta
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.backends import default_backend


# ── Test key generation (same pattern as test_scim.py) ──

def _generate_test_keys():
    private_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
        backend=default_backend()
    )
    public_key = private_key.public_key()

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


TEST_PRIVATE_KEY, TEST_PUBLIC_KEY = _generate_test_keys()


def _create_test_license(tier="enterprise", features=None):
    now = datetime.now(timezone.utc)
    payload = {
        "iss": "bagofwords.com",
        "sub": "lic_test_ldap",
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(days=365)).timestamp()),
        "tier": tier,
        "org_name": "LDAP Test Corp",
        "features": features or [],
    }
    token = jwt.encode(payload, TEST_PRIVATE_KEY, algorithm="RS256")
    return f"bow_lic_{token}"


# ── Fixtures ──

@pytest.fixture
def license_env_cleanup():
    import os
    from app.ee.license import clear_license_cache

    original = os.environ.get("BOW_LICENSE_KEY")
    yield
    if original:
        os.environ["BOW_LICENSE_KEY"] = original
    elif "BOW_LICENSE_KEY" in os.environ:
        del os.environ["BOW_LICENSE_KEY"]
    clear_license_cache()


@pytest.fixture
def patch_license_key(license_env_cleanup):
    import app.ee.license as license_module

    original_key = license_module.LICENSE_PUBLIC_KEY
    license_module.LICENSE_PUBLIC_KEY = TEST_PUBLIC_KEY
    yield
    license_module.LICENSE_PUBLIC_KEY = original_key


@pytest.fixture
def enterprise_license(patch_license_key):
    from app.ee.license import clear_license_cache
    from app.settings.config import settings
    from app.settings.bow_config import LicenseConfig

    test_license = _create_test_license(tier="enterprise")

    if not hasattr(settings.bow_config, 'license') or not settings.bow_config.license:
        settings.bow_config.license = LicenseConfig(key=test_license)
    else:
        settings.bow_config.license.key = test_license

    clear_license_cache()
    yield


@pytest.fixture
def enable_ldap():
    """Enable LDAP in settings for the duration of the test."""
    from app.settings.config import settings

    original_enabled = settings.bow_config.ldap.enabled
    settings.bow_config.ldap.enabled = True
    settings.bow_config.ldap.url = "ldaps://mock-ldap.test:636"
    settings.bow_config.ldap.base_dn = "dc=test,dc=com"
    settings.bow_config.ldap.bind_dn = "cn=admin,dc=test,dc=com"
    settings.bow_config.ldap.bind_password = "admin_pass"
    yield
    settings.bow_config.ldap.enabled = original_enabled


@pytest.fixture
def ldap_setup(test_client, create_user, login_user, whoami, enterprise_license, enable_ldap):
    """Set up a user, org, and auth headers for LDAP admin endpoints."""
    user = create_user()
    token = login_user(user["email"], user["password"])
    user_info = whoami(token)
    org_id = user_info['organizations'][0]['id']

    return {
        "user_token": token,
        "org_id": org_id,
        "headers": {
            "Authorization": f"Bearer {token}",
            "X-Organization-Id": org_id,
        },
    }


# ── Mock data ──

MOCK_LDAP_USERS = [
    {"dn": "cn=alice,ou=Users,dc=test,dc=com", "email": "alice@test.com", "name": "Alice Smith"},
    {"dn": "cn=bob,ou=Users,dc=test,dc=com", "email": "bob@test.com", "name": "Bob Jones"},
    {"dn": "cn=charlie,ou=Users,dc=test,dc=com", "email": "charlie@test.com", "name": "Charlie Brown"},
]

MOCK_LDAP_GROUPS = [
    {
        "dn": "cn=Engineering,ou=Groups,dc=test,dc=com",
        "name": "Engineering",
        "members": [
            "cn=alice,ou=Users,dc=test,dc=com",
            "cn=bob,ou=Users,dc=test,dc=com",
        ],
    },
    {
        "dn": "cn=Marketing,ou=Groups,dc=test,dc=com",
        "name": "Marketing",
        "members": [
            "cn=charlie,ou=Users,dc=test,dc=com",
        ],
    },
]


def _mock_connection_manager():
    """Create a mock LDAPConnectionManager."""
    mock = MagicMock()
    mock.search_users.return_value = MOCK_LDAP_USERS
    mock.search_groups.return_value = MOCK_LDAP_GROUPS
    mock.test_connection.return_value = {
        "connected": True,
        "server": "ldaps://mock-ldap.test:636",
        "vendor": "MockLDAP",
    }
    return mock


# ============================================================================
# LDAP Admin Endpoint Tests
# ============================================================================


@pytest.mark.e2e
class TestLdapTestConnection:

    def test_test_connection_success(self, ldap_setup, test_client):
        """Test the test-connection endpoint with mocked LDAP."""
        with patch("app.ee.ldap.routes.LDAPConnectionManager") as MockCM:
            mock_cm = _mock_connection_manager()
            MockCM.return_value = mock_cm

            response = test_client.get(
                "/api/enterprise/ldap/test-connection",
                headers=ldap_setup["headers"],
            )
            assert response.status_code == 200, f"Expected 200, got {response.status_code}: {response.json()}"
            data = response.json()
            assert data["connected"] is True
            assert data["server"] == "ldaps://mock-ldap.test:636"

    def test_test_connection_failure(self, ldap_setup, test_client):
        """Test the test-connection endpoint when LDAP is unreachable."""
        with patch("app.ee.ldap.routes.LDAPConnectionManager") as MockCM:
            mock_cm = MagicMock()
            mock_cm.test_connection.return_value = {
                "connected": False,
                "server": "ldaps://mock-ldap.test:636",
                "error": "Connection refused",
            }
            MockCM.return_value = mock_cm

            response = test_client.get(
                "/api/enterprise/ldap/test-connection",
                headers=ldap_setup["headers"],
            )
            assert response.status_code == 200
            data = response.json()
            assert data["connected"] is False
            assert "error" in data


@pytest.mark.e2e
class TestLdapSyncStatus:

    def test_get_status_initial(self, ldap_setup, test_client):
        """Test getting sync status when no sync has run."""
        response = test_client.get(
            "/api/enterprise/ldap/sync/status",
            headers=ldap_setup["headers"],
        )
        assert response.status_code == 200
        data = response.json()
        assert data["ldap_configured"] is True
        assert data["last_sync"] is None


@pytest.mark.e2e
class TestLdapSync:

    def test_trigger_sync(self, ldap_setup, test_client):
        """Test triggering an LDAP group sync."""
        with patch("app.ee.ldap.routes.LDAPGroupSyncService") as MockService:
            from app.ee.ldap.schemas import SyncResult

            mock_service = MagicMock()
            mock_service.sync_groups = AsyncMock(return_value=SyncResult(
                groups_created=2,
                groups_updated=0,
                groups_removed=0,
                memberships_added=3,
                memberships_removed=0,
                users_not_found=0,
                errors=[],
                timestamp=datetime.now(timezone.utc),
            ))
            MockService.return_value = mock_service

            response = test_client.post(
                "/api/enterprise/ldap/sync",
                headers=ldap_setup["headers"],
            )
            assert response.status_code == 200

    def test_preview_sync(self, ldap_setup, test_client):
        """Test the sync preview (dry run)."""
        with patch("app.ee.ldap.routes.LDAPGroupSyncService") as MockService:
            from app.ee.ldap.schemas import LDAPSyncPreview, LDAPGroupPreview

            mock_service = MagicMock()
            mock_service.preview_sync = AsyncMock(return_value=LDAPSyncPreview(
                groups_to_create=2,
                groups_to_update=0,
                groups_to_remove=0,
                total_membership_changes=3,
                groups=[
                    LDAPGroupPreview(
                        dn="cn=Engineering,ou=Groups,dc=test,dc=com",
                        name="Engineering",
                        member_count=2,
                        exists_in_app=False,
                        members_to_add=2,
                        members_to_remove=0,
                    ),
                    LDAPGroupPreview(
                        dn="cn=Marketing,ou=Groups,dc=test,dc=com",
                        name="Marketing",
                        member_count=1,
                        exists_in_app=False,
                        members_to_add=1,
                        members_to_remove=0,
                    ),
                ],
            ))
            MockService.return_value = mock_service

            response = test_client.get(
                "/api/enterprise/ldap/sync/preview",
                headers=ldap_setup["headers"],
            )
            assert response.status_code == 200
            data = response.json()
            assert data["groups_to_create"] == 2
            assert len(data["groups"]) == 2


@pytest.mark.e2e
class TestLdapRequiresLicense:

    def test_sync_requires_license(
        self, test_client, create_user, login_user, whoami, license_env_cleanup, enable_ldap,
    ):
        """Test that LDAP endpoints require enterprise license."""
        from app.ee.license import clear_license_cache
        from app.settings.config import settings

        user = create_user()
        token = login_user(user["email"], user["password"])
        org_id = whoami(token)['organizations'][0]['id']

        if hasattr(settings.bow_config, 'license') and settings.bow_config.license:
            settings.bow_config.license.key = None
        clear_license_cache()

        response = test_client.post(
            "/api/enterprise/ldap/sync",
            headers={
                "Authorization": f"Bearer {token}",
                "X-Organization-Id": org_id,
            },
        )
        assert response.status_code == 402

    def test_test_connection_requires_license(
        self, test_client, create_user, login_user, whoami, license_env_cleanup, enable_ldap,
    ):
        """Test that test-connection requires enterprise license."""
        from app.ee.license import clear_license_cache
        from app.settings.config import settings

        user = create_user()
        token = login_user(user["email"], user["password"])
        org_id = whoami(token)['organizations'][0]['id']

        if hasattr(settings.bow_config, 'license') and settings.bow_config.license:
            settings.bow_config.license.key = None
        clear_license_cache()

        response = test_client.get(
            "/api/enterprise/ldap/test-connection",
            headers={
                "Authorization": f"Bearer {token}",
                "X-Organization-Id": org_id,
            },
        )
        assert response.status_code == 402


@pytest.mark.e2e
class TestLdapRequiresConfig:

    def test_sync_requires_ldap_enabled(
        self, test_client, create_user, login_user, whoami, enterprise_license,
    ):
        """Test that LDAP sync fails when LDAP is not configured."""
        from app.settings.config import settings

        user = create_user()
        token = login_user(user["email"], user["password"])
        org_id = whoami(token)['organizations'][0]['id']

        # Ensure LDAP is disabled
        settings.bow_config.ldap.enabled = False

        response = test_client.post(
            "/api/enterprise/ldap/sync",
            headers={
                "Authorization": f"Bearer {token}",
                "X-Organization-Id": org_id,
            },
        )
        assert response.status_code == 400
        assert "not configured" in response.json()["detail"].lower()
