"""
Unit tests for connection_service.default_user_auth_modes.

The create/edit forms have no auth-mode picker, so this helper is what makes
"Require user authentication" actually enable the /oauth/authorize route.
A type missing here fails closed: OAuth sign-in is silently unreachable.
"""
import pytest

from app.services.connection_service import default_user_auth_modes


class TestEntraTypes:
    @pytest.mark.parametrize("conn_type", ["powerbi", "ms_fabric", "sharepoint", "onedrive", "outlook_mail"])
    def test_entra_types_default_to_oauth_unconditionally(self, conn_type):
        assert default_user_auth_modes(conn_type, {}, {}) == ["oauth"]


class TestOAuthAppTypes:
    @pytest.mark.parametrize("conn_type", ["servicenow", "snowflake", "bigquery"])
    def test_oauth_when_client_configured(self, conn_type):
        creds = {"oauth_client_id": "c", "oauth_client_secret": "s"}
        assert default_user_auth_modes(conn_type, {}, creds) == ["oauth"]

    @pytest.mark.parametrize("conn_type", ["servicenow", "snowflake", "bigquery"])
    def test_no_default_without_oauth_client(self, conn_type):
        # Users may still bring their own credentials — modes stay unset.
        assert default_user_auth_modes(conn_type, {}, {"user": "u", "password": "p"}) is None

    def test_none_credentials_tolerated(self):
        assert default_user_auth_modes("snowflake", None, None) is None


class TestKerberos:
    def test_mssql_kerberos_system_auth_defaults_to_kerberos_delegated(self):
        assert default_user_auth_modes("MSSQL", {"auth_type": "kerberos"}, {}) == ["kerberos_delegated"]

    def test_mssql_password_auth_has_no_default(self):
        assert default_user_auth_modes("MSSQL", {"auth_type": "sql"}, {}) is None


class TestNoDefault:
    @pytest.mark.parametrize("conn_type", ["postgresql", "mysql", "clickhouse"])
    def test_userpass_only_types_have_no_default(self, conn_type):
        assert default_user_auth_modes(conn_type, {}, {"oauth_client_id": "c"}) is None
