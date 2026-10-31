"""A host application whose people live in Active Directory (identity source
``ldap``): the host's assertion names the person's objectGUID.

Only directory network operations are substituted (as in
test_ldap_auth_security.py); assertion checks, admission, user creation and
sessions run for real.

* With "create people" on, a GUID the directory knows and admits becomes a
  member on first sign-in, and the next sign-in is the same person, the
  same one a later directory login to BOW finds.
* A GUID the directory refuses (outside the admission group) or does not
  know creates no one. Without "create people", an unknown GUID is refused.
"""
import time
import uuid

import jwt
import pytest

from app.ee.ldap.connection import LDAPConnectionManager, LDAPSecurityError
from app.settings.bow_config import LDAPConfig
from app.settings.config import settings

pytestmark = pytest.mark.e2e


@pytest.fixture
def directory(test_client, create_user, login_user, whoami, monkeypatch):
    admin = create_user()
    token = login_user(admin["email"], admin["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    config = LDAPConfig(enabled=True, url="ldaps://ad.example.test", organization_id=org_id,
                        admission_group_dn="cn=bow-users,dc=example,dc=test", kerberos_realm="EXAMPLE.TEST",
                        auto_provision_users=True)
    monkeypatch.setattr(settings.bow_config, "ldap", config)
    people = {}

    def identity_by_guid(self, guid):
        person = people.get(guid)
        if person == "refused":
            raise LDAPSecurityError("not in the admission group")
        return person

    monkeypatch.setattr(LDAPConnectionManager, "identity_by_guid", identity_by_guid)
    monkeypatch.setattr(LDAPConnectionManager, "read_identity", lambda self, dn: next(
        (p for p in people.values() if isinstance(p, dict) and p["dn"] == dn), None))

    def person(name, *, admitted=True):
        guid = str(uuid.uuid4())
        people[guid] = "refused" if not admitted else {
            "provider": LDAPConnectionManager(config).provider_id, "guid": guid, "sid_hex": "010100000000000501000000",
            "principal": f"{name}@EXAMPLE.TEST", "email": f"{name}-{guid[:6]}@example.com", "name": name.title(),
            "dn": f"cn={name},dc=example,dc=test", "organization_id": org_id}
        return guid

    headers = {"Authorization": f"Bearer {token}", "X-Organization-Id": org_id}
    return {"org_id": org_id, "headers": headers, "person": person, "people": people}


def _host(client, directory, create_users):
    r = client.post("/api/host_applications", headers=directory["headers"], json={
        "name": "Intranet", "allowed_origins": ["https://intranet.example.com"], "identity_mode": "assertion",
        "identity_source": "ldap", "issuer": "intranet", "create_users": create_users})
    assert r.status_code == 201, r.text
    return r.json()


def _sign_in(client, host, guid):
    now = int(time.time())
    assertion = jwt.encode({"iss": "intranet", "aud": host["public_id"], "sub": guid, "iat": now, "exp": now + 60,
                            "jti": uuid.uuid4().hex}, host["private_key"], algorithm="RS256")
    return client.post(f"/api/embed/{host['public_id']}/session", json={"assertion": assertion})


def test_a_directory_person_joins_on_first_sign_in_and_stays_the_same_person(test_client, directory):
    host = _host(test_client, directory, create_users=True)
    guid = directory["person"]("maria")
    first = _sign_in(test_client, host, guid)
    assert first.status_code == 200, first.text
    again = _sign_in(test_client, host, guid)
    assert again.status_code == 200 and again.json()["user"]["id"] == first.json()["user"]["id"]
    members = test_client.get(f"/api/organizations/{directory['org_id']}/members", headers=directory["headers"]).json()
    joined = [m for m in members if m.get("user_id") == first.json()["user"]["id"]]
    assert len(joined) == 1 and joined[0]["role"] == "member"


@pytest.mark.parametrize("case", ["refused_by_directory", "unknown_to_directory", "not_a_guid"])
def test_the_directory_decides_who_may_join(test_client, directory, case):
    host = _host(test_client, directory, create_users=True)
    guid = {"refused_by_directory": lambda: directory["person"]("eve", admitted=False),
            "unknown_to_directory": lambda: str(uuid.uuid4()),
            "not_a_guid": lambda: "eve@example.com"}[case]()
    before = len(test_client.get(f"/api/organizations/{directory['org_id']}/members", headers=directory["headers"]).json())
    r = _sign_in(test_client, host, guid)
    assert r.status_code == 401 and r.json()["error_code"] == "embed.identity_unmapped"
    after = len(test_client.get(f"/api/organizations/{directory['org_id']}/members", headers=directory["headers"]).json())
    assert after == before


def test_without_create_people_only_known_directory_people_sign_in(test_client, directory):
    creating = _host(test_client, directory, create_users=True)
    known = directory["person"]("omar")
    assert _sign_in(test_client, creating, known).status_code == 200  # Omar exists in BOW now
    strict = _host(test_client, directory, create_users=False)
    assert _sign_in(test_client, strict, known).status_code == 200
    stranger = _sign_in(test_client, strict, directory["person"]("lena"))
    assert stranger.status_code == 401 and stranger.json()["error_code"] == "embed.identity_unmapped"
