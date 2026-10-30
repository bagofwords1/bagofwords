"""Host applications that sign people in through the identity provider
(``idp_token`` mode) and create them on first sign-in.

The provider's discovery document and keys are the boundary and are mocked
(app.services.entra_token_exchange._json); token checks, identity lookup,
user creation and sessions run for real.

* A token the configured provider signed, for this audience, signs in the
  person the provider's subject is linked to; a token from another key,
  audience or issuer, or an expired one, signs in no one.
* With "create people" on, a subject nobody holds becomes a member through
  the organization's own admission (an open invitation), linked to that
  subject, and the next token finds the same person. Without it, or when
  the organization would not admit them, no one is created.
* An existing account is never claimed by email: a token whose email
  belongs to someone else signs in no one and creates no one.
"""
from __future__ import annotations

import time
import uuid

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from tests.e2e.dashboards.dashboard_setup import as_, own, publish, share

pytestmark = pytest.mark.e2e

ISSUER = "https://login.example.com/tenant-1/v2.0"
AUDIENCE = "api://chinook-backoffice"
DASH = "conv_three_dash/dash_sales"


def _key():
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = jwt.algorithms.RSAAlgorithm.to_jwk(private.public_key(), as_dict=True)
    return private, jwk


class Provider:
    def __init__(self):
        self.private, jwk = _key()
        self.kid = uuid.uuid4().hex[:8]
        self.jwks = {"keys": [{**jwk, "kid": self.kid, "alg": "RS256", "use": "sig"}]}

    def token(self, sub, *, aud=AUDIENCE, iss=ISSUER, ttl=600, key=None, kid=None, **claims):
        now = int(time.time())
        body = {"iss": iss, "aud": aud, "sub": sub, "iat": now, "exp": now + ttl, **claims}
        return jwt.encode(body, key or self.private, algorithm="RS256", headers={"kid": kid or self.kid})


@pytest.fixture
def provider(monkeypatch):
    """An enabled OIDC provider 'entra' whose discovery and keys are served from here."""
    from app.services import entra_token_exchange
    from app.settings.bow_config import OIDCProvider
    from app.settings.config import settings

    idp = Provider()

    async def fake_json(url, refresh=False):
        if url == f"{ISSUER}/.well-known/openid-configuration":
            return {"issuer": ISSUER, "jwks_uri": "https://login.example.com/tenant-1/discovery/v2.0/keys"}
        if url == "https://login.example.com/tenant-1/discovery/v2.0/keys":
            return idp.jwks
        raise AssertionError(f"unexpected fetch {url}")

    monkeypatch.setattr(entra_token_exchange, "_json", fake_json)
    providers = settings.bow_config.oidc_providers
    entry = OIDCProvider(name="entra", enabled=True, issuer=ISSUER, client_id="bow", uid_claim="oid")
    providers.append(entry)
    try:
        yield idp
    finally:
        providers.remove(entry)


def _host(client, corpus, *, create_users=False):
    r = client.post("/api/host_applications", headers=as_(corpus, "admin"), json={
        "name": "Back office (Entra)", "allowed_origins": ["https://erp.example.com"], "identity_mode": "idp_token",
        "identity_source": "oidc:entra", "audience": AUDIENCE, "create_users": create_users})
    assert r.status_code == 201, r.text
    return r.json()


def _session(client, host, token):
    return client.post(f"/api/embed/{host['public_id']}/session", json={"id_token": token})


def _link(corpus_rows, corpus, person, oid):
    """The person signed in to BOW through 'entra' once (the OAuth account
    that login leaves behind)."""
    corpus_rows.insert("oauth_accounts", id=str(uuid.uuid4()), oauth_name="entra", access_token="",
                       account_id=oid, account_email=corpus.cast.people[person].email,
                       user_id=corpus.cast.id(person))


def _count(corpus_rows, sql, **params) -> int:
    from sqlalchemy import text

    with corpus_rows.engine.connect() as conn:
        return conn.execute(text(sql), params).scalar()


def _invite(client, corpus, email):
    """The organization's own admission: an open invitation for that address."""
    r = client.post(f"/api/organizations/{corpus.cast.org_id}/members", headers=as_(corpus, "admin"),
                    json={"organization_id": corpus.cast.org_id, "email": email, "role": "member"})
    assert r.status_code == 200, r.text


def test_a_provider_token_signs_in_the_person_it_names(provider, corpus, corpus_rows, test_client):
    _link(corpus_rows, corpus, "member", "oid-member-1")
    host = _host(test_client, corpus)
    r = _session(test_client, host, provider.token("ignored", oid="oid-member-1"))
    assert r.status_code == 200, r.text
    assert r.json()["user"]["id"] == corpus.cast.id("member")
    # And it opens what the artifact allows for that host, as that person.
    aid = own(corpus_rows, corpus, DASH)
    publish(aid)
    share(test_client, corpus, aid, "internal")
    test_client.put(f"/api/artifacts/{aid}/embedding", json={"host_application_ids": [host["id"]]},
                    headers=as_(corpus, "owner"))
    assert test_client.get(f"/api/d/{aid}", headers={"Authorization": f"Bearer {r.json()['token']}"}).status_code == 200


def test_tokens_the_provider_did_not_issue_for_this_host_sign_in_no_one(provider, corpus, corpus_rows, test_client):
    _link(corpus_rows, corpus, "member", "oid-member-2")
    host = _host(test_client, corpus)
    stranger, _ = _key()
    bad = {
        "other_key_same_kid": provider.token("x", oid="oid-member-2", key=stranger),
        "unknown_kid": provider.token("x", oid="oid-member-2", key=stranger, kid="nope"),
        "audience": provider.token("x", oid="oid-member-2", aud="api://someone-else"),
        "issuer": provider.token("x", oid="oid-member-2", iss="https://evil.example.com/v2.0"),
        "expired": provider.token("x", oid="oid-member-2", ttl=-600),
        "no_subject": provider.token("x"),
        "assertion_instead": None,
    }
    codes = {}
    for name, token in bad.items():
        r = (test_client.post(f"/api/embed/{host['public_id']}/session", json={"assertion": "a.b.c"})
             if token is None else _session(test_client, host, token))
        codes[name] = (r.status_code, r.json().get("error_code"))
    assert all(code == (401, "embed.assertion_invalid") for code in codes.values()), codes


def test_without_create_people_an_unknown_subject_is_refused(provider, corpus, test_client):
    host = _host(test_client, corpus)
    r = _session(test_client, host, provider.token("x", oid="oid-new", email="newcomer@corpus.com"))
    assert r.status_code == 401 and r.json()["error_code"] == "embed.identity_unmapped"


def test_create_people_admits_a_newcomer_once_through_the_orgs_own_policy(provider, corpus, corpus_rows, test_client):
    host = _host(test_client, corpus, create_users=True)
    token = lambda: provider.token("x", oid="oid-jit-1", email="Newcomer@Corpus.com", name="New Comer")  # noqa: E731
    # Nobody invited them: nobody is created.
    refused = _session(test_client, host, token())
    assert refused.status_code == 401 and refused.json()["error_code"] == "embed.identity_unmapped"
    assert _count(corpus_rows, "select count(*) from users where lower(email) = 'newcomer@corpus.com'") == 0

    _invite(test_client, corpus, "newcomer@corpus.com")
    first = _session(test_client, host, token())
    assert first.status_code == 200, first.text
    again = _session(test_client, host, token())
    assert again.status_code == 200 and again.json()["user"]["id"] == first.json()["user"]["id"]
    members = test_client.get(f"/api/organizations/{corpus.cast.org_id}/members", headers=as_(corpus, "admin")).json()
    joined = [m for m in members if m.get("user_id") == first.json()["user"]["id"]]
    assert len(joined) == 1 and joined[0]["role"] == "member"


def _domain_signup(corpus_rows, corpus, domain):
    """The organization admits anyone at ``domain`` (enterprise domain signup).
    Written directly: the corpus's license fake reaches the admission code
    but not the settings route, which imported its own license check."""
    import json

    from sqlalchemy import text

    with corpus_rows.engine.begin() as conn:
        row = conn.execute(text("select id, config from organization_settings where organization_id = :o"),
                           {"o": corpus.cast.org_id}).first()
        config = row.config if isinstance(row.config, dict) else json.loads(row.config or "{}")
        config["signup_policy"] = {"enabled": True, "allowed_domains": [domain], "auto_invite_role": "member"}
        conn.execute(text("update organization_settings set config = :c where id = :i"),
                     {"c": json.dumps(config), "i": row.id})


@pytest.mark.parametrize("admission", ["domain_signup", "invitation"])
def test_an_existing_account_is_never_claimed_by_email(provider, corpus, corpus_rows, test_client, multi_org,
                                                       admission):
    # An account that exists (in another organization), and this organization
    # would admit its address: a token merely claiming that email must not
    # bind the account to the token's subject.
    victim = corpus.cast.people["other_org"].email
    if admission == "domain_signup":
        _domain_signup(corpus_rows, corpus, victim.split("@")[1])
    else:
        _invite(test_client, corpus, victim)
    host = _host(test_client, corpus, create_users=True)
    r = _session(test_client, host, provider.token("x", oid="oid-attacker", email=victim))
    assert r.status_code == 401 and r.json()["error_code"] == "embed.identity_unmapped"
    assert _count(corpus_rows, "select count(*) from oauth_accounts where account_id = 'oid-attacker'") == 0
