"""Delegation is authorized by a verified directory object, never form input."""
from types import SimpleNamespace
import pytest
from app.services.connection_identity import resolve_kerberos_principal


@pytest.mark.parametrize("email", ["admin@example.test", "reader@example.test"])
@pytest.mark.parametrize("override", [None, "administrator@EXAMPLE.TEST"])
def test_unverified_identity_cannot_delegate(email, override):
    user = SimpleNamespace(email=email)
    row = SimpleNamespace(auth_mode="kerberos_delegated", decrypt_credentials=lambda: {"kerberos_impersonate": override})
    assert resolve_kerberos_principal(user, row) is None


@pytest.mark.parametrize("saved", [None, "administrator@EXAMPLE.TEST"])
def test_verified_object_controls_principal_not_saved_override(monkeypatch, saved):
    from app.settings.config import settings
    from app.settings.bow_config import LDAPConfig
    from app.ee.ldap.connection import LDAPConnectionManager
    config = LDAPConfig(enabled=True, url="ldaps://ad.example.test", organization_id="org",
        admission_group_dn="cn=admitted", kerberos_realm="EXAMPLE.TEST")
    monkeypatch.setattr(settings.bow_config, "ldap", config)
    provider = LDAPConnectionManager(config).provider_id
    identity = {"provider": provider, "guid": "immutable-object", "sid_hex": "010100000000000501000000",
        "principal": "reader@EXAMPLE.TEST"}
    user = SimpleNamespace(email="unrelated@example.com", ldap_subject=provider+":immutable-object", ldap_identity=identity)
    row = SimpleNamespace(auth_mode="kerberos_delegated", decrypt_credentials=lambda: {"kerberos_impersonate": saved})
    assert resolve_kerberos_principal(user, row) == identity["principal"]
    config.admission_group_dn = "cn=different-scope"
    assert resolve_kerberos_principal(user, row) is None
