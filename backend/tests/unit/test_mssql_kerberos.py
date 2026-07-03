"""Unit tests for SQL Server Kerberos support.

Covers:
- MSSQLClient connection-string construction for Kerberos (Windows Integrated)
  vs SQL login auth, and the ODBC-keyword injection guard
- the MSSQL registry auth variants (kerberos / kerberos_delegated) and their
  credentials schemas
- KerberosTicketManager: ccache acquisition via S4U2Self, per-principal
  caching, KRB5CCNAME activation/restore, and the missing-gssapi error
- ConnectionService._kerberos_delegated_credentials resolution rules

python-gssapi is mocked via sys.modules, so these run without krb5 or an AD
domain available.
"""
from __future__ import annotations

import sys
import types
from urllib.parse import parse_qs, unquote_plus, urlsplit

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.data_sources.clients.mssql_client import MSSQLClient


def _odbc_params(client: MSSQLClient) -> dict:
    """Decode the odbc_connect payload of the SQLAlchemy URI into a dict."""
    query = urlsplit(client.sql_server_uri).query
    odbc = parse_qs(query)["odbc_connect"][0]
    odbc = unquote_plus(odbc) if "%" in odbc or "+" in odbc else odbc
    pairs = [p for p in odbc.split(";") if p]
    return {k.strip().lower(): v for k, v in (p.split("=", 1) for p in pairs)}


# ---------- connection string ---------- #


def test_userpass_uri_unchanged():
    client = MSSQLClient("db.corp.example.com", 1433, "dwh", "svc", "secret")
    params = _odbc_params(client)
    assert params["uid"] == "svc"
    assert params["pwd"] == "secret"
    assert "trusted_connection" not in params


def test_kerberos_uri_uses_integrated_auth_and_omits_sql_login():
    client = MSSQLClient(
        "db.corp.example.com", 1433, "dwh", use_kerberos=True,
    )
    params = _odbc_params(client)
    assert params["trusted_connection"] == "yes"
    assert "uid" not in params
    assert "pwd" not in params
    assert params["server"] == "db.corp.example.com,1433"


def test_additional_params_cannot_flip_auth_scheme():
    client = MSSQLClient(
        "db.corp.example.com", 1433, "dwh", "svc", "secret",
        additional_params={
            "Trusted_Connection": "yes",
            "Authentication": "ActiveDirectoryIntegrated",
            "Integrated Security": "SSPI",
            "ApplicationIntent": "ReadOnly",
        },
    )
    params = _odbc_params(client)
    assert "trusted_connection" not in params
    assert "authentication" not in params
    assert "integrated security" not in params
    assert params["applicationintent"] == "ReadOnly"


def test_kerberos_principals_normalized():
    client = MSSQLClient(
        "db.corp.example.com", 1433, "dwh", use_kerberos=True,
        kerberos_principal="  ", kerberos_impersonate=" jdoe@CORP.EXAMPLE.COM ",
    )
    assert client.kerberos_principal is None
    assert client.kerberos_impersonate == "jdoe@CORP.EXAMPLE.COM"


# ---------- registry / schemas ---------- #


def test_registry_exposes_kerberos_auth_variants():
    from app.schemas.data_source_registry import get_entry

    entry = get_entry("MSSQL")
    by_auth = entry.credentials_auth.by_auth
    assert set(by_auth) == {"userpass", "kerberos", "kerberos_delegated"}
    assert by_auth["kerberos"].scopes == ["system"]
    assert by_auth["kerberos_delegated"].scopes == ["user"]
    # default UX unchanged
    assert entry.credentials_auth.default == "userpass"


def test_kerberos_credentials_schema_validation():
    from app.schemas.data_sources.configs import (
        MssqlKerberosCredentials,
        MssqlKerberosDelegatedCredentials,
    )

    assert MssqlKerberosCredentials().kerberos_principal is None
    assert MssqlKerberosCredentials(kerberos_principal="svc@REALM").use_kerberos is True
    with pytest.raises(ValidationError):
        MssqlKerberosCredentials(use_kerberos=False)

    delegated = MssqlKerberosDelegatedCredentials(kerberos_impersonate="jdoe@corp.example.com")
    assert delegated.use_kerberos is True
    with pytest.raises(ValidationError):
        MssqlKerberosDelegatedCredentials(use_kerberos=False)


# ---------- ticket manager (fake gssapi) ---------- #


class _FakeCreds:
    def __init__(self, lifetime=3600):
        self.lifetime = lifetime


class _FakeAcquireResult:
    def __init__(self, lifetime=3600):
        self.creds = _FakeCreds(lifetime)
        self.lifetime = lifetime


def _install_fake_gssapi(monkeypatch, store_calls, impersonate_calls, lifetime=3600):
    fake = types.ModuleType("gssapi")

    class _Name:
        def __init__(self, name, name_type=None):
            self.name = name
            self.name_type = name_type

    class _Credentials:
        def __init__(self, usage=None, name=None, store=None):
            self.usage = usage
            self.name = name
            self.store = store
            self.lifetime = lifetime

    def _acquire_cred_impersonate_name(impersonator, name, usage=None):
        impersonate_calls.append((impersonator, name.name, usage))
        return _FakeAcquireResult(lifetime)

    def _store_cred_into(store, creds, usage=None, overwrite=None, set_default=None):
        store_calls.append((store, creds, usage, overwrite, set_default))
        # materialize the file like a real store would
        path = store["ccache"].split(":", 1)[1]
        with open(path, "w") as f:
            f.write("fake-ccache")

    fake.Name = _Name
    fake.Credentials = _Credentials
    fake.NameType = types.SimpleNamespace(
        kerberos_principal="krb-principal", enterprise_principal="enterprise"
    )
    fake.raw = types.SimpleNamespace(
        acquire_cred_impersonate_name=_acquire_cred_impersonate_name,
        store_cred_into=_store_cred_into,
    )
    monkeypatch.setitem(sys.modules, "gssapi", fake)
    return fake


def test_delegated_ccache_acquires_and_caches(tmp_path, monkeypatch):
    from app.data_sources.kerberos import KerberosTicketManager

    store_calls, impersonate_calls = [], []
    _install_fake_gssapi(monkeypatch, store_calls, impersonate_calls)
    mgr = KerberosTicketManager(ccache_dir=str(tmp_path), client_keytab="/etc/bow/svc.keytab")

    path1 = mgr.delegated_ccache("jdoe@corp.example.com")
    path2 = mgr.delegated_ccache("jdoe@corp.example.com")
    other = mgr.delegated_ccache("asmith@corp.example.com")

    assert path1 == path2  # cached, no re-acquisition
    assert len(impersonate_calls) == 2
    assert path1 != other
    assert path1.startswith(str(tmp_path))
    # S4U2Self was asked for the right user, with initiate usage
    assert impersonate_calls[0][1] == "jdoe@corp.example.com"
    assert impersonate_calls[0][2] == "initiate"


def test_delegated_ccache_refreshes_near_expiry(tmp_path, monkeypatch):
    from app.data_sources.kerberos import KerberosTicketManager

    store_calls, impersonate_calls = [], []
    # lifetime below the refresh margin → every call re-acquires
    _install_fake_gssapi(monkeypatch, store_calls, impersonate_calls, lifetime=30)
    mgr = KerberosTicketManager(ccache_dir=str(tmp_path), client_keytab="/etc/bow/svc.keytab")

    mgr.delegated_ccache("jdoe@corp.example.com")
    mgr.delegated_ccache("jdoe@corp.example.com")
    assert len(impersonate_calls) == 2


def test_delegated_ccache_rejects_bare_username(tmp_path):
    from app.data_sources.kerberos import KerberosDelegationError, KerberosTicketManager

    mgr = KerberosTicketManager(ccache_dir=str(tmp_path))
    with pytest.raises(KerberosDelegationError):
        mgr.delegated_ccache("jdoe")
    with pytest.raises(KerberosDelegationError):
        mgr.delegated_ccache("")


def test_service_ccache_default_is_noop(tmp_path):
    from app.data_sources.kerberos import KerberosTicketManager

    mgr = KerberosTicketManager(ccache_dir=str(tmp_path))
    assert mgr.service_ccache(None) is None
    assert mgr.service_ccache("") is None


def test_missing_gssapi_raises_configuration_error(tmp_path, monkeypatch):
    from app.data_sources.kerberos import (
        KerberosConfigurationError,
        KerberosTicketManager,
    )

    monkeypatch.setitem(sys.modules, "gssapi", None)
    mgr = KerberosTicketManager(ccache_dir=str(tmp_path))
    with pytest.raises(KerberosConfigurationError):
        mgr.delegated_ccache("jdoe@corp.example.com")


def test_activate_sets_and_restores_krb5ccname(tmp_path, monkeypatch):
    from app.data_sources.kerberos import KerberosTicketManager

    mgr = KerberosTicketManager(ccache_dir=str(tmp_path))
    monkeypatch.setenv("KRB5CCNAME", "FILE:/original")
    import os
    with mgr.activate(str(tmp_path / "cc")):
        assert os.environ["KRB5CCNAME"] == f"FILE:{tmp_path / 'cc'}"
    assert os.environ["KRB5CCNAME"] == "FILE:/original"

    monkeypatch.delenv("KRB5CCNAME", raising=False)
    with mgr.activate(str(tmp_path / "cc")):
        assert os.environ["KRB5CCNAME"] == f"FILE:{tmp_path / 'cc'}"
    assert "KRB5CCNAME" not in os.environ

    # None → no env mutation at all
    with mgr.activate(None):
        assert "KRB5CCNAME" not in os.environ


# ---------- resolve_credentials helper ---------- #


class _FakeConnection:
    def __init__(self, modes):
        self.allowed_user_auth_modes = modes


class _FakeUser:
    def __init__(self, email):
        self.email = email


class _FakeRow:
    def __init__(self, auth_mode, creds=None):
        self.auth_mode = auth_mode
        self._creds = creds or {}

    def decrypt_credentials(self):
        return self._creds


def _resolve(connection, user, row):
    from app.services.connection_service import ConnectionService

    return ConnectionService._kerberos_delegated_credentials(connection, user, row)


def test_kerberos_sso_not_enabled_returns_none():
    assert _resolve(_FakeConnection([]), _FakeUser("a@b.c"), None) is None
    assert _resolve(_FakeConnection(["oauth"]), _FakeUser("a@b.c"), None) is None


def test_kerberos_sso_derives_upn_from_login_email():
    creds = _resolve(_FakeConnection(["kerberos_delegated"]), _FakeUser("jdoe@corp.example.com"), None)
    assert creds == {"use_kerberos": True, "kerberos_impersonate": "jdoe@corp.example.com"}


def test_kerberos_sso_explicit_row_principal_wins():
    row = _FakeRow("kerberos_delegated", {"kerberos_impersonate": "j.doe@ad.corp.example.com"})
    creds = _resolve(_FakeConnection(["kerberos_delegated"]), _FakeUser("jdoe@corp.example.com"), row)
    assert creds["kerberos_impersonate"] == "j.doe@ad.corp.example.com"


def test_kerberos_sso_honors_other_real_auth_mode():
    row = _FakeRow("userpass", {"user": "u", "password": "p"})
    assert _resolve(_FakeConnection(["kerberos_delegated"]), _FakeUser("a@b.c"), row) is None


def test_kerberos_sso_ignores_service_account_marker_row():
    row = _FakeRow("service_account", {})
    creds = _resolve(_FakeConnection(["kerberos_delegated"]), _FakeUser("jdoe@corp.example.com"), row)
    assert creds["kerberos_impersonate"] == "jdoe@corp.example.com"


def test_kerberos_sso_requires_upn_shaped_identity():
    with pytest.raises(HTTPException) as exc:
        _resolve(_FakeConnection(["kerberos_delegated"]), _FakeUser("no-upn-here"), None)
    assert exc.value.status_code == 403
