"""An invite link admits its holder to the organization that issued it — only.

Contract (several organizations on one instance):
* Registering with organization A's link joins A. An invite another
  organization holds for the same address stays pending.
* That other invite is accepted with its own link by the signed-in account it
  was addressed to, or by a sign-in through a provider trusted for the email.
* Someone else's link is refused and leaves the invite pending.
* Two organizations inviting the same address never break registration.
With a single organization there is one invite, and registration is unchanged.
"""
import pytest

from tests.e2e.org_isolation.identity_helpers import (
    FakeIdentityProvider, bootstrap_admin, feature, hdr, invite, login, oidc_provider, org_ids,
    pending_invite_emails, register, second_org_admin, unique_email, whoami,
)


@pytest.fixture
def two_orgs(test_client):
    with feature("allow_multiple_organizations", True):
        first = bootstrap_admin(test_client)
        second = second_org_admin(test_client, first)
        yield first, second


def _accept(client, session, token):
    return client.post("/api/invites/accept", json={"token": token}, headers=hdr(session))


@pytest.mark.parametrize("other_role", ["member", "admin"])
def test_registering_with_one_link_leaves_the_other_organizations_invite_pending(test_client, two_orgs, other_role):
    first, second = two_orgs
    email = unique_email("invitee")
    _, first_token = invite(test_client, first["token"], first["org_id"], email)
    invite(test_client, second["token"], second["org_id"], email, role=other_role)

    assert register(test_client, email, invite_token=first_token).status_code == 201
    session = login(test_client, email).json()["access_token"]

    assert org_ids(test_client, session) == {first["org_id"]}
    assert email in pending_invite_emails(test_client, second["token"], second["org_id"])
    denied = test_client.get(f"/api/organizations/{second['org_id']}/members", headers=hdr(session, second["org_id"]))
    assert denied.status_code == 403


def test_pending_invite_is_accepted_with_its_own_link(test_client, two_orgs):
    first, second = two_orgs
    email = unique_email("invitee")
    _, first_token = invite(test_client, first["token"], first["org_id"], email)
    _, second_token = invite(test_client, second["token"], second["org_id"], email, role="admin")
    assert register(test_client, email, invite_token=first_token).status_code == 201
    session = login(test_client, email).json()["access_token"]

    accepted = _accept(test_client, session, second_token)

    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["organization_id"] == second["org_id"]
    assert org_ids(test_client, session) == {first["org_id"], second["org_id"]}
    assert email not in pending_invite_emails(test_client, second["token"], second["org_id"])
    roles = {o["id"]: o["role"] for o in whoami(test_client, session)["organizations"]}
    assert roles[second["org_id"]] == "admin" and roles[first["org_id"]] == "member"
    # A link works once.
    assert _accept(test_client, session, second_token).status_code == 400


def test_a_link_addressed_to_someone_else_is_refused(test_client, two_orgs):
    first, second = two_orgs
    email = unique_email("invitee")
    _, second_token = invite(test_client, second["token"], second["org_id"], email, role="admin")

    # Neither another organization's admin nor an unrelated member can use it.
    outsider = unique_email("outsider")
    _, outsider_token = invite(test_client, first["token"], first["org_id"], outsider)
    assert register(test_client, outsider, invite_token=outsider_token).status_code == 201
    for session in (first["token"], login(test_client, outsider).json()["access_token"]):
        resp = _accept(test_client, session, second_token)
        assert resp.status_code == 403, resp.text
        assert second["org_id"] not in org_ids(test_client, session)
    assert email in pending_invite_emails(test_client, second["token"], second["org_id"])

    assert _accept(test_client, first["token"], "not-an-invite-token").status_code == 400
    assert test_client.post("/api/invites/accept", json={"token": second_token}).status_code == 401


def test_two_organizations_inviting_one_address_do_not_break_registration(test_client, two_orgs):
    first, second = two_orgs
    email = unique_email("invitee")
    invite(test_client, first["token"], first["org_id"], email)
    _, second_token = invite(test_client, second["token"], second["org_id"], email)

    # Without a link the person is pointed at their invite, in any letter case.
    for typed in (email, email.upper()):
        assert register(test_client, typed).status_code == 400
    # With open sign-ups the account is created, but joins no organization on
    # the strength of a typed address.
    with feature("allow_uninvited_signups", True):
        assert register(test_client, email).status_code == 201
    session = login(test_client, email).json()["access_token"]
    assert org_ids(test_client, session) == set()
    assert _accept(test_client, session, second_token).status_code == 200
    assert org_ids(test_client, session) == {second["org_id"]}


def test_trusted_provider_sign_in_claims_the_remaining_invites(test_client, two_orgs, monkeypatch):
    first, second = two_orgs
    email = unique_email("invitee")
    _, first_token = invite(test_client, first["token"], first["org_id"], email)
    invite(test_client, second["token"], second["org_id"], email)
    assert register(test_client, email, invite_token=first_token).status_code == 201
    user_id = whoami(test_client, login(test_client, email).json()["access_token"])["id"]
    idp = FakeIdentityProvider(monkeypatch, [oidc_provider()])

    outcome, session = idp.sign_in(test_client, "corp", email)

    assert outcome == "ok" and whoami(test_client, session)["id"] == user_id
    assert org_ids(test_client, session) == {first["org_id"], second["org_id"]}


def test_untrusted_provider_sign_up_is_refused_and_invites_stay_claimable(test_client, two_orgs, monkeypatch):
    first, second = two_orgs
    email = unique_email("invitee")
    invite(test_client, first["token"], first["org_id"], email)
    _, second_token = invite(test_client, second["token"], second["org_id"], email)
    idp = FakeIdentityProvider(monkeypatch, [oidc_provider("corp"), oidc_provider("partner")])

    assert idp.sign_in(test_client, "corp", email) == ("error", "invite_link_required")

    for org in (first, second):
        assert email in pending_invite_emails(test_client, org["token"], org["org_id"])
    # The emailed link is still the way in, for the person who received it.
    assert register(test_client, email, invite_token=second_token).status_code == 201
    session = login(test_client, email).json()["access_token"]
    assert org_ids(test_client, session) == {second["org_id"]}


def test_trusted_provider_sign_up_joins_every_inviting_organization(test_client, two_orgs, monkeypatch):
    first, second = two_orgs
    email = unique_email("invitee")
    invite(test_client, first["token"], first["org_id"], email)
    invite(test_client, second["token"], second["org_id"], email)
    idp = FakeIdentityProvider(monkeypatch, [oidc_provider()])

    outcome, session = idp.sign_in(test_client, "corp", email)

    assert outcome == "ok"
    assert org_ids(test_client, session) == {first["org_id"], second["org_id"]}


def test_single_organization_invite_registration_is_unchanged(test_client):
    admin = bootstrap_admin(test_client)
    email = unique_email("invitee")
    _, token = invite(test_client, admin["token"], admin["org_id"], email, role="admin")

    assert register(test_client, email, invite_token=token).status_code == 201
    session = login(test_client, email).json()["access_token"]

    me = whoami(test_client, session)
    assert [(o["id"], o["role"]) for o in me["organizations"]] == [(admin["org_id"], "admin")]
    assert me["is_verified"] is True
    assert email not in pending_invite_emails(test_client, admin["token"], admin["org_id"])
