"""A provider sign-in is tied to an existing account by email only when the
provider is trusted for that email.

Contract:
* The deployment's single sign-in provider links a first sign-in to the
  existing account with the same email (pre-created or local users moving to
  SSO keep working).
* A single-organization deployment trusts every provider it configured,
  unless the provider is explicitly set not to be trusted.
* When several organizations share the instance: with more than one provider
  enabled, with a token that marks the email unverified, or with an issuer
  that accepts accounts from any tenant, the sign-in is refused with a fixed code, the existing account is untouched and
  no second account appears for the address.
* ``trust_email_for_linking`` on the provider overrides the automatic rule in
  both directions.
* People without an existing account are admitted by a trusted provider
  exactly as before. An untrusted provider cannot create the account an invite
  or a domain policy is addressed to; where anyone may sign up it creates an
  account like the sign-up form would, without verifying the address.
"""
import pytest

from app.settings.config import settings as bow_settings
from tests.e2e.org_isolation.identity_helpers import (
    FakeIdentityProvider, bootstrap_admin, enable_domain_signup, feature, hdr, invite, login, oidc_provider, org_ids, register,
    unique_email, whoami,
)

REFUSED = ("error", "account_link_required")


@pytest.fixture
def multi_org():
    """Several organizations share the instance: the automatic rule is strict."""
    with feature("allow_multiple_organizations", True):
        yield


def _existing_member(client, admin):
    email = unique_email("member")
    _, token = invite(client, admin["token"], admin["org_id"], email)
    assert register(client, email, invite_token=token).status_code == 201
    return {"email": email, "id": whoami(client, login(client, email).json()["access_token"])["id"]}


@pytest.mark.parametrize("who", ["admin", "member"])
def test_single_provider_links_first_sign_in_to_the_existing_account(test_client, monkeypatch, multi_org, who):
    admin = bootstrap_admin(test_client)
    account = admin if who == "admin" else _existing_member(test_client, admin)
    idp = FakeIdentityProvider(monkeypatch, [oidc_provider()])

    outcome, session = idp.sign_in(test_client, "corp", account["email"])

    assert outcome == "ok"
    assert whoami(test_client, session)["id"] == account["id"]
    assert admin["org_id"] in org_ids(test_client, session)
    # The link is now by provider subject: a later sign-in is the same account.
    again, session2 = idp.sign_in(test_client, "corp", account["email"])
    assert again == "ok" and whoami(test_client, session2)["id"] == account["id"]


def test_token_without_a_verification_claim_is_accepted_from_the_single_provider(test_client, monkeypatch, multi_org):
    admin = bootstrap_admin(test_client)
    idp = FakeIdentityProvider(monkeypatch, [oidc_provider()])
    outcome, session = idp.sign_in(test_client, "corp", admin["email"], email_verified=True, xms_edov=True)
    assert outcome == "ok" and whoami(test_client, session)["id"] == admin["id"]


def _assert_refused_and_untouched(client, idp, provider, account, **claims):
    assert idp.sign_in(client, provider, account["email"], **claims) == REFUSED
    # The account still signs in the way it did, and is still the only one for the address.
    session = login(client, account["email"])
    assert session.status_code == 200, session.text
    assert whoami(client, session.json()["access_token"])["id"] == account["id"]


def test_second_enabled_provider_does_not_link_by_email(test_client, monkeypatch, multi_org):
    admin = bootstrap_admin(test_client)
    member = _existing_member(test_client, admin)
    idp = FakeIdentityProvider(monkeypatch, [oidc_provider("corp"), oidc_provider("partner")])

    for account in (admin, member):
        for provider in ("corp", "partner"):
            _assert_refused_and_untouched(test_client, idp, provider, account)

    # Back to one provider: the address was not taken by a second account in
    # the meantime, so the sign-in resolves to the original one.
    monkeypatch.setattr(bow_settings.bow_config, "oidc_providers", [oidc_provider("corp")])
    outcome, session = idp.sign_in(test_client, "corp", member["email"])
    assert outcome == "ok" and whoami(test_client, session)["id"] == member["id"]


def test_google_sign_in_counts_as_a_second_provider(test_client, monkeypatch, multi_org):
    admin = bootstrap_admin(test_client)
    idp = FakeIdentityProvider(monkeypatch, [oidc_provider()])
    monkeypatch.setattr(bow_settings.bow_config.google_oauth, "enabled", True)
    _assert_refused_and_untouched(test_client, idp, "corp", admin)


@pytest.mark.parametrize("claims", [
    {"email_verified": False},
    {"email_verified": "false"},
    {"xms_edov": False},
    {"email_verified": True, "xms_edov": False},
])
def test_email_the_provider_marks_unverified_does_not_link(test_client, monkeypatch, multi_org, claims):
    admin = bootstrap_admin(test_client)
    idp = FakeIdentityProvider(monkeypatch, [oidc_provider()])
    _assert_refused_and_untouched(test_client, idp, "corp", admin, **claims)


@pytest.mark.parametrize("tenant", ["common", "organizations", "consumers"])
def test_issuer_open_to_any_tenant_does_not_link(test_client, monkeypatch, multi_org, tenant):
    admin = bootstrap_admin(test_client)
    issuer = f"https://login.microsoftonline.com/{tenant}/v2.0"
    idp = FakeIdentityProvider(monkeypatch, [oidc_provider(issuer=issuer)])
    _assert_refused_and_untouched(test_client, idp, "corp", admin)


def test_trust_setting_restores_linking_for_a_provider(test_client, monkeypatch, multi_org):
    admin = bootstrap_admin(test_client)
    idp = FakeIdentityProvider(monkeypatch, [
        oidc_provider("corp", trust_email_for_linking=True), oidc_provider("partner"),
    ])
    # Only the provider that was marked trusted links, even for a token that
    # would otherwise be refused.
    assert idp.sign_in(test_client, "partner", admin["email"]) == REFUSED
    outcome, session = idp.sign_in(test_client, "corp", admin["email"], email_verified=False)
    assert outcome == "ok" and whoami(test_client, session)["id"] == admin["id"]


def test_trust_setting_can_switch_linking_off_for_a_single_provider(test_client, monkeypatch):
    admin = bootstrap_admin(test_client)
    idp = FakeIdentityProvider(monkeypatch, [oidc_provider(trust_email_for_linking=False)])
    _assert_refused_and_untouched(test_client, idp, "corp", admin)


SINGLE_ORG_SETUPS = {
    "two_providers": dict(providers=lambda: [oidc_provider("corp"), oidc_provider("partner")], claims={}),
    "unverified_email": dict(providers=lambda: [oidc_provider()], claims={"email_verified": False}),
    "multi_tenant_issuer": dict(
        providers=lambda: [oidc_provider(issuer="https://login.microsoftonline.com/common/v2.0")], claims={}),
}


@pytest.mark.parametrize("setup", sorted(SINGLE_ORG_SETUPS))
def test_single_organization_deployment_links_and_admits_as_it_always_has(test_client, monkeypatch, setup):
    """One organization: every configured provider is the operator's own
    choice for it, so existing accounts link and invited people join."""
    cfg = SINGLE_ORG_SETUPS[setup]
    admin = bootstrap_admin(test_client)
    member = _existing_member(test_client, admin)
    newcomer = unique_email("newcomer")
    invite(test_client, admin["token"], admin["org_id"], newcomer)
    idp = FakeIdentityProvider(monkeypatch, cfg["providers"]())

    for account in (admin, member):
        outcome, session = idp.sign_in(test_client, "corp", account["email"], **cfg["claims"])
        assert outcome == "ok" and whoami(test_client, session)["id"] == account["id"]
    outcome, session = idp.sign_in(test_client, "corp", newcomer, **cfg["claims"])
    assert outcome == "ok" and org_ids(test_client, session) == {admin["org_id"]}


def test_invited_person_without_an_account_signs_in_through_the_single_provider_and_joins(test_client, monkeypatch):
    admin = bootstrap_admin(test_client)
    email = unique_email("newcomer")
    invite(test_client, admin["token"], admin["org_id"], email)
    idp = FakeIdentityProvider(monkeypatch, [oidc_provider()])

    outcome, session = idp.sign_in(test_client, "corp", email)

    assert outcome == "ok"
    me = whoami(test_client, session)
    assert me["email"] == email and me["is_verified"] is True
    assert org_ids(test_client, session) == {admin["org_id"]}


@pytest.mark.parametrize("admitted_by", ["invite", "domain_policy"])
def test_untrusted_provider_cannot_create_the_account_an_invite_is_waiting_for(test_client, monkeypatch, admitted_by):
    """An email an untrusted provider asserts is not the invited person: no
    account is created under it, and the invite still works for its owner."""
    admin = bootstrap_admin(test_client)
    email = unique_email("newcomer")
    token = None
    if admitted_by == "invite":
        _, token = invite(test_client, admin["token"], admin["org_id"], email)
    else:
        enable_domain_signup(test_client, monkeypatch, admin, ["test.com"])
    idp = FakeIdentityProvider(monkeypatch, [oidc_provider(trust_email_for_linking=False)])

    for _ in range(2):
        assert idp.sign_in(test_client, "corp", email) == ("error", "invite_link_required")

    # The address is still free: its owner registers (with the link when invited) and joins.
    registered = register(test_client, email, invite_token=token)
    assert registered.status_code == 201, registered.text
    session = login(test_client, email).json()["access_token"]
    assert org_ids(test_client, session) == {admin["org_id"]}


def test_untrusted_provider_open_sign_up_creates_an_unverified_account(test_client, monkeypatch, multi_org):
    """Where anyone may sign up and nothing is addressed to the email, an
    untrusted provider is as good as the sign-up form — and no better."""
    bootstrap_admin(test_client)
    idp = FakeIdentityProvider(monkeypatch, [oidc_provider("corp"), oidc_provider("partner")])
    email = unique_email("walk_in")

    with feature("allow_uninvited_signups", True), feature("verify_emails", True):
        outcome, session = idp.sign_in(test_client, "corp", email)
        assert outcome == "ok"
        me = whoami(test_client, session)
        assert me["email"] == email and me["is_verified"] is False
        assert org_ids(test_client, session) == set()

        # A trusted provider's word does verify the address.
        monkeypatch.setattr(bow_settings.bow_config, "oidc_providers", [oidc_provider("corp")])
        outcome, trusted_session = idp.sign_in(test_client, "corp", unique_email("walk_in"))
        assert outcome == "ok" and whoami(test_client, trusted_session)["is_verified"] is True


def test_uninvited_person_is_still_asked_for_an_invite(test_client, monkeypatch):
    bootstrap_admin(test_client)
    idp = FakeIdentityProvider(monkeypatch, [oidc_provider()])
    assert idp.sign_in(test_client, "corp", unique_email("stranger")) == ("error", "invitation_required")
