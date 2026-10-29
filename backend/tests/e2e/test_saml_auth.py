"""SAML public contracts with real signatures, sessions and organization admission."""

import base64
import zlib
import uuid
from urllib.parse import urlsplit, parse_qs
from datetime import datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from defusedxml.ElementTree import fromstring
from sqlalchemy import update

from app.routes.auth import router
from app.errors.handlers import register_exception_handlers
from app.settings.config import settings
from app.settings.bow_config import SAMLProvider
from tests.mocks.saml_idp import key_pair, response_xml

pytestmark = pytest.mark.e2e


@pytest.fixture
def saml_env(monkeypatch, create_user, login_user, whoami):
    monkeypatch.setattr(settings.bow_config.features, "allow_uninvited_signups", True)
    create_user()
    token = login_user()
    org = whoami(token)["organizations"][0]["id"]
    key, cert = key_pair()
    cfg = SAMLProvider(
        name="test-idp",
        enabled=True,
        organization_id=org,
        auto_provision_users=True,
        idp={
            "entity_id": "https://idp.example.test",
            "sso_url": "https://idp.example.test/login",
            "certificates": [cert],
        },
        attributes={"email": "mail", "name": "display"},
    )
    monkeypatch.setattr(settings.bow_config, "saml_providers", [cfg])
    monkeypatch.setattr(settings.bow_config, "base_url", "https://localhost:3000")
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(router, prefix="/api")
    with TestClient(app, base_url="https://localhost:3000", follow_redirects=False) as client:
        yield client, cfg, key, cert


def login_form(env, **kwargs):
    client, cfg, key, cert = env
    resp = client.get(f"/api/auth/saml/{cfg.name}/authorize")
    assert resp.status_code == 200, resp.text
    params = parse_qs(urlsplit(resp.json()["authorization_url"]).query)
    req = fromstring(zlib.decompress(base64.b64decode(params["SAMLRequest"][0]), -15))
    assertion = response_xml(
        req.get("ID"),
        req.get("AssertionConsumerServiceURL"),
        req.find("{urn:oasis:names:tc:SAML:2.0:assertion}Issuer").text,
        cfg.idp.entity_id,
        key,
        cert,
        **kwargs,
    )
    return {"SAMLResponse": assertion, "RelayState": params["RelayState"][0]}


def submit(env, form):
    return env[0].post(f"/api/auth/saml/{env[1].name}/acs", data=form)


def redeem(env, resp):
    assert resp.status_code == 303
    query = parse_qs(urlsplit(resp.headers["location"]).query)
    assert "login_code" in query, resp.headers["location"]
    result = env[0].post("/api/auth/exchange", json={"login_code": query["login_code"][0]})
    assert result.status_code == 200
    return result.json()["access_token"]


def test_saml_metadata_endpoint(saml_env):
    client, cfg, *_ = saml_env
    response = client.get("/api/auth/saml/test-idp/metadata")
    assert response.status_code == 200
    root = fromstring(response.content)
    assert root.get("entityID") == "https://localhost:3000/saml/test-idp"
    assert "https://localhost:3000/api/auth/saml/test-idp/acs" in response.text
    assert "PRIVATE KEY" not in response.text


@pytest.mark.parametrize("signature", ["assertion", "response", "both"])
def test_trusted_signatures_create_member_session(saml_env, test_client, signature):
    resp = submit(saml_env, login_form(saml_env, signature=signature))
    token = redeem(saml_env, resp)
    who = test_client.get("/api/users/whoami", headers={"Authorization": f"Bearer {token}"})
    assert who.status_code == 200, who.text
    assert who.json()["email"] == "person@example.com"
    orgs = test_client.get("/api/organizations", headers={"Authorization": f"Bearer {token}"})
    assert orgs.status_code == 200
    assert {o["id"] for o in orgs.json()} == {saml_env[1].organization_id}
    assert "access_token=" not in resp.headers["location"]
    code = parse_qs(urlsplit(resp.headers["location"]).query)["login_code"][0]
    assert saml_env[0].post("/api/auth/exchange", json={"login_code": code}).status_code == 400


@pytest.mark.parametrize(
    "variant",
    [
        "expired",
        "tampered",
        "wrong_issuer",
        "wrong_audience",
        "wrong_destination",
        "wrong_request",
        "missing_correlation",
        "wrapping",
        "entity",
    ],
)
def test_invalid_assertions_never_issue_session(saml_env, variant):
    resp = submit(saml_env, login_form(saml_env, variant=variant))
    assert "error_code=saml_login_failed" in resp.headers["location"]


def test_unsigned_and_untrusted_keys_rejected(saml_env):
    resp = submit(saml_env, login_form(saml_env, signature="none"))
    assert "login_code=" not in resp.headers["location"]
    badkey, badcert = key_pair()
    other = (*saml_env[:2], badkey, badcert)
    resp = submit(saml_env, login_form(other))
    assert "login_code=" not in resp.headers["location"]


@pytest.mark.parametrize("damage", ["cookie", "relay", "duplicate", "unsolicited"])
def test_browser_binding_is_required(saml_env, damage):
    form = login_form(saml_env)
    if damage == "cookie":
        saml_env[0].cookies.clear()
    if damage == "relay":
        form["RelayState"] = "other-browser"
    if damage == "unsolicited":
        form.pop("RelayState")
    if damage == "duplicate":
        form["SAMLResponse"] = [form["SAMLResponse"], form["SAMLResponse"]]
    resp = submit(saml_env, form)
    assert "login_code=" not in resp.headers["location"]


def test_response_cannot_be_replayed_even_with_browser_cookie(saml_env):
    form = login_form(saml_env)
    cookies = dict(saml_env[0].cookies)
    redeem(saml_env, submit(saml_env, form))
    saml_env[0].cookies.update(cookies)
    assert "login_code=" not in submit(saml_env, form).headers["location"]


@pytest.mark.parametrize("attrs", [{}, {"mail": ["a@example.com", "b@example.com"]}, {"mail": "not-email"}])
def test_missing_or_ambiguous_email_rejected(saml_env, attrs):
    assert "login_code=" not in submit(saml_env, login_form(saml_env, attributes=attrs)).headers["location"]


def test_existing_email_does_not_implicitly_link(saml_env, create_user):
    create_user(email="existing@example.com")
    assert "login_code=" not in submit(saml_env, login_form(saml_env, email="existing@example.com")).headers["location"]


def test_jit_disabled_requires_invite(saml_env):
    saml_env[1].auto_provision_users = False
    assert "login_code=" not in submit(saml_env, login_form(saml_env)).headers["location"]


def test_provider_disable_revokes_saml_session(saml_env, test_client):
    token = redeem(saml_env, submit(saml_env, login_form(saml_env)))
    saml_env[1].enabled = False
    assert saml_env[0].get("/api/auth/saml/test-idp/authorize").status_code == 404
    assert test_client.get("/api/users/whoami", headers={"Authorization": f"Bearer {token}"}).status_code == 401


def test_claim_mapping_and_provider_names_are_generic(saml_env, test_client):
    cfg = saml_env[1]
    cfg.name = "corporate-keycloak"
    cfg.attributes.email = "urn:company:email"
    cfg.attributes.subject = "employee-id"
    cfg.attributes.name = "full-name"
    token = redeem(
        saml_env,
        submit(
            saml_env,
            login_form(
                saml_env,
                attributes={
                    "urn:company:email": "employee@example.com",
                    "employee-id": "immutable-56",
                    "full-name": "Employee Name",
                },
            ),
        ),
    )
    who = test_client.get("/api/users/whoami", headers={"Authorization": f"Bearer {token}"})
    assert who.status_code == 200
    assert who.json()["name"] == "Employee Name"


async def mutate_database(statement):
    from app.dependencies import async_session_maker

    async with async_session_maker() as db:
        await db.execute(statement)
        await db.commit()


@pytest.mark.asyncio
async def test_expired_request_is_rejected(saml_env):
    from app.models.saml import SAMLRequest

    form = login_form(saml_env)
    # Expiry is a clock boundary; no public API backdates a pending request.
    await mutate_database(update(SAMLRequest).values(expires_at=datetime.utcnow() - timedelta(seconds=1)))
    assert "login_code=" not in submit(saml_env, form).headers["location"]


@pytest.mark.asyncio
@pytest.mark.parametrize("revoke", ["membership", "user", "identity"])
async def test_revocation_blocks_relogin_and_existing_session(saml_env, test_client, revoke):
    from app.models.membership import Membership
    from app.models.user import User
    from app.models.saml import SAMLIdentity

    token = redeem(saml_env, submit(saml_env, login_form(saml_env)))
    user = test_client.get("/api/users/whoami", headers={"Authorization": f"Bearer {token}"}).json()
    # Fixture-only lifecycle states also produced by admin/SCIM operations.
    if revoke == "membership":
        stmt = update(Membership).where(Membership.user_id == user["id"]).values(deleted_at=datetime.utcnow())
    elif revoke == "user":
        stmt = update(User).where(User.id == user["id"]).values(is_active=False)
    else:
        stmt = update(SAMLIdentity).where(SAMLIdentity.user_id == user["id"]).values(deleted_at=datetime.utcnow())
    await mutate_database(stmt)
    assert test_client.get("/api/users/whoami", headers={"Authorization": f"Bearer {token}"}).status_code in (401, 403)
    assert "login_code=" not in submit(saml_env, login_form(saml_env)).headers["location"]


def test_configuration_change_invalidates_inflight_request(saml_env):
    form = login_form(saml_env)
    saml_env[1].organization_id = str(uuid.uuid4())
    assert "login_code=" not in submit(saml_env, form).headers["location"]


def test_provider_cannot_claim_another_providers_identity(saml_env, monkeypatch):
    redeem(saml_env, submit(saml_env, login_form(saml_env)))
    other = saml_env[1].model_copy(deep=True)
    other.name = "other-idp"
    monkeypatch.setattr(settings.bow_config, "saml_providers", [saml_env[1], other])
    second = (saml_env[0], other, *saml_env[2:])
    assert "login_code=" not in submit(second, login_form(second)).headers["location"]


def test_invitation_admits_member_without_jit(saml_env, test_client, login_user):
    cfg = saml_env[1]
    cfg.auto_provision_users = False
    headers = {"Authorization": "Bearer " + login_user(), "X-Organization-Id": cfg.organization_id}
    invite = test_client.post(
        f"/api/organizations/{cfg.organization_id}/members",
        headers=headers,
        json={"organization_id": cfg.organization_id, "email": "person@example.com", "role": "member"},
    )
    assert invite.status_code == 200, invite.text
    token = redeem(saml_env, submit(saml_env, login_form(saml_env)))
    who = test_client.get("/api/users/whoami", headers={"Authorization": "Bearer " + token})
    assert who.status_code == 200
    assert who.json()["organizations"][0]["role"] == "member"


def test_approved_account_link_requires_membership(saml_env, test_client, create_user):
    create_user(email="linked@example.com")
    saml_env[1].account_links = {"subject-123": "linked@example.com"}
    # A link approves identity, but it must not grant organization membership.
    assert "login_code=" not in submit(saml_env, login_form(saml_env, email="linked@example.com")).headers["location"]


def test_signing_certificate_rollover_accepts_both_trusted_keys(saml_env):
    key, cert = key_pair()
    saml_env[1].idp.certificates.append(cert)
    for k, c in [saml_env[2:], (key, cert)]:
        env = (*saml_env[:2], k, c)
        assert redeem(env, submit(env, login_form(env)))


def test_public_settings_do_not_expose_trust_material(saml_env, test_client):
    response = test_client.get("/api/settings")
    provider = response.json()["saml_providers"][0]
    assert set(provider) == {"name", "enabled", "label", "brand", "protocol"}
    assert "BEGIN CERTIFICATE" not in response.text
    assert "idp.example.test" not in response.text


def test_cross_provider_callback_rejected(saml_env, monkeypatch):
    form = login_form(saml_env)
    other = saml_env[1].model_copy(deep=True)
    other.name = "other-provider"
    monkeypatch.setattr(settings.bow_config, "saml_providers", [saml_env[1], other])
    response = saml_env[0].post("/api/auth/saml/other-provider/acs", data=form)
    assert "login_code=" not in response.headers["location"]


def test_encrypted_signed_assertion(saml_env, tmp_path):
    from tests.mocks.saml_idp import encrypt_assertion

    key, cert = key_pair()
    (tmp_path / "sp.key").write_text(key)
    (tmp_path / "sp.crt").write_text(cert)
    cfg = saml_env[1]
    cfg.sp.private_key_file = str(tmp_path / "sp.key")
    cfg.sp.certificate_file = str(tmp_path / "sp.crt")
    cfg.sp.want_assertions_encrypted = True
    form = login_form(saml_env)
    form["SAMLResponse"] = encrypt_assertion(form["SAMLResponse"], cert)
    assert redeem(saml_env, submit(saml_env, form))


def test_signed_authentication_request(saml_env, tmp_path):
    from onelogin.saml2.utils import OneLogin_Saml2_Utils

    key, cert = key_pair()
    (tmp_path / "sp.key").write_text(key)
    (tmp_path / "sp.crt").write_text(cert)
    cfg = saml_env[1]
    cfg.sp.private_key_file = str(tmp_path / "sp.key")
    cfg.sp.certificate_file = str(tmp_path / "sp.crt")
    cfg.sp.sign_requests = True
    response = saml_env[0].get("/api/auth/saml/test-idp/authorize")
    assert response.status_code == 200
    query = urlsplit(response.json()["authorization_url"]).query
    params = parse_qs(query)
    signed = "&".join(part for part in query.split("&") if not part.startswith("Signature="))
    assert OneLogin_Saml2_Utils.validate_binary_sign(
        signed, base64.b64decode(params["Signature"][0]), cert, params["SigAlg"][0]
    )


@pytest.mark.asyncio
async def test_removed_membership_cannot_be_recreated_by_jit(saml_env):
    from app.models.membership import Membership
    from sqlalchemy import delete

    redeem(saml_env, submit(saml_env, login_form(saml_env)))
    # Membership deletion is intentionally independent of the federated identity.
    await mutate_database(delete(Membership).where(Membership.directory_provider == "saml:test-idp"))
    assert "login_code=" not in submit(saml_env, login_form(saml_env)).headers["location"]


def test_deprecated_signature_algorithm_rejected(saml_env):
    assert "login_code=" not in submit(saml_env, login_form(saml_env, signature="legacy")).headers["location"]


def test_explicit_link_admits_existing_member(saml_env, test_client, create_user, login_user, whoami):
    member = create_user(email="linked-member@example.com")
    member_id = whoami(login_user(member["email"], member["password"]))["id"]
    cfg = saml_env[1]
    response = test_client.post(
        f"/api/organizations/{cfg.organization_id}/members",
        headers={"Authorization": "Bearer " + login_user(), "X-Organization-Id": cfg.organization_id},
        json={"organization_id": cfg.organization_id, "user_id": member_id, "role": "member"},
    )
    assert response.status_code == 200, response.text
    cfg.account_links = {"subject-123": member["email"]}
    token = redeem(saml_env, submit(saml_env, login_form(saml_env, email=member["email"])))
    assert whoami(token)["id"] == member_id


def test_plaintext_assertion_rejected_when_encryption_required(saml_env, tmp_path):
    key, cert = key_pair()
    (tmp_path / "sp.key").write_text(key)
    (tmp_path / "sp.crt").write_text(cert)
    cfg = saml_env[1]
    cfg.sp.private_key_file = str(tmp_path / "sp.key")
    cfg.sp.certificate_file = str(tmp_path / "sp.crt")
    cfg.sp.want_assertions_encrypted = True
    assert "login_code=" not in submit(saml_env, login_form(saml_env)).headers["location"]


@pytest.mark.parametrize("variant", ["valid", "expired", "ambiguous", "post-only"])
def test_metadata_file_trust_and_selection(saml_env, tmp_path, variant):
    from app.settings.bow_config import SAMLIdP

    cfg = saml_env[1]
    certificate = "".join(saml_env[3].splitlines()[1:-1])
    binding = "HTTP-POST" if variant == "post-only" else "HTTP-Redirect"
    expiry = ' validUntil="2000-01-01T00:00:00Z"' if variant == "expired" else ""
    document = f'''<md:EntityDescriptor xmlns:md="urn:oasis:names:tc:SAML:2.0:metadata"
        xmlns:ds="http://www.w3.org/2000/09/xmldsig#" entityID="https://idp.example.test"{expiry}>
        <md:IDPSSODescriptor protocolSupportEnumeration="urn:oasis:names:tc:SAML:2.0:protocol">
        <md:KeyDescriptor use="signing"><ds:KeyInfo><ds:X509Data><ds:X509Certificate>{certificate}</ds:X509Certificate></ds:X509Data></ds:KeyInfo></md:KeyDescriptor>
        <md:SingleSignOnService Binding="urn:oasis:names:tc:SAML:2.0:bindings:{binding}" Location="https://idp.example.test/login"/>
        </md:IDPSSODescriptor></md:EntityDescriptor>'''
    if variant == "ambiguous":
        document = (
            '<md:EntitiesDescriptor xmlns:md="urn:oasis:names:tc:SAML:2.0:metadata">'
            + document
            + document.replace('entityID="https://idp.example.test"', 'entityID="https://other.example.test"')
            + "</md:EntitiesDescriptor>"
        )
    path = tmp_path / "metadata.xml"
    path.write_text(document)
    cfg.idp = SAMLIdP(metadata_file=str(path))
    response = saml_env[0].get("/api/auth/saml/test-idp/metadata")
    assert response.status_code == (200 if variant == "valid" else 400)
    if variant == "ambiguous":
        cfg.idp.entity_id = "https://idp.example.test"
        assert saml_env[0].get("/api/auth/saml/test-idp/metadata").status_code == 200
    if variant in ("valid", "ambiguous"):
        # The explicit issuer is supplied only to the independent IdP fixture;
        # the application still obtains all trust material from the file.
        cfg.idp.entity_id = "https://idp.example.test"
        assert redeem(saml_env, submit(saml_env, login_form(saml_env)))
