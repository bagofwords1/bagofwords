"""Host applications and embedded artifacts (Settings -> Integrations,
an artifact's Embed tab, /api/embed/*).

The contract, through the HTTP surface:

* Host applications are configured by ``manage_settings`` only. The signing
  private key is readable once (create / rotate); edits are revision-checked.
* A credential matching the host's identity mode becomes a short-lived
  ``bow_embed_`` session for exactly one existing member. Anything else
  (wrong key, issuer, audience, expired, too long-lived, replayed, algorithm
  confusion, unmapped or ambiguous subject, disabled host) is refused with a
  typed error and no session.
* An embed session opens a published artifact only when that artifact allows
  the session's host application, and then only as far as the user's own
  sharing allows. It is refused everywhere else in the API, and stops
  working after logout, key rotation, revocation, disabling or removal from
  the organization.
* SSO mode: a signed-in member gets a one-time code bound to PKCE.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
import uuid

import jwt
import pytest

from tests.e2e.dashboards.dashboard_setup import as_, own, publish, share

pytestmark = pytest.mark.e2e

DASH = "conv_three_dash/dash_sales"
OTHER_DASH = "conv_three_dash/deck"
ERP = "https://erp.example.com"


# ── helpers ───────────────────────────────────────────────────────────────

def sign(private_key, *, aud, sub, iss="erp", jti=None, iat=None, ttl=60, algorithm="RS256", **extra):
    now = int(time.time())
    claims = {"iss": iss, "aud": aud, "sub": sub, "iat": now if iat is None else iat,
              "jti": jti or uuid.uuid4().hex}
    claims["exp"] = claims["iat"] + ttl
    claims.update(extra)
    return jwt.encode(claims, private_key, algorithm=algorithm)


def forge_hs256(claims: dict, key: bytes) -> str:
    """HS256 signed with the PUBLIC key's bytes: the algorithm-confusion attack
    (PyJWT refuses to build it, so it is assembled by hand)."""
    def b64(raw: bytes) -> str:
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
    head = b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    body = b64(json.dumps(claims).encode())
    sig = hmac.new(key, f"{head}.{body}".encode(), hashlib.sha256).digest()
    return f"{head}.{body}.{b64(sig)}"


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


class Embed:
    def __init__(self, client, corpus):
        self.client, self.corpus = client, corpus
        self.admin = as_(corpus, "admin")

    def attribute(self, person, value, key="employee_id"):
        membership_id = self.corpus.cast.people[person].membership_id
        r = self.client.put(f"/api/organizations/{self.corpus.cast.org_id}/members/{membership_id}",
                            json={"custom_attributes": {key: value}}, headers=self.admin)
        assert r.status_code == 200, r.text

    def create(self, **overrides):
        body = {"name": "Priority ERP", "allowed_origins": [ERP], "identity_mode": "assertion",
                "identity_source": "member_attribute:employee_id", "issuer": "erp"}
        body.update(overrides)
        r = self.client.post("/api/host_applications", json=body, headers=self.admin)
        assert r.status_code == 201, r.text
        return r.json()

    def allow(self, artifact_id, *hosts, by="owner"):
        return self.client.put(f"/api/artifacts/{artifact_id}/embedding",
                               json={"host_application_ids": [h["id"] for h in hosts]},
                               headers=as_(self.corpus, by))

    def session(self, host, **credential):
        return self.client.post(f"/api/embed/{host['public_id']}/session", json=credential)

    def sign_in(self, host, sub, **claims):
        r = self.session(host, assertion=sign(host["private_key"], aud=host["public_id"], sub=sub, **claims))
        assert r.status_code == 200, r.text
        return r.json()["token"]


@pytest.fixture
def embed(test_client, corpus):
    return Embed(test_client, corpus)


@pytest.fixture
def published(corpus, corpus_rows, test_client):
    """The sales dashboard, published and open to the organization."""
    aid = own(corpus_rows, corpus, DASH)
    publish(aid)
    assert share(test_client, corpus, aid, "internal").status_code == 200
    return aid


# ── administration ────────────────────────────────────────────────────────

def test_private_key_is_readable_once(embed):
    host = embed.create()
    assert host["private_key"].startswith("-----BEGIN PRIVATE KEY-----")
    assert host["public_id"].startswith("host_") and host["key_fingerprint"]
    fetched = embed.client.get(f"/api/host_applications/{host['id']}", headers=embed.admin).json()
    listed = embed.client.get("/api/host_applications", headers=embed.admin).json()
    assert fetched["private_key"] is None and fetched["public_key"].startswith("-----BEGIN PUBLIC KEY-----")
    assert all("private_key" not in h for h in listed)

    rotated = embed.client.post(f"/api/host_applications/{host['id']}/rotate_key", headers=embed.admin).json()
    assert rotated["private_key"] and rotated["key_fingerprint"] != host["key_fingerprint"]


@pytest.mark.parametrize("person", ["member", "owner", "anonymous"])
def test_only_manage_settings_configures_host_applications(embed, person):
    host = embed.create()
    headers = as_(embed.corpus, person)
    calls = [
        embed.client.get("/api/host_applications", headers=headers),
        embed.client.post("/api/host_applications", json={"name": "x", "issuer": "i"}, headers=headers),
        embed.client.patch(f"/api/host_applications/{host['id']}", json={"revision": host["revision"], "name": "y"},
                           headers=headers),
        embed.client.post(f"/api/host_applications/{host['id']}/rotate_key", headers=headers),
        embed.client.post(f"/api/host_applications/{host['id']}/revoke_sessions", headers=headers),
        embed.client.delete(f"/api/host_applications/{host['id']}", headers=headers),
    ]
    assert {r.status_code for r in calls} <= {401, 403}, [r.status_code for r in calls]


def test_edits_are_revision_checked_and_origins_normalized(embed):
    host = embed.create()
    stale = embed.client.patch(f"/api/host_applications/{host['id']}",
                               json={"revision": host["revision"] + 3, "name": "late"}, headers=embed.admin)
    assert stale.status_code == 409 and stale.json()["error_code"] == "host_app.revision_conflict"
    ok = embed.client.patch(f"/api/host_applications/{host['id']}", json={
        "revision": host["revision"], "allowed_origins": ["https://ERP.example.com/", "https://portal.example.com:443",
                                                          "http://localhost:8080"]}, headers=embed.admin)
    assert ok.status_code == 200, ok.text
    assert ok.json()["allowed_origins"] == ["https://erp.example.com", "https://portal.example.com",
                                            "http://localhost:8080"]
    for bad in (["https://erp.example.com/path"], ["javascript:alert(1)"], ["https://u:p@erp.example.com"]):
        r = embed.client.patch(f"/api/host_applications/{host['id']}",
                               json={"revision": ok.json()["revision"], "allowed_origins": bad}, headers=embed.admin)
        assert r.status_code == 422, (bad, r.text)


@pytest.mark.parametrize("body", [
    {"identity_mode": "assertion", "issuer": None},
    {"identity_mode": "idp_token", "audience": None, "identity_source": "member_attribute:employee_id"},
    {"identity_source": "member_attribute:"},
    {"identity_source": "member_attribute:employee_id", "create_users": True},
    {"identity_source": "scim", "create_users": True},
    {"identity_source": "saml:not-configured"},
    {"identity_source": "oidc:not-configured"},
])
def test_identity_configuration_that_cannot_work_is_refused(embed, body):
    payload = {"name": "x", "allowed_origins": [ERP], "identity_mode": "assertion",
               "identity_source": "member_attribute:employee_id", "issuer": "erp", **body}
    r = embed.client.post("/api/host_applications", json=payload, headers=embed.admin)
    assert r.status_code in (400, 422), r.text
    if r.status_code == 400:
        assert r.json()["error_code"] == "host_app.config_invalid"


# ── signing in ────────────────────────────────────────────────────────────

def test_a_signed_assertion_signs_in_the_member_it_names(embed):
    embed.attribute("member", "E-1042")
    host = embed.create()
    r = embed.session(host, assertion=sign(host["private_key"], aud=host["public_id"], sub="E-1042"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["token"].startswith("bow_embed_")
    assert body["user"]["id"] == embed.corpus.cast.id("member")
    status = embed.client.get(f"/api/embed/{host['public_id']}/session", headers=bearer(body["token"]))
    assert status.status_code == 200 and status.json()["token"] == ""


def test_a_session_s_expiry_is_sent_in_utc(embed):
    # The page schedules renewal from it: a time without a zone is read as the
    # browser's local time, hours off anywhere but UTC.
    from datetime import datetime, timezone

    embed.attribute("member", "E-2048")
    host = embed.create()
    body = embed.session(host, assertion=sign(host["private_key"], aud=host["public_id"], sub="E-2048")).json()
    expires = datetime.fromisoformat(body["expires_at"].replace("Z", "+00:00"))
    assert expires.utcoffset() is not None and expires.utcoffset().total_seconds() == 0
    remaining = (expires - datetime.now(timezone.utc)).total_seconds()
    assert 50 * 60 < remaining <= 60 * 60 + 5
    status = embed.client.get(f"/api/embed/{host['public_id']}/session", headers=bearer(body["token"])).json()
    assert status["expires_at"] == body["expires_at"]


def _refusals(embed, host, other_key, public_key_pem):
    aud, key = host["public_id"], host["private_key"]
    now = int(time.time())
    yield "wrong_key", sign(other_key, aud=aud, sub="E-1"), "embed.assertion_invalid"
    yield "issuer", sign(key, aud=aud, sub="E-1", iss="someone-else"), "embed.assertion_invalid"
    yield "audience", sign(key, aud="host_another", sub="E-1"), "embed.assertion_invalid"
    yield "expired", sign(key, aud=aud, sub="E-1", iat=now - 600, ttl=60), "embed.assertion_invalid"
    yield "lifetime", sign(key, aud=aud, sub="E-1", ttl=3600), "embed.assertion_invalid"
    yield "future", sign(key, aud=aud, sub="E-1", iat=now + 600), "embed.assertion_invalid"
    yield "hs256", forge_hs256({"iss": "erp", "aud": aud, "sub": "E-1", "iat": now, "exp": now + 60,
                                "jti": uuid.uuid4().hex}, public_key_pem.encode()), "embed.assertion_invalid"
    yield "none", jwt.encode({"iss": "erp", "aud": aud, "sub": "E-1", "iat": now, "exp": now + 60,
                              "jti": uuid.uuid4().hex}, None, algorithm="none"), "embed.assertion_invalid"
    yield "no_jti", jwt.encode({"iss": "erp", "aud": aud, "sub": "E-1", "iat": now, "exp": now + 60}, key,
                               algorithm="RS256"), "embed.assertion_invalid"
    yield "unmapped", sign(key, aud=aud, sub="E-nobody"), "embed.identity_unmapped"


def test_every_bad_assertion_is_refused_with_a_typed_error(embed):
    from app.services.host_application_service import generate_keypair

    embed.attribute("member", "E-1")
    host = embed.create()
    public_key = embed.client.get(f"/api/host_applications/{host['id']}", headers=embed.admin).json()["public_key"]
    other_key = generate_keypair()[0]
    results = {}
    for name, assertion, code in _refusals(embed, host, other_key, public_key):
        r = embed.session(host, assertion=assertion)
        results[name] = (r.status_code, r.json().get("error_code"), code)
    wrong = {k: v for k, v in results.items() if v[0] not in (401, 403) or v[1] != v[2]}
    assert not wrong, wrong


def test_an_assertion_is_good_once(embed):
    embed.attribute("member", "E-7")
    host = embed.create()
    assertion = sign(host["private_key"], aud=host["public_id"], sub="E-7")
    assert embed.session(host, assertion=assertion).status_code == 200
    again = embed.session(host, assertion=assertion)
    assert again.status_code == 401 and again.json()["error_code"] == "embed.assertion_replayed"


def test_a_subject_two_members_share_signs_in_no_one(embed):
    embed.attribute("member", "E-dup")
    embed.attribute("direct_share", "E-dup")
    host = embed.create()
    r = embed.session(host, assertion=sign(host["private_key"], aud=host["public_id"], sub="E-dup"))
    assert r.status_code in (401, 403, 409) and r.json()["error_code"] == "embed.identity_ambiguous"


def test_the_credential_must_match_the_identity_mode(embed):
    embed.attribute("member", "E-3")
    host = embed.create()
    r = embed.session(host, id_token="eyJ.x.y")
    assert r.status_code == 401 and r.json()["error_code"] == "embed.assertion_invalid"
    both = embed.session(host, assertion=sign(host["private_key"], aud=host["public_id"], sub="E-3"),
                         sso_code="abc")
    assert both.status_code == 401


def test_a_disabled_or_deleted_host_signs_in_no_one(embed):
    embed.attribute("member", "E-4")
    host = embed.create()
    off = embed.client.patch(f"/api/host_applications/{host['id']}",
                             json={"revision": host["revision"], "enabled": False}, headers=embed.admin)
    assert off.status_code == 200
    r = embed.session(host, assertion=sign(host["private_key"], aud=host["public_id"], sub="E-4"))
    assert r.status_code == 403 and r.json()["error_code"] == "host_app.disabled"
    assert embed.client.delete(f"/api/host_applications/{host['id']}", headers=embed.admin).status_code == 204
    gone = embed.session(host, assertion=sign(host["private_key"], aud=host["public_id"], sub="E-4"))
    assert gone.status_code == 404


def test_scim_external_id_is_an_identity_source(embed, corpus, corpus_rows):
    corpus_rows.update("users", corpus.cast.id("member"), scim_external_id="00u-okta-77")
    host = embed.create(identity_source="scim")
    assert embed.session(host, assertion=sign(host["private_key"], aud=host["public_id"],
                                              sub="00u-okta-77")).status_code == 200


# ── what a session opens ──────────────────────────────────────────────────

def test_a_session_opens_only_artifacts_that_allow_its_host(embed, published, corpus, corpus_rows):
    embed.attribute("member", "E-10")
    host = embed.create()
    token = embed.sign_in(host, "E-10")
    # Not allowed yet: refused, though the member could open it in BOW.
    before = embed.client.get(f"/api/d/{published}", headers=bearer(token))
    assert before.status_code == 403 and before.json()["error_code"] == "embed.not_allowed"
    assert embed.allow(published, host).status_code == 200
    assert embed.client.get(f"/api/d/{published}", headers=bearer(token)).status_code == 200
    assert embed.client.get(f"/api/d/{published}/content", headers=bearer(token)).status_code == 200
    assert embed.client.get(f"/api/artifacts/{published}/runtime/resources",
                            headers=bearer(token)).status_code == 200
    # Another artifact, not allowed for this host.
    other = own(corpus_rows, corpus, OTHER_DASH)
    publish(other)
    share(embed.client, corpus, other, "internal")
    assert embed.client.get(f"/api/d/{other}", headers=bearer(token)).status_code == 403


@pytest.mark.parametrize("path", [
    "/api/users/whoami", "/api/reports", "/api/host_applications", "/api/organizations",
    "/api/data_sources",
])
def test_a_session_is_refused_by_the_rest_of_the_api(embed, published, path):
    embed.attribute("member", "E-11")
    host = embed.create()
    embed.allow(published, host)
    token = embed.sign_in(host, "E-11")
    headers = {**bearer(token), "X-Organization-Id": embed.corpus.cast.org_id}
    assert embed.client.get(path, headers=headers).status_code == 401


def test_embedding_never_widens_who_may_see_an_artifact(embed, published):
    embed.attribute("member", "E-12")
    host = embed.create()
    embed.allow(published, host)
    token = embed.sign_in(host, "E-12")
    assert share(embed.client, embed.corpus, published, "none").status_code == 200
    assert embed.client.get(f"/api/d/{published}", headers=bearer(token)).status_code == 404
    assert share(embed.client, embed.corpus, published, "shared", users=["direct_share"]).status_code == 200
    assert embed.client.get(f"/api/d/{published}", headers=bearer(token)).status_code == 403


@pytest.mark.parametrize("ending", ["logout", "rotate_key", "revoke_sessions", "disable", "change_source",
                                    "remove_member"])
def test_a_session_stops_working_when_it_should(embed, published, ending, corpus):
    embed.attribute("member", "E-20")
    host = embed.create()
    embed.allow(published, host)
    token = embed.sign_in(host, "E-20")
    assert embed.client.get(f"/api/d/{published}", headers=bearer(token)).status_code == 200
    hid, rev = host["id"], host["revision"]
    if ending == "logout":
        assert embed.client.delete(f"/api/embed/{host['public_id']}/session",
                                   headers=bearer(token)).status_code == 204
    elif ending in ("rotate_key", "revoke_sessions"):
        assert embed.client.post(f"/api/host_applications/{hid}/{ending}", headers=embed.admin).status_code == 200
    elif ending == "disable":
        embed.client.patch(f"/api/host_applications/{hid}", json={"revision": rev, "enabled": False},
                           headers=embed.admin)
    elif ending == "change_source":
        embed.client.patch(f"/api/host_applications/{hid}", json={"revision": rev, "identity_source": "scim"},
                           headers=embed.admin)
    elif ending == "remove_member":
        membership_id = corpus.cast.people["member"].membership_id
        r = embed.client.delete(f"/api/organizations/{corpus.cast.org_id}/members/{membership_id}",
                                headers=embed.admin)
        assert r.status_code in (200, 204), r.text
    r = embed.client.get(f"/api/d/{published}", headers=bearer(token))
    assert r.status_code == 401 and r.json()["error_code"] == "embed.session_invalid"


def test_a_session_from_one_host_is_not_another_hosts(embed, published):
    embed.attribute("member", "E-21")
    erp = embed.create()
    portal = embed.create(name="Portal", issuer="portal")
    embed.allow(published, portal)
    token = embed.sign_in(erp, "E-21")
    assert embed.client.get(f"/api/d/{published}", headers=bearer(token)).status_code == 403
    assert embed.client.get(f"/api/embed/{portal['public_id']}/session", headers=bearer(token)).status_code == 401


# ── the artifact's Embed tab ──────────────────────────────────────────────

def test_the_embed_tab_needs_edit_rights_on_the_artifact(embed, published):
    host = embed.create()
    for person in ("member", "direct_share", "anonymous"):
        r = embed.allow(published, host, by=person)
        assert r.status_code in (401, 403, 404), (person, r.status_code)
    r = embed.allow(published, host)
    assert r.status_code == 200, r.text
    assert [h["allowed"] for h in r.json()["hosts"] if h["id"] == host["id"]] == [True]
    assert r.json()["can_manage_hosts"] is False
    # The organization's admin reads others' dashboards but does not edit them
    # (the rest of the Settings window too); on their own, they may also add hosts.
    assert embed.client.get(f"/api/artifacts/{published}/embedding", headers=embed.admin).status_code in (403, 404)


def test_an_admin_editing_their_own_artifact_can_add_hosts_from_it(embed, corpus, corpus_rows):
    aid = own(corpus_rows, corpus, OTHER_DASH, owner="admin")
    got = embed.client.get(f"/api/artifacts/{aid}/embedding", headers=embed.admin)
    assert got.status_code == 200 and got.json()["can_manage_hosts"] is True


def test_the_embed_tab_offers_only_this_organizations_hosts(embed, published, allow_other_org_host):
    foreign = allow_other_org_host
    r = embed.client.put(f"/api/artifacts/{published}/embedding", json={"host_application_ids": [foreign]},
                         headers=as_(embed.corpus, "owner"))
    assert r.status_code == 400 and r.json()["error_code"] == "host_app.not_found"


@pytest.fixture
def allow_other_org_host(test_client, corpus):
    r = test_client.post("/api/host_applications", json={
        "name": "Elsewhere", "allowed_origins": [ERP], "identity_mode": "assertion",
        "identity_source": "member_attribute:employee_id", "issuer": "x"},
        headers=corpus.cast.people["other_org"].headers(corpus.cast.other_org_id))
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_the_frame_policy_names_the_hosts_origins_only_when_allowed(embed, published):
    host = embed.create(allowed_origins=[ERP, "https://erp-test.example.com"])
    path = f"/api/embed/{host['public_id']}/frame?artifact={published}"
    assert embed.client.get(path).json()["frame_ancestors"] == "'self'"
    embed.allow(published, host)
    assert embed.client.get(path).json()["frame_ancestors"] == f"'self' {ERP} https://erp-test.example.com"
    # Unknown host, or an artifact that does not exist: this origin only.
    assert embed.client.get(f"/api/embed/host_0000000000/frame?artifact={published}").json()["frame_ancestors"] == "'self'"
    assert embed.client.get(f"/api/embed/{host['public_id']}/frame?artifact=no-such").json()["frame_ancestors"] == "'self'"


# ── SSO mode ──────────────────────────────────────────────────────────────

def _pkce():
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def test_sso_mode_hands_a_signed_in_member_a_one_time_pkce_code(embed, published):
    host = embed.create(identity_mode="sso", identity_source=None, issuer=None)
    embed.allow(published, host)
    verifier, challenge = _pkce()
    code = embed.client.post(f"/api/embed/{host['public_id']}/sso/code", json={"code_challenge": challenge},
                             headers=as_(embed.corpus, "member"))
    assert code.status_code == 200, code.text
    wrong = embed.session(host, sso_code=code.json()["code"], code_verifier="x" * 50)
    assert wrong.status_code == 401
    # The failed attempt spent the code.
    spent = embed.session(host, sso_code=code.json()["code"], code_verifier=verifier)
    assert spent.status_code == 401

    verifier2, challenge2 = _pkce()
    code2 = embed.client.post(f"/api/embed/{host['public_id']}/sso/code", json={"code_challenge": challenge2},
                              headers=as_(embed.corpus, "member")).json()["code"]
    ok = embed.session(host, sso_code=code2, code_verifier=verifier2)
    assert ok.status_code == 200 and ok.json()["user"]["id"] == embed.corpus.cast.id("member")
    assert embed.client.get(f"/api/d/{published}", headers=bearer(ok.json()["token"])).status_code == 200
    assert embed.session(host, sso_code=code2, code_verifier=verifier2).status_code == 401


def test_sso_codes_go_only_to_members_of_the_hosts_organization(embed):
    host = embed.create(identity_mode="sso", identity_source=None, issuer=None)
    _, challenge = _pkce()
    for person, codes in (("other_org", {403}), ("anonymous", {401})):
        r = embed.client.post(f"/api/embed/{host['public_id']}/sso/code", json={"code_challenge": challenge},
                              headers=as_(embed.corpus, person))
        assert r.status_code in codes, (person, r.status_code, r.text)
