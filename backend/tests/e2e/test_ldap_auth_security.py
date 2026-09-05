"""Real login/DB policy with only directory network operations substituted."""
import uuid
import pytest
from app.settings.config import settings
from app.settings.bow_config import LDAPConfig
from app.ee.ldap.connection import LDAPConnectionManager


def login(client, email, password):
    return client.post("/api/auth/jwt/login", data={"username": email, "password": password})


@pytest.mark.parametrize("enabled", [False, True])
def test_public_settings_exposes_only_directory_login_availability(test_client, monkeypatch, enabled):
    monkeypatch.setattr(settings.bow_config, "ldap", LDAPConfig(enabled=enabled,
        bind_dn="cn=private-lookup", bind_password="private-bind-password"))
    response = test_client.get("/api/settings")
    assert response.status_code == 200
    assert response.json()["ldap"] == {"enabled": enabled}
    assert "private-lookup" not in response.text
    assert "private-bind-password" not in response.text


@pytest.mark.e2e
def test_directory_outage_never_allows_local_password(test_client, create_user, monkeypatch):
    user = create_user()
    def unavailable(*args):
        raise ConnectionError("synthetic directory outage")
    monkeypatch.setattr(LDAPConnectionManager, "find_user_dn", unavailable)
    monkeypatch.setattr(settings.bow_config, "ldap", LDAPConfig(enabled=True, url="ldaps://ad.example.test"))
    assert login(test_client, user["email"], user["password"]).status_code in (400, 401, 403)


@pytest.mark.e2e
def test_directory_login_cannot_claim_existing_local_account(test_client, create_user, monkeypatch):
    user = create_user()
    monkeypatch.setattr(LDAPConnectionManager, "find_user_dn", lambda *a: "cn=directory-person")
    monkeypatch.setattr(LDAPConnectionManager, "bind_user", lambda *a: True)
    identity = {"provider": "synthetic", "guid": str(uuid.uuid4()), "sid_hex": "010100000000000501000000",
                "principal": "person@EXAMPLE.TEST", "email": user["email"], "dn": "cn=directory-person"}
    monkeypatch.setattr(LDAPConnectionManager, "authenticate_identity", lambda *a: identity)
    monkeypatch.setattr(settings.bow_config, "ldap", LDAPConfig(enabled=True, url="ldaps://ad.example.test"))
    assert login(test_client, user["email"], "directory-password").status_code in (400, 401, 403)


@pytest.mark.e2e
def test_admitted_directory_member_gets_revocable_sso_session(test_client, create_user, login_user, whoami, monkeypatch):
    admin = create_user()
    org_id = whoami(login_user(admin["email"], admin["password"]))["organizations"][0]["id"]
    email = f"directory-{uuid.uuid4().hex}@example.com"
    config = LDAPConfig(enabled=True, url="ldaps://ad.example.test", organization_id=org_id,
        admission_group_dn="cn=admitted,dc=example,dc=test", kerberos_realm="EXAMPLE.TEST", auto_provision_users=True)
    monkeypatch.setattr(settings.bow_config, "ldap", config)
    monkeypatch.setattr(settings.bow_config.auth, "mode", "sso_only")
    identity = {"provider": LDAPConnectionManager(config).provider_id, "guid": str(uuid.uuid4()),
        "sid_hex": "010100000000000501000000", "principal": "member@EXAMPLE.TEST", "email": email,
        "name": "Directory member", "dn": "cn=member,dc=example,dc=test", "organization_id": org_id}
    monkeypatch.setattr(LDAPConnectionManager, "authenticate_identity", lambda *a: identity)
    monkeypatch.setattr(LDAPConnectionManager, "read_identity", lambda *a: identity)
    response = login(test_client, email, "synthetic-directory-password")
    assert response.status_code == 200, response.text
    token = response.json()["access_token"]
    info = whoami(token)
    assert len(info["organizations"]) == 1
    assert info["organizations"][0]["role"] == "member"
    def revoked(*a):
        raise ConnectionError("directory access revoked")
    monkeypatch.setattr(LDAPConnectionManager, "read_identity", revoked)
    assert test_client.get("/api/users/whoami", headers={"Authorization": f"Bearer {token}"}).status_code in (401, 403)
