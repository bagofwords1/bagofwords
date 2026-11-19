"""An address on the instance-administrator list is reserved.

The role follows the address (``BOW_SUPERUSER_EMAILS``), so an account can be
created under a listed address only with proof of it: the first-user
bootstrap, an invite link sent to it, or a provider trusted for the email.
Open sign-ups, a domain policy and an untrusted provider are refused, with the
same answer an uninvited address gets.
"""
import pytest

from app.settings.config import settings as bow_settings
from tests.e2e.org_isolation.identity_helpers import (
    FakeIdentityProvider, bootstrap_admin, enable_domain_signup, feature, hdr, invite, login, oidc_provider, register,
    unique_email, whoami,
)


@pytest.fixture
def reserved(test_client, monkeypatch):
    admin = bootstrap_admin(test_client)
    email = unique_email("Root.Operator")
    # Listed in a different letter case than it is typed at sign-up.
    monkeypatch.setattr(bow_settings.bow_config.auth, "superuser_emails", [email.upper(), "other@ops.example"])
    return {"admin": admin, "email": email.lower()}


def _no_account(client, email):
    return login(client, email).status_code == 400


def test_open_sign_up_is_refused_for_a_reserved_address(test_client, reserved):
    with feature("allow_uninvited_signups", True):
        refused = register(test_client, reserved["email"])
        assert refused.status_code == 400, refused.text
        # Anyone else still signs up, and gets no role from it.
        assert register(test_client, unique_email("walk_in")).status_code == 201
    with feature("allow_uninvited_signups", False):
        closed_answer = register(test_client, unique_email("stranger"))
    # Indistinguishable from the answer an uninvited address gets.
    assert refused.json() == closed_answer.json()
    assert _no_account(test_client, reserved["email"])


def test_domain_policy_sign_up_is_refused_for_a_reserved_address(test_client, reserved, monkeypatch):
    admin = reserved["admin"]
    enable_domain_signup(test_client, monkeypatch, admin, ["test.com"])

    assert register(test_client, reserved["email"]).status_code == 400
    assert _no_account(test_client, reserved["email"])
    # The policy itself still admits the domain's other addresses.
    assert register(test_client, unique_email("colleague")).status_code == 201


def test_invite_link_registration_is_accepted_and_grants_the_role(test_client, reserved):
    admin = reserved["admin"]
    _, token = invite(test_client, admin["token"], admin["org_id"], reserved["email"])

    assert register(test_client, reserved["email"], invite_token=token).status_code == 201

    session = login(test_client, reserved["email"]).json()["access_token"]
    me = whoami(test_client, session)
    assert me["is_superuser"] is True
    assert [o["id"] for o in me["organizations"]] == [admin["org_id"]]


@pytest.mark.parametrize("open_signups", [False, True])
def test_untrusted_provider_cannot_create_a_reserved_account(test_client, reserved, monkeypatch, open_signups):
    idp = FakeIdentityProvider(monkeypatch, [oidc_provider(trust_email_for_linking=False)])
    with feature("allow_uninvited_signups", open_signups):
        outcome, code = idp.sign_in(test_client, "corp", reserved["email"])
        stranger = idp.sign_in(test_client, "corp", unique_email("stranger")) if not open_signups else None
    assert (outcome, code) == ("error", "invitation_required")
    if stranger is not None:
        assert stranger == (outcome, code)
    # No account was left behind for the address: its invite link still registers it.
    admin = reserved["admin"]
    _, token = invite(test_client, admin["token"], admin["org_id"], reserved["email"])
    assert register(test_client, reserved["email"], invite_token=token).status_code == 201


def test_trusted_provider_creates_the_reserved_account_with_the_role(test_client, reserved, monkeypatch):
    admin = reserved["admin"]
    invite(test_client, admin["token"], admin["org_id"], reserved["email"])
    idp = FakeIdentityProvider(monkeypatch, [oidc_provider()])

    outcome, session = idp.sign_in(test_client, "corp", reserved["email"])

    assert outcome == "ok"
    me = whoami(test_client, session)
    assert me["email"] == reserved["email"] and me["is_superuser"] is True


def test_unlisted_accounts_get_no_role(test_client, reserved):
    assert whoami(test_client, reserved["admin"]["token"]).get("is_superuser") is not True
